"""Отправка готовых писем из вашего почтового ящика (SMTP) с защитой от повторов и перегруза.

Схема: Cowork читает таблицу компаний (leads_<дата>.csv), для каждой готовит письмо и записывает
его в файл `outbox_<дата>.csv` с колонками «Почта; Тема; Текст». Этот скрипт отправляет письма
из файла по одному, с паузой, и ведёт журнал отправленного.

Что он гарантирует:
    * по умолчанию ничего не отправляется: пробный запуск показывает первые письма целиком;
      настоящая отправка включается ключом --send;
    * одному адресу письмо уходит не больше одного раза - ни при повторном запуске, ни из другого
      outbox-файла (журнал sent_mail.json). Если связь оборвалась в момент отправки и неизвестно,
      дошло ли письмо, адрес помечается «sending» и сам не повторяется: проверьте папку «Отправленные»;
    * не больше --daily-limit писем за последние 24 часа (по умолчанию 50: новый или давно молчавший
      ящик нужно «разгонять» постепенно, иначе почтовик пометит его как спамера);
    * адреса из списка отказов (suppress.txt: адрес или @домен на строку) пропускаются;
    * при отказе почтового сервера (лимит, ошибка входа) отправка останавливается, а не долбит дальше.

Пароль и настройки сервера берутся из переменных окружения, в файлы и журнал не пишутся:
    SMTP_HOST, SMTP_PORT (587 - STARTTLS, 465 - SSL), SMTP_USER, SMTP_PASSWORD,
    MAIL_FROM (необязательно, по умолчанию SMTP_USER), MAIL_FROM_NAME (необязательно).

Запуск:
    python send_mail.py outbox_2026-10-09.csv                # пробный: ничего не уходит
    python send_mail.py outbox_2026-10-09.csv --send --limit 3
    python send_mail.py outbox_2026-10-09.csv --send --daily-limit 200
"""
import argparse
import csv
import io
import json
import os
import re
import smtplib
import ssl
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

SENT = "sent"               # письмо ушло
SENDING = "sending"         # отправка началась, исход неизвестен (оборвалась связь): сам не повторяется
REJECTED = "rejected"       # сервер отверг адрес получателя - окончательно

OK_EXIT, INPUT_ERROR, AUTH_ERROR, SERVER_STOP, INTERRUPTED, TOO_MANY_REJECTS = 0, 1, 2, 3, 4, 5

COLUMN_NAMES = {
    "email": ("почта", "email", "e-mail", "to", "кому"),
    "subject": ("тема", "subject"),
    "body": ("текст", "письмо", "body", "text"),
}
EMAIL_RE = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")
WINDOW = timedelta(hours=24)


class InputError(Exception):
    """Неверный файл с письмами или настройки: понятное сообщение для пользователя."""


@dataclass
class Row:
    line: int
    email: str
    subject: str
    body: str


@dataclass
class Settings:
    host: str
    port: int
    user: str
    password: str
    sender: str = ""
    name: str = ""

    @property
    def from_address(self) -> str:
        return self.sender or self.user


def load_settings(env=os.environ) -> Settings:
    missing = [name for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD") if not env.get(name)]
    if missing:
        raise InputError("Не заданы переменные окружения: " + ", ".join(missing)
                         + ". Значения в сообщениях не печатаются; см. README, раздел «Рассылка».")
    try:
        port = int(env.get("SMTP_PORT") or 587)
    except ValueError:
        raise InputError("SMTP_PORT должен быть числом (587 или 465)") from None
    return Settings(env["SMTP_HOST"], port, env["SMTP_USER"], env["SMTP_PASSWORD"],
                    env.get("MAIL_FROM", ""), env.get("MAIL_FROM_NAME", ""))


# ---------- чтение файлов ----------

def read_outbox(path: str) -> list:
    """Строки файла с письмами. Разделитель «;» или «,» определяется по заголовку."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except OSError as exc:
        raise InputError(f"Не удалось открыть {path}: {exc.strerror or exc}") from None
    header = text.split("\n", 1)[0]
    delimiter = ";" if header.count(";") >= header.count(",") and ";" in header else ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        names = [cell.strip().lower() for cell in next(reader)]
    except StopIteration:
        raise InputError(f"Файл {path} пуст") from None

    index = {}
    for key, aliases in COLUMN_NAMES.items():
        for position, name in enumerate(names):
            if name in aliases:
                index[key] = position
                break
    absent = [aliases[0].capitalize() for key, aliases in COLUMN_NAMES.items() if key not in index]
    if absent:
        raise InputError("В первой строке файла должны быть колонки «Почта», «Тема», «Текст»; нет: " + ", ".join(absent))

    rows = []
    for cells in reader:
        if not any(cell.strip() for cell in cells):
            continue
        get = lambda key: cells[index[key]] if index[key] < len(cells) else ""
        rows.append(Row(reader.line_num, get("email"), get("subject"), get("body")))
    return rows


def read_suppress(path: str) -> set:
    """Адреса и @домены, которым писать нельзя. Нет файла - пустой список."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            lines = [line.strip().lower() for line in fh]
    except FileNotFoundError:
        return set()
    return {line for line in lines if line and not line.startswith("#")}


def is_suppressed(email: str, suppress: set) -> bool:
    return email in suppress or "@" + email.rsplit("@", 1)[-1] in suppress


# ---------- журнал отправленного ----------

def load_ledger(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except ValueError:
        raise InputError(f"Журнал {path} повреждён. Не удаляйте его молча: в нём список тех, "
                         "кому уже писали. Верните копию или поправьте файл.") from None
    return data if isinstance(data, dict) else {}


def save_ledger(path: str, ledger: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def sent_in_window(ledger: dict, now: datetime) -> int:
    count = 0
    for entry in ledger.values():
        if entry.get("status") in (SENT, SENDING):
            try:
                when = datetime.fromisoformat(entry["at"])
            except (KeyError, ValueError):
                continue
            if now - when < WINDOW:
                count += 1
    return count


class Lock:
    """Не даёт запустить две отправки сразу: иначе одно письмо могло бы уйти дважды."""

    def __init__(self, path: str):
        self.path = path

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise InputError(f"Уже идёт другая отправка. Если её нет (прошлая оборвалась аварийно), "
                             f"удалите файл {self.path}.") from None
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass


# ---------- письмо и соединение ----------

def build_message(settings: Settings, row: Row, email: str, subject: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((settings.name, settings.from_address)) if settings.name else settings.from_address
    msg["To"] = email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=settings.from_address.rsplit("@", 1)[-1])
    msg.set_content(row.body.replace("\r\n", "\n").strip() + "\n", charset="utf-8")
    return msg


def smtp_connect(settings: Settings, timeout: float = 30.0):
    context = ssl.create_default_context()
    if settings.port == 465:
        smtp = smtplib.SMTP_SSL(settings.host, settings.port, timeout=timeout, context=context)
    else:
        smtp = smtplib.SMTP(settings.host, settings.port, timeout=timeout)
        smtp.ehlo()
        smtp.starttls(context=context)       # без шифрования пароль не отправляем
        smtp.ehlo()
    smtp.login(settings.user, settings.password)
    return smtp


def _close(smtp) -> None:
    try:
        smtp.quit()
    except (smtplib.SMTPException, OSError):
        pass


def _alive(smtp) -> bool:
    try:
        return smtp.noop()[0] == 250
    except (smtplib.SMTPException, OSError):
        return False


def _reason(exc) -> str:
    code = getattr(exc, "smtp_code", None)
    text = getattr(exc, "smtp_error", b"")
    text = text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)
    return f"{code} {text}".strip() if code else (text or type(exc).__name__)


# ---------- основной цикл ----------

def run(outbox: str, ledger_path: str, settings=None, send: bool = False, limit: int = 0,
        daily_limit: int = 50, pause: float = 45.0, suppress_path: str = "suppress.txt",
        retry_unknown: bool = False, preview: int = 3, max_rejects_in_a_row: int = 5,
        connect=smtp_connect, sleep=time.sleep, now=lambda: datetime.now(timezone.utc),
        log=print) -> int:
    """Код выхода: 0 - готово, 1 - ошибка в файлах/настройках, 2 - не удалось войти в ящик,
    3 - сервер отказал или связь оборвалась (остановлено), 4 - прервано вами, 5 - подряд слишком
    много отвергнутых адресов."""
    try:
        rows = read_outbox(outbox)
        suppress = read_suppress(suppress_path)
        if send and settings is None:
            settings = load_settings()
        if send:
            with Lock(ledger_path + ".lock"):
                return _run(rows, ledger_path, settings, True, limit, daily_limit, pause, suppress,
                            retry_unknown, preview, max_rejects_in_a_row, connect, sleep, now, log)
        return _run(rows, ledger_path, settings, False, limit, daily_limit, pause, suppress,
                    retry_unknown, preview, max_rejects_in_a_row, connect, sleep, now, log)
    except InputError as exc:
        log(str(exc))
        return INPUT_ERROR


def _run(rows, ledger_path, settings, send, limit, daily_limit, pause, suppress, retry_unknown,
         preview, max_rejects_in_a_row, connect, sleep, now, log) -> int:
    ledger = load_ledger(ledger_path)
    skipped = Counter()
    queue, seen = [], set()
    for row in rows:
        email = row.email.strip().lower()
        subject = " ".join(row.subject.split())         # перевод строки в теме - это подделка заголовков
        if not EMAIL_RE.match(email):
            skipped["некорректная почта"] += 1
        elif not subject or not row.body.strip():
            skipped["пустая тема или текст"] += 1
        elif email in seen:
            skipped["повтор адреса в файле"] += 1
        elif is_suppressed(email, suppress):
            skipped["в списке отказов"] += 1
        elif email in ledger and not (retry_unknown and ledger[email].get("status") == SENDING):
            skipped[{SENT: "уже отправлено раньше", SENDING: "исход прошлой отправки неизвестен "
                     "(проверьте «Отправленные»; --retry-unknown повторит)",
                     REJECTED: "адрес отвергнут сервером раньше"}.get(ledger[email].get("status"), "есть в журнале")] += 1
        else:
            queue.append((row, email, subject))
        seen.add(email)

    room = max(daily_limit - sent_in_window(ledger, now()), 0)
    batch = queue[:min(room, limit) if limit else room]
    postponed = len(queue) - len(batch)

    log(f"В файле писем: {len(rows)}. К отправке: {len(batch)}"
        + (f"; отложено до следующих суток/запусков: {postponed}" if postponed else "") + ".")
    for reason, number in skipped.items():
        log(f"Пропущено ({reason}): {number}")
    if postponed and room == 0:
        log(f"Дневной лимит {daily_limit} за последние 24 часа достигнут. Остальное - позже.")

    if not send:
        for row, email, subject in batch[:preview]:
            log(f"--- Письмо для {email} ---\nТема: {subject}\n\n{row.body.strip()}\n")
        log("Это пробный запуск: ничего не отправлено. Если письма в порядке, запустите с ключом --send.")
        return OK_EXIT

    smtp, code, rejects_in_a_row = None, OK_EXIT, 0
    try:
        for number, (row, email, subject) in enumerate(batch, 1):
            if number > 1:
                sleep(pause)
            try:
                if smtp is None or not _alive(smtp):
                    if smtp is not None:
                        _close(smtp)
                    smtp = connect(settings)
            except smtplib.SMTPAuthenticationError:
                log("Почтовый сервер не принял логин или пароль. Для Gmail/Яндекса/Mail.ru нужен "
                    "пароль приложения, а не обычный пароль от ящика.")
                code = AUTH_ERROR
                break
            except (smtplib.SMTPException, OSError) as exc:
                log(f"Не удалось подключиться к почтовому серверу: {_reason(exc)}"
                    .replace(settings.password, "***"))
                code = SERVER_STOP
                break

            try:
                message = build_message(settings, row, email, subject)
            except ValueError as exc:
                log(f"[{number}/{len(batch)}] {email}: письмо не собрано ({exc}), пропущено")
                continue

            ledger[email] = {"status": SENDING, "at": now().isoformat(), "subject": subject}
            save_ledger(ledger_path, ledger)            # до отправки: оборванная отправка не повторится сама
            try:
                smtp.send_message(message)
            except smtplib.SMTPRecipientsRefused as exc:
                code_, text = next(iter(exc.recipients.values()), (0, b""))
                text = text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)
                ledger[email].update(status=REJECTED, error=f"{code_} {text}".strip()[:200])
                save_ledger(ledger_path, ledger)
                log(f"[{number}/{len(batch)}] {email}: адрес отвергнут сервером")
                rejects_in_a_row += 1
                if rejects_in_a_row >= max_rejects_in_a_row:
                    log(f"{rejects_in_a_row} отвергнутых адресов подряд - останавливаемся: возможно, "
                        "ящик заблокирован или список адресов плохой.")
                    code = TOO_MANY_REJECTS
                    break
                continue
            except smtplib.SMTPAuthenticationError:
                del ledger[email]
                save_ledger(ledger_path, ledger)
                log("Почтовый сервер разлогинил ящик (ошибка входа). Отправка остановлена.")
                code = AUTH_ERROR
                break
            except smtplib.SMTPResponseException as exc:
                del ledger[email]                       # сервер ответил отказом: письмо не ушло
                save_ledger(ledger_path, ledger)
                log(f"Сервер отказал: {_reason(exc)}. Отправка остановлена (возможно, исчерпан лимит "
                    "ящика); это письмо не ушло и будет отправлено при следующем запуске.")
                code = SERVER_STOP
                break
            except (smtplib.SMTPException, OSError) as exc:
                log(f"[{number}/{len(batch)}] {email}: связь оборвалась в момент отправки "
                    f"({type(exc).__name__}). Неизвестно, дошло ли письмо: адрес помечен «sending» и сам не "
                    "повторится; проверьте папку «Отправленные». Отправка остановлена.")
                code = SERVER_STOP
                break

            rejects_in_a_row = 0
            ledger[email].update(status=SENT, at=now().isoformat())
            save_ledger(ledger_path, ledger)
            log(f"[{number}/{len(batch)}] {email}: отправлено")
    except KeyboardInterrupt:
        log("Остановлено вами. Журнал сохранён; если прервали в момент отправки, проверьте «Отправленные».")
        code = INTERRUPTED
    finally:
        if smtp is not None:
            _close(smtp)

    done = sum(1 for e in ledger.values() if e.get("status") == SENT)
    log(f"Готово. Отправлено всего (по журналу): {done}; за последние 24 часа: {sent_in_window(ledger, now())}.")
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Отправка писем из outbox-файла (Почта; Тема; Текст) из вашего ящика")
    parser.add_argument("outbox", help="CSV с колонками «Почта», «Тема», «Текст»")
    parser.add_argument("--send", action="store_true", help="отправлять по-настоящему (без ключа - пробный запуск)")
    parser.add_argument("--limit", type=int, default=0, help="сколько писем отправить за этот запуск (0 - сколько позволит лимит)")
    parser.add_argument("--daily-limit", type=int, default=50, help="не больше писем за 24 часа (по умолчанию 50)")
    parser.add_argument("--pause", type=float, default=45.0, help="пауза между письмами, с (по умолчанию 45)")
    parser.add_argument("--ledger", default="sent_mail.json", help="журнал отправленного")
    parser.add_argument("--suppress", default="suppress.txt", help="список отказов: адрес или @домен на строку")
    parser.add_argument("--retry-unknown", action="store_true", help="повторить письма со статусом sending (исход неизвестен)")
    parser.add_argument("--preview", type=int, default=3, help="сколько писем показать целиком в пробном запуске")
    args = parser.parse_args(argv)
    return run(args.outbox, args.ledger, send=args.send, limit=args.limit, daily_limit=args.daily_limit,
               pause=args.pause, suppress_path=args.suppress, retry_unknown=args.retry_unknown,
               preview=args.preview)


if __name__ == "__main__":
    sys.exit(main())
