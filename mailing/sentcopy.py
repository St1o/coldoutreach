"""Копия отправленного письма в папку «Отправленные» того же ящика mail.ru (IMAP, imap.mail.ru:993).

К этому моменту письмо уже принято SMTP-сервером, поэтому любой сбой здесь - только предупреждение: save() не бросает
исключений, ничего не отправляет заново и не трогает журнал отправки. В APPEND уходят те же байты, что и по SMTP
(msg.as_bytes(): те же Date и Message-ID), с флагом \\Seen. Одно соединение на ящик на запуск; оборвалось - следующее письмо
открывает новое. Логин и пароль те же, что для SMTP («пароль для внешних приложений», в настройках почты должен быть
включён доступ по IMAP). Только стандартная библиотека."""
import base64
import imaplib
import os
import re
import ssl

from . import config, journal
from .smtp import make_ssl_context


SEEN = "\\Seen"
SENT_FLAG = "\\sent"                                   # специальный атрибут папки в ответе LIST (RFC 6154), без учёта регистра
FALLBACK_FOLDERS = ("Отправленные", "Sent")            # если у сервера нет атрибута \Sent; папку сравниваем по имени
TIMEOUT = 20.0                                         # IMAP не должен задерживать рассылку дольше, чем нужно
MAX_FAILURES_IN_A_ROW = 3                              # столько сбоев подряд - и до конца запуска больше не пробуем
STOP_NOTE = "; до конца запуска больше не пробуем"
REPORT_NAME = "sent_copy.json"                         # итог последнего запуска для mailer.py --status (адресов в нём нет)

_LIST_LINE = re.compile(r'\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>"(?:[^"\\]|\\.)*"|\S+)\s*$')
_LIST_LITERAL = re.compile(r'\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+\{\d+\}\s*$')


def utf7_encode(name: str) -> str:
    """Имя папки в modified UTF-7 (RFC 3501): «Отправленные» -> &BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-."""
    out, chunk = [], []

    def flush():
        if chunk:
            raw = "".join(chunk).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-")
            chunk.clear()

    for char in name:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            out.append("&-" if char == "&" else char)
        else:
            chunk.append(char)
    flush()
    return "".join(out)


def utf7_decode(name: str) -> str:
    def part(found):
        if not found.group(1):
            return "&"
        data = found.group(1).replace(",", "/")
        try:
            return base64.b64decode(data + "=" * (-len(data) % 4)).decode("utf-16-be")
        except ValueError:
            return found.group(0)
    return re.sub(r"&([^-]*)-", part, name)


def parse_list(data) -> list:
    """[(флаги маленькими буквами, имя папки как прислал сервер)] из ответа LIST. Имя-литерал imaplib отдаёт кортежем."""
    found = []
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[0], bytes) and isinstance(item[1], bytes):
            head = _LIST_LITERAL.match(item[0].decode("utf-8", "replace").strip())
            if head:
                found.append((set(head.group("flags").lower().split()), item[1].decode("utf-8", "replace")))
            continue
        if not isinstance(item, bytes):
            continue
        line = _LIST_LINE.match(item.decode("utf-8", "replace").strip())
        if line:
            name = line.group("name")
            if name.startswith('"'):
                name = re.sub(r"\\(.)", r"\1", name[1:-1])
            found.append((set(line.group("flags").lower().split()), name))
    return found


def find_sent_folder(entries):
    """Имя папки «Отправленные» как его прислал сервер: по атрибуту \\Sent, иначе по имени «Отправленные» или Sent.
    Нет такой - None (новую папку не создаём)."""
    for flags, name in entries:
        if SENT_FLAG in flags and "\\noselect" not in flags:
            return name
    by_name = {utf7_decode(name).casefold(): name for flags, name in entries if "\\noselect" not in flags}
    for candidate in FALLBACK_FOLDERS:
        if candidate.casefold() in by_name:
            return by_name[candidate.casefold()]
    return None


def default_connect(host: str, port: int, timeout: float):
    """Защищённое соединение с проверкой сертификата, с тем же набором доверенных центров, что и у SMTP."""
    return imaplib.IMAP4_SSL(host, port, ssl_context=make_ssl_context(), timeout=timeout)


def _text(exc) -> str:
    parts = [arg.decode("utf-8", "replace") if isinstance(arg, bytes) else str(arg) for arg in exc.args]
    return " ".join(part for part in parts if part).strip() or type(exc).__name__


class CopyError(Exception):
    """Копию не сохранили. reason - причина для журнала; fatal - исправить это можно только руками, повторять в этом запуске нечего."""

    def __init__(self, reason: str, fatal: bool = False):
        super().__init__(reason)
        self.reason, self.fatal = reason, fatal


class SentFolderSaver:
    """Одно IMAP-соединение одного ящика на весь запуск. save(msg) -> (True, "") или (False, причина), исключений не бросает."""

    def __init__(self, user: str, password: str, host: str = config.IMAP_HOST, port: int = config.IMAP_PORT,
                 connect=None, timeout: float = TIMEOUT, off_reason: str = ""):
        self.user, self._password = user, password
        self._host, self._port, self._timeout = host, port, timeout
        self._connect = connect or default_connect
        self._imap, self._folder = None, None
        self._off = off_reason                         # причина, по которой копию не пробуем вовсе (неверная настройка, прошлые сбои)
        self._failures = 0

    def _clean(self, text: str) -> str:
        """Причина для журнала: без пароля, одной строкой, не длиннее 220 знаков."""
        if self._password:
            text = text.replace(self._password, "***")
        return " ".join(text.split())[:220]

    def save(self, msg):
        if self._off:
            return False, self._off
        fatal = False
        try:
            self._store(msg)
        except CopyError as exc:
            reason, fatal = exc.reason, exc.fatal
        except Exception as exc:                        # письмо уже ушло: что бы ни случилось, это не сбой отправки
            self._drop()
            reason = f"{type(exc).__name__}: {_text(exc)[:150]}"
        else:
            self._failures = 0
            return True, ""
        self._failures += 1
        reason = self._clean(reason)
        if fatal or self._failures >= MAX_FAILURES_IN_A_ROW:
            self._off = reason + STOP_NOTE
            return False, self._off
        return False, reason

    def _store(self, msg) -> None:
        data = msg.as_bytes()                           # те же байты, что ушли по SMTP: ничего не пересобираем
        imap = self._connection()
        folder = self._folder_of(imap)
        try:
            kind, reply = imap.append(folder, SEEN, None, data)
        except (imaplib.IMAP4.error, OSError) as exc:   # abort и SSLError входят сюда же
            self._drop()
            raise CopyError(f"связь с {self._host}:{self._port} оборвалась при сохранении ({type(exc).__name__}: {_text(exc)[:120]})")
        if kind != "OK":
            self._folder = None                         # возможно, папку переименовали: в следующий раз найдём заново
            raise CopyError("сервер не принял копию: " + kind + " " + " ".join(
                part.decode("utf-8", "replace") if isinstance(part, bytes) else str(part) for part in (reply or []))[:150])

    def _connection(self):
        if self._imap is not None:
            try:
                alive = self._imap.noop()[0] == "OK"
            except (imaplib.IMAP4.error, OSError):
                alive = False
            if not alive:
                self._drop()
        if self._imap is None:
            self._imap = self._open()
        return self._imap

    def _open(self):
        where = f"{self._host}:{self._port}"
        try:
            imap = self._connect(self._host, self._port, self._timeout)
        except ssl.SSLCertVerificationError as exc:     # такой же случай, как у SMTP: сертификат сервера не принят
            raise CopyError(f"не удалось проверить сертификат {where} ({_text(exc)[:120]}); корневой сертификат нужен тот же, "
                            "что для SMTP: запустите check_cert.py или впишите MAILRU_CA_FILE в local.env", fatal=True)
        except ssl.SSLError as exc:                     # обрыв при установке шифрования: VPN, прокси, антивирус
            raise CopyError(f"соединение с {where} оборвалось при установке защищённого соединения ({_text(exc)[:100]}): "
                            "чаще всего мешает VPN, прокси или антивирус с проверкой HTTPS")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise CopyError(f"нет соединения с {where} ({type(exc).__name__}: {_text(exc)[:100]})")
        try:
            imap.login(self.user, self._password)       # при отказе imaplib сам бросает IMAP4.error
        except imaplib.IMAP4.abort as exc:
            raise CopyError(f"{where} оборвал связь при входе ({_text(exc)[:100]})")
        except imaplib.IMAP4.error as exc:
            self._shutdown(imap)
            raise CopyError(f"IMAP не принял логин или пароль ({_text(exc)[:100]}): включите доступ по IMAP в настройках "
                            "почты mail.ru; нужен «пароль для внешних приложений»", fatal=True)
        except OSError as exc:
            self._shutdown(imap)
            raise CopyError(f"связь с {where} оборвалась при входе ({type(exc).__name__}: {_text(exc)[:100]})")
        return imap

    def _folder_of(self, imap) -> str:
        if self._folder:
            return self._folder
        try:
            kind, data = imap.list()
        except (imaplib.IMAP4.error, OSError) as exc:
            self._drop()
            raise CopyError(f"связь с {self._host}:{self._port} оборвалась при чтении папок ({type(exc).__name__}: {_text(exc)[:100]})")
        if kind != "OK":
            raise CopyError(f"сервер не показал список папок ({kind})")
        folder = find_sent_folder(parse_list(data))
        if folder is None:
            raise CopyError("в ящике нет папки «Отправленные» (ни с атрибутом \\Sent, ни «Отправленные», ни Sent); "
                            "новую не создаём", fatal=True)
        self._folder = folder
        return folder

    @staticmethod
    def _shutdown(imap) -> None:
        try:
            imap.shutdown()
        except Exception:
            pass

    def _drop(self) -> None:
        imap, self._imap = self._imap, None
        if imap is not None:
            self._shutdown(imap)

    def close(self) -> None:
        imap, self._imap = self._imap, None
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                self._shutdown(imap)


def make_saver(user: str, password: str, environ=None, connect=None, timeout: float = TIMEOUT):
    """Копировщик для ящика или None, если копия выключена (SAVE_SENT_COPY=0). Неверный MAILRU_IMAP_HOST или MAILRU_IMAP_PORT
    отправку не останавливает: копировщик создаётся, а в журнал каждый раз попадает причина."""
    if not config.resolve_sent_copy(environ):
        return None
    try:
        host, port = config.resolve_imap(environ)
    except ValueError as exc:
        return SentFolderSaver(user, password, connect=connect, timeout=timeout, off_reason=str(exc))
    return SentFolderSaver(user, password, host, port, connect, timeout)


def make_savers(boxes, environ=None, connect=None, timeout: float = TIMEOUT) -> dict:
    """{номер ящика: копировщик} для подключённых ящиков; пусто, если копия выключена."""
    savers = {}
    for box in boxes:
        saver = make_saver(box.user, box.password, environ, connect, timeout)
        if saver is not None:
            savers[box.id] = saver
    return savers


def write_report(folder: str, report: dict) -> None:
    """Итог текущего или последнего запуска (сколько копий сохранено и не сохранено) для mailer.py --status. Не журнал отправки."""
    try:
        journal.save_state(os.path.join(folder, REPORT_NAME), report)
    except OSError:
        pass


def status_line(folder: str, environ=None) -> str:
    """Одна строка для mailer.py --status: сколько копий не удалось сохранить за последний запуск."""
    if not config.resolve_sent_copy(environ):
        return "Копия в «Отправленные»: выключена (SAVE_SENT_COPY=0 в local.env)."
    report = journal.load_state(os.path.join(folder, REPORT_NAME))
    if not isinstance(report, dict) or not report:
        return "Копия в «Отправленные»: включена; запусков с копией в этой папке ещё не было."
    try:
        saved, failed = int(report.get("saved") or 0), int(report.get("failed") or 0)
    except (TypeError, ValueError):
        saved = failed = 0
    text = (f"Копия в «Отправленные» за последний запуск ({report.get('started', '?')}): сохранено {saved}, "
            f"не удалось сохранить {failed}")
    if failed and report.get("last_reason"):
        text += f" (последняя причина: {report['last_reason']})"
    return text + "."
