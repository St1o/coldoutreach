"""Отправка одного письма с сайта (страница /mail): проба цепочки «шаблон -> mail.ru».

Доступ к ящику - переменные окружения MAILRU_USER и MAILRU_APP_PASSWORD («пароль для внешних
приложений»). Пароль только оттуда: на страницу, в журнал и в ответы он не попадает.

Ограничения: одно письмо за раз и не больше max_per_hour удачных отправок в час, чтобы страница
(даже если пароль к ней утечёт) не превратилась в рассылку и не испортила репутацию ящика.
"""
import os
import smtplib
import ssl
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from mailing import config as mailcfg, mailboxes as boxes, smtp
from mailing import table as lettertable
from letter_template import DEFAULT_OFFER, render_letter

CONNECT_TIMEOUT = 20.0


def _connect_hint() -> str:
    if os.environ.get("RENDER"):                    # Render сам ставит эту переменную в своих сервисах
        return ("Сайт стоит на Render: на бесплатном плане он закрывает исходящие порты SMTP "
                "(25, 465, 587), нужен платный тип сервиса.")
    return ("Порт 465 может блокировать антивирус, брандмауэр или провайдер; проверьте, что интернет "
            "работает, и попробуйте ещё раз.")


class MailError(Exception):
    """Ошибка с кодом ответа сайта и понятным пользователю текстом."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class MailService:
    def __init__(self, user: str = "", password: str = "", from_name: str = "", example=None,
                 max_per_hour: int = 10, sender_factory=None, clock=time.monotonic,
                 port: int = smtp.SMTP_PORT, mailboxes=None):
        """mailboxes - все подключённые ящики (mailing.mailboxes.load_mailboxes): с каждого можно отправить пробное
        письмо. Не задан - один ящик из user и password, как раньше."""
        self.user = (user or "").strip()
        self._password = password or ""
        self.from_name = from_name
        self.example = example or {}
        self.max_per_hour = max_per_hour
        self.port = port
        self.mailboxes = ([boxes.Mailbox("1", self.user, self._password, from_name)] if mailboxes is None
                          else list(mailboxes))
        first = next((box for box in self.mailboxes if box.ready), None)
        if mailboxes is not None and first is not None:
            self.user, self._password = first.user, first.password
        self._custom_factory = sender_factory             # подставной отправитель для тестов, один на все ящики
        self._clock = clock
        self._busy = threading.Lock()
        self._sent = deque()                  # моменты удачных отправок за последний час

    def ready_mailboxes(self) -> list:
        return [box for box in self.mailboxes if box.ready]

    @property
    def configured(self) -> bool:
        return bool(self.ready_mailboxes())

    def _pick(self, mailbox: str = ""):
        """Ящик, с которого уйдёт пробное письмо: названный номером или первый подключённый."""
        ready = self.ready_mailboxes()
        if not mailbox:
            return ready[0]
        for box in ready:
            if box.id == str(mailbox).strip():
                return box
        raise MailError(400, "Нет такого подключённого ящика: " + str(mailbox)[:20] + ". Есть: "
                        + ", ".join(box.id for box in ready) + ".")

    def _sender_for(self, box):
        if self._custom_factory:
            return self._custom_factory()
        return smtp.MailruSender(box.user, box.password, port=self.port, timeout=CONNECT_TIMEOUT)

    def defaults(self) -> dict:
        """Готовый пример для формы. Адрес получателя по умолчанию - сам ящик отправителя:
        пробное письмо уходит владельцу, а чужие адреса в код не вписаны."""
        fields = {
            "company": self.example.get("company") or "ооо ромашка",
            "director": self.example.get("director") or "иванов иван иванович",
            "okved": self.example.get("okved") or "торговля",
            "signature": self.example.get("signature") or self.from_name or "Иван Петров, ООО «Пример»",
            "offer": self.example.get("offer") or DEFAULT_OFFER,
        }
        subject, body = render_letter(**fields)
        return {"configured": self.configured, "from": self.user,
                "mailboxes": [{"id": box.id, "address": box.user} for box in self.ready_mailboxes()],
                "to": self.example.get("to") or self.user,
                "limit_per_hour": self.max_per_hour, "subject": subject, "body": body, **fields}

    def _check_quota(self) -> None:
        now = self._clock()
        while self._sent and now - self._sent[0] >= 3600:
            self._sent.popleft()
        if len(self._sent) >= self.max_per_hour:
            minutes = max(1, int((3600 - (now - self._sent[0])) // 60) + 1)
            raise MailError(429, f"Лимит страницы: не больше {self.max_per_hour} писем в час. "
                                 f"Повторите примерно через {minutes} мин.")

    def send(self, to: str, subject: str, body: str, mailbox: str = "") -> dict:
        if not self.configured:
            raise MailError(503, "Отправка не настроена: на сервере нужны переменные окружения "
                                 "MAILRU_USER (адрес ящика) и MAILRU_APP_PASSWORD (пароль для внешних приложений).")
        to, subject, body = (to or "").strip(), (subject or "").strip(), (body or "").strip()
        if not mailcfg.check_email(to):
            raise MailError(400, "Некорректный адрес получателя.")
        if not subject or "\n" in subject or "\r" in subject or len(subject) > lettertable.MAX_SUBJECT:
            raise MailError(400, f"Тема: одна строка, от 1 до {lettertable.MAX_SUBJECT} знаков.")
        if not body or len(body) > lettertable.MAX_BODY:
            raise MailError(400, f"Текст письма: от 1 до {lettertable.MAX_BODY} знаков.")
        box = self._pick(mailbox)
        if not self._busy.acquire(blocking=False):
            raise MailError(409, "Предыдущее письмо ещё отправляется.")
        try:
            self._check_quota()
            message = smtp.build_message(lettertable.Letter(to, subject, body), box.user, box.from_name or self.from_name)
            sender = self._sender_for(box)
            which = f" ({box.label})" if len(self.ready_mailboxes()) > 1 else ""
            try:
                sender.send(message)
            except smtplib.SMTPAuthenticationError as exc:
                raise MailError(502, f"mail.ru не принял логин или пароль{which} ({smtp.reply_text(exc)}). "
                                     "Нужен «пароль для внешних приложений», а не обычный пароль от ящика; "
                                     "MAILRU_USER - полный адрес ящика.") from exc
            except smtplib.SMTPRecipientsRefused as exc:
                reply = next(iter(exc.recipients.values()), ("", b""))
                raise MailError(502, "mail.ru не принял адрес получателя: "
                                     f"{reply[0]} {reply[1].decode('utf-8', 'replace')[:150]}".strip()) from exc
            except smtplib.SMTPException as exc:           # раньше OSError: SMTPException - его подкласс
                raise MailError(502, f"mail.ru отказал или оборвал связь: {smtp.reply_text(exc)}. "
                                     "Если это лимит отправки (451), повторите позже.") from exc
            except ssl.SSLCertVerificationError as exc:    # сертификат сервера не принят
                raise MailError(502, f"Не удалось проверить сертификат {smtp.SMTP_HOST}:{self.port}: "
                                     f"{exc}. Вероятно, на этом компьютере нет корневого сертификата, которому доверяет mail.ru "
                                     "(на Windows список обновляется не сразу), либо соединение подменяет антивирус. "
                                     "Запустите check_cert.py и пришлите его вывод."
                                ) from exc
            except ssl.SSLError as exc:                    # соединение оборвано при установке шифрования (EOF и подобное)
                raise MailError(502, f"Соединение с {smtp.SMTP_HOST}:{self.port} оборвалось при установке защищённого соединения "
                                     f"({exc}). Это не пароль и не сертификат: чаще всего мешает VPN, прокси или антивирус с "
                                     "проверкой HTTPS, реже провайдер или порт. Запустите check_cert.py (правой кнопкой, "
                                     "Run 'check_cert'): он проверит порты 465 и 587 и скажет, что делать. "
                                     "Если рабочим окажется 587, впишите в local.env строку MAILRU_PORT=587."
                                ) from exc
            except OSError as exc:
                raise MailError(504, f"Не удалось соединиться с {smtp.SMTP_HOST}:{self.port} "
                                     f"({type(exc).__name__}: {exc}). {_connect_hint()}") from exc
            finally:
                sender.close()
            self._sent.append(self._clock())
        finally:
            self._busy.release()
        moment = datetime.now(timezone(timedelta(hours=3))).strftime("%H:%M:%S")
        return {"ok": True, "to": to, "from": box.user, "message_id": message["Message-ID"],
                "message": f"Сервер mail.ru принял письмо в {moment} (МСК). Проверьте входящие у "
                           f"получателя и папку «Спам»: «принято» ещё не значит «доставлено во входящие»."}
