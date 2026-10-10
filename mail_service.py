"""Отправка одного письма с сайта (страница /mail): проба цепочки «шаблон -> mail.ru».

Доступ к ящику - переменные окружения MAILRU_USER и MAILRU_APP_PASSWORD («пароль для внешних
приложений»). Пароль только оттуда: на страницу, в журнал и в ответы он не попадает.

Ограничения: одно письмо за раз и не больше max_per_hour удачных отправок в час, чтобы страница
(даже если пароль к ней утечёт) не превратилась в рассылку и не испортила репутацию ящика.
"""
import smtplib
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import mailer
from letter_template import DEFAULT_OFFER, render_letter

CONNECT_TIMEOUT = 20.0


class MailError(Exception):
    """Ошибка с кодом ответа сайта и понятным пользователю текстом."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class MailService:
    def __init__(self, user: str = "", password: str = "", from_name: str = "", example=None,
                 max_per_hour: int = 10, sender_factory=None, clock=time.monotonic,
                 port: int = mailer.SMTP_PORT):
        self.user = (user or "").strip()
        self._password = password or ""
        self.from_name = from_name
        self.example = example or {}
        self.max_per_hour = max_per_hour
        self.port = port
        self._factory = sender_factory or (lambda: mailer.MailruSender(
            self.user, self._password, port=port, timeout=CONNECT_TIMEOUT))
        self._clock = clock
        self._busy = threading.Lock()
        self._sent = deque()                  # моменты удачных отправок за последний час

    @property
    def configured(self) -> bool:
        return mailer.check_email(self.user) and bool(self._password)

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

    def send(self, to: str, subject: str, body: str) -> dict:
        if not self.configured:
            raise MailError(503, "Отправка не настроена: на сервере нужны переменные окружения "
                                 "MAILRU_USER (адрес ящика) и MAILRU_APP_PASSWORD (пароль для внешних приложений).")
        to, subject, body = (to or "").strip(), (subject or "").strip(), (body or "").strip()
        if not mailer.check_email(to):
            raise MailError(400, "Некорректный адрес получателя.")
        if not subject or "\n" in subject or "\r" in subject or len(subject) > mailer.MAX_SUBJECT:
            raise MailError(400, f"Тема: одна строка, от 1 до {mailer.MAX_SUBJECT} знаков.")
        if not body or len(body) > mailer.MAX_BODY:
            raise MailError(400, f"Текст письма: от 1 до {mailer.MAX_BODY} знаков.")
        if not self._busy.acquire(blocking=False):
            raise MailError(409, "Предыдущее письмо ещё отправляется.")
        try:
            self._check_quota()
            message = mailer.build_message(mailer.Letter(to, subject, body), self.user, self.from_name)
            sender = self._factory()
            try:
                sender.send(message)
            except smtplib.SMTPAuthenticationError as exc:
                raise MailError(502, f"mail.ru не принял логин или пароль ({mailer.reply_text(exc)}). "
                                     "Нужен «пароль для внешних приложений», а не обычный пароль от ящика; "
                                     "MAILRU_USER - полный адрес ящика.") from exc
            except smtplib.SMTPRecipientsRefused as exc:
                reply = next(iter(exc.recipients.values()), ("", b""))
                raise MailError(502, "mail.ru не принял адрес получателя: "
                                     f"{reply[0]} {reply[1].decode('utf-8', 'replace')[:150]}".strip()) from exc
            except smtplib.SMTPException as exc:           # раньше OSError: SMTPException - его подкласс
                raise MailError(502, f"mail.ru отказал или оборвал связь: {mailer.reply_text(exc)}. "
                                     "Если это лимит отправки (451), повторите позже.") from exc
            except OSError as exc:
                raise MailError(504, f"Не удалось соединиться с {mailer.SMTP_HOST}:{self.port} "
                                     f"({type(exc).__name__}). Если сайт стоит на бесплатном плане Render, "
                                     "причина, скорее всего, в этом: Render закрывает исходящие порты SMTP "
                                     "(25, 465, 587) для бесплатных сервисов. Нужен платный тип сервиса.") from exc
            finally:
                sender.close()
            self._sent.append(self._clock())
        finally:
            self._busy.release()
        moment = datetime.now(timezone(timedelta(hours=3))).strftime("%H:%M:%S")
        return {"ok": True, "to": to, "from": self.user, "message_id": message["Message-ID"],
                "message": f"Сервер mail.ru принял письмо в {moment} (МСК). Проверьте входящие у "
                           f"получателя и папку «Спам»: «принято» ещё не значит «доставлено во входящие»."}
