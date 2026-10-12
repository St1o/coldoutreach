"""Отправка по SMTP mail.ru: письмо, защищённое соединение, одно соединение на весь запуск."""
import os
import smtplib
import socket
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from .table import Letter


SMTP_HOST = "smtp.mail.ru"
SMTP_PORT = 465                 # SSL; на 587 используется STARTTLS
CONNECT_ATTEMPTS = 3            # попыток установить соединение подряд, когда его оборвали до логина (рассылка)
CONNECT_PAUSE = 8.0             # пауза перед второй попыткой, перед третьей вдвое дольше


def build_message(letter: Letter, sender: str, from_name: str = "", unsubscribe: bool = False) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, sender)) if from_name else sender
    msg["To"] = letter.email
    msg["Subject"] = letter.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    if unsubscribe:             # почтовые программы показывают кнопку «отписаться»; письмо придёт в наш ящик
        msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    msg.set_content(letter.body.replace("\r\n", "\n"), charset="utf-8", cte="base64")
    return msg


def make_ssl_context() -> ssl.SSLContext:
    """Проверка сертификата сервера включена всегда. Доверяем центрам из хранилища системы, из набора
    certifi (на Windows системный список пополняется лениво, и нужного корня в нём может не быть) и из
    необязательного файла MAILRU_CA_FILE (PEM) с дополнительным корневым сертификатом."""
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError):
        pass
    extra = os.environ.get("MAILRU_CA_FILE", "").strip()
    if extra:
        context.load_verify_locations(cafile=extra)       # неверный путь - понятная ошибка вместо молчания
    return context


def default_connect(host: str, port: int, timeout: float):
    context = make_ssl_context()
    if port == 465:
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    server = smtplib.SMTP(host, port, timeout=timeout)
    server.starttls(context=context)
    return server


def is_dropped_connection(exc) -> bool:
    """Соединение оборвано при установке (до логина и до письма): повтор безопасен, письмо не могло уйти.
    Неверный сертификат сюда не входит: его повтор не лечит и скрывать его нельзя."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    return isinstance(exc, (ssl.SSLError, ConnectionError, TimeoutError, socket.timeout, smtplib.SMTPServerDisconnected))


class MailruSender:
    """Одно соединение с SMTP mail.ru на весь запуск. Перед отправкой проверяет, что оно живо.

    connect_attempts > 1: оборванную при установке связь (is_dropped_connection) пробуем открыть ещё раз с паузой
    retry_pause, потом вдвое дольше; об этом сообщает notify(текст). Логин и само письмо не повторяются никогда."""

    def __init__(self, user: str, password: str, host: str = SMTP_HOST, port: int = SMTP_PORT,
                 connect=default_connect, timeout: float = 60.0, connect_attempts: int = 1,
                 retry_pause: float = CONNECT_PAUSE, sleep=time.sleep, notify=None):
        self.user, self._password = user, password
        self._host, self._port, self._connect, self._timeout = host, port, connect, timeout
        self._attempts, self._retry_pause, self._sleep, self._notify = max(1, connect_attempts), retry_pause, sleep, notify
        self._server = None

    def _open(self):
        pause = self._retry_pause
        for attempt in range(1, self._attempts + 1):
            try:
                return self._connect(self._host, self._port, self._timeout)
            except Exception as exc:
                if attempt == self._attempts or not is_dropped_connection(exc):
                    raise
                if self._notify:
                    self._notify(f"Соединение с {self._host} оборвано при установке шифрования ({reply_text(exc)}). "
                                 f"Письмо не уходило. Повтор через {pause:.0f} с, попытка {attempt + 1} из {self._attempts}.")
                self._sleep(pause)
                pause *= 2

    def _alive(self) -> bool:
        try:
            return self._server.noop()[0] == 250
        except (smtplib.SMTPException, OSError):
            return False

    def _ensure(self):
        if self._server is not None and not self._alive():
            self.close()
        if self._server is None:
            server = self._open()
            try:
                server.login(self.user, self._password)
            except Exception:
                try:
                    server.close()
                except Exception:
                    pass
                raise
            self._server = server
        return self._server

    def send(self, msg: EmailMessage) -> None:
        self._ensure().send_message(msg)

    def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


def reply_text(exc) -> str:
    """Код и текст ответа сервера (в ответе нет пароля; адрес получателя может быть)."""
    code = getattr(exc, "smtp_code", None)
    error = getattr(exc, "smtp_error", b"")
    text = error.decode("utf-8", "replace") if isinstance(error, bytes) else str(error)
    return (f"{code} " if code else "") + (text or str(exc))[:200]
