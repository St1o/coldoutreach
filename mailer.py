"""Отправка готовых писем через mail.ru (SMTP).

Агенты Claude Cowork формируют письма и кладут их в таблицу (CSV) с колонками
«Почта», «Тема», «Текст»: одна строка - одно письмо. Эта программа отправляет письма из таблицы.

Безопасные значения по умолчанию:
    * без ключа --send ничего не отправляется: только проверка таблицы и список «что уйдёт»;
    * за один запуск уходит не больше --limit писем (по умолчанию 20), между письмами пауза --pause;
    * рядом с таблицей ведётся журнал <таблица>.sent.json: кому уже отправлено, повторно не шлём;
    * не больше --daily-limit писем за последние 24 часа (по журналам всех таблиц letters*.sent.json в той же
      папке; по умолчанию 40: в правилах рассылки для нового ящика 30-50 писем в день);
    * адреса из стоп-листа (stoplist.txt рядом с таблицей: строка = адрес или @домен) пропускаются при каждом
      запуске, так что отписка действует сразу, даже если письмо уже лежит в таблице;
    * в каждое письмо добавляется заголовок List-Unsubscribe (mailto на адрес отправителя);
    * при ошибке входа, отказе или лимите сервера mail.ru программа сразу останавливается
      (дальше стучаться бессмысленно и вредно для репутации ящика); готовое сохранено.

Лишние колонки таблицы (например, «Группа», «Источник адреса») записываются в журнал рядом с письмом.

Доступ к ящику (в аргументы командной строки пароль не попадает):
    MAILRU_USER           полный адрес ящика, например name@mail.ru
    MAILRU_APP_PASSWORD   «пароль для внешних приложений» (обычный пароль от ящика не подойдёт)
Берутся из переменных окружения, а если их нет - из файла local.env рядом с mailer.py (тот же файл,
что использует run_local.py; в git он не попадает). Настоящие переменные окружения главнее файла.

Запуск:
    python mailer.py letters.csv                          # проверка, ничего не уходит
    python mailer.py letters.csv --send --limit 5         # отправить первые 5
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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

SMTP_HOST = "smtp.mail.ru"
SMTP_PORT = 465                 # SSL; на 587 используется STARTTLS
FIELDS = ["Почта", "Тема", "Текст"]
_ALIASES = {"почта": 0, "email": 0, "e-mail": 0, "to": 0,
            "тема": 1, "subject": 1,
            "текст": 2, "body": 2}
MAX_SUBJECT = 200
MAX_BODY = 20_000
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local.env")

SENT = "sent"             # письмо принято сервером
REJECTED = "rejected"     # сервер отказал по адресу получателя (5xx): повторно не шлём

_EMAIL_RE = re.compile(r"^[^@\s<>,;\"'()\[\]]+@[^@\s<>,;\"'()\[\]]+\.[^@\s<>,;\"'()\[\]]+$")


class LettersError(Exception):
    """Таблицу нельзя прочитать (нет файла или нужных колонок)."""


@dataclass
class Letter:
    email: str
    subject: str
    body: str
    meta: dict = field(default_factory=dict)      # прочие колонки таблицы: «Группа», «Источник адреса» и т. д.


def check_email(address: str) -> bool:
    return bool(_EMAIL_RE.match(address))


def read_stoplist(path: str) -> frozenset:
    """Стоп-лист: по строке на адрес или на домен целиком (@example.ru); пустые строки и строки с # пропускаются."""
    entries = set()
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip().lower()
            if line:
                entries.add(line)
    return frozenset(entries)


def is_stopped(address: str, stoplist) -> bool:
    address = address.lower()
    return address in stoplist or "@" + address.rpartition("@")[2] in stoplist


def read_env_file(path: str) -> dict:
    """Строки КЛЮЧ=значение; пустые строки и строки с # игнорируются; кавычки вокруг значения снимаются."""
    values = {}
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def load_env_file(path: str) -> bool:
    """Подставляет значения из файла в os.environ, не трогая уже заданные непустые переменные.
    Возвращает True, если файл найден. Нет файла - не ошибка: можно обойтись переменными окружения."""
    try:
        values = read_env_file(path)
    except FileNotFoundError:
        return False
    for key, value in values.items():
        if value and not os.environ.get(key):
            os.environ[key] = value
    return True


def read_letters(path: str):
    """Таблица -> (письма, проблемы). Проблема - (номер строки, причина); адреса в причинах нет."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except OSError as exc:
        raise LettersError(f"не удалось открыть {path}: {exc.strerror or exc}") from exc
    header = text.split("\n", 1)[0]
    delimiter = max(";,\t", key=header.count) if any(d in header for d in ";,\t") else ";"
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        raise LettersError("таблица пустая")

    columns = {}
    for index, name in enumerate(rows[0]):
        slot = _ALIASES.get(name.strip().lower())
        if slot is not None and slot not in columns:
            columns[slot] = index
    missing = [FIELDS[slot] for slot in range(3) if slot not in columns]
    if missing:
        raise LettersError("в таблице нет колонок: " + ", ".join(missing)
                           + ". Нужны: " + ", ".join(FIELDS))

    extra = {index: name.strip() for index, name in enumerate(rows[0])
             if name.strip() and index not in columns.values()}
    letters, problems, seen = [], [], set()
    for number, row in enumerate(rows[1:], 2):
        if not any(cell.strip() for cell in row):
            continue                                    # пустая строка
        cells = [row[columns[slot]].strip() if columns[slot] < len(row) else "" for slot in range(3)]
        address, subject, body = cells
        if not check_email(address):
            problems.append((number, "некорректный адрес почты"))
        elif not subject or not body:
            problems.append((number, "пустая тема или текст"))
        elif "\n" in subject or "\r" in subject:
            problems.append((number, "тема в несколько строк"))
        elif len(subject) > MAX_SUBJECT or len(body) > MAX_BODY:
            problems.append((number, "слишком длинная тема или текст"))
        elif address.lower() in seen:
            problems.append((number, "повтор адреса (второе письмо туда же не отправляем)"))
        else:
            seen.add(address.lower())
            meta = {name: row[index].strip() for index, name in extra.items()
                    if index < len(row) and row[index].strip()}
            letters.append(Letter(address, subject, body, meta))
    return letters, problems


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


def state_path(letters_path: str) -> str:
    return os.path.splitext(letters_path)[0] + ".sent.json"


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, path)


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


class MailruSender:
    """Одно соединение с SMTP mail.ru на весь запуск. Перед отправкой проверяет, что оно живо."""

    def __init__(self, user: str, password: str, host: str = SMTP_HOST, port: int = SMTP_PORT,
                 connect=default_connect, timeout: float = 60.0):
        self.user, self._password = user, password
        self._host, self._port, self._connect, self._timeout = host, port, connect, timeout
        self._server = None

    def _alive(self) -> bool:
        try:
            return self._server.noop()[0] == 250
        except (smtplib.SMTPException, OSError):
            return False

    def _ensure(self):
        if self._server is not None and not self._alive():
            self.close()
        if self._server is None:
            server = self._connect(self._host, self._port, self._timeout)
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


def sent_last_24h(directory: str, now: datetime) -> int:
    """Сколько писем ушло за последние 24 часа по журналам всех таблиц letters*.sent.json в папке."""
    total = 0
    for name in os.listdir(directory):
        if not (name.startswith("letters") and name.endswith(".sent.json")):
            continue
        for entry in load_state(os.path.join(directory, name)).values():
            if entry.get("status") != SENT:
                continue
            try:
                when = datetime.fromisoformat(entry["time"])
            except (KeyError, ValueError):
                continue
            if timedelta(0) <= now - when < timedelta(hours=24):
                total += 1
    return total


def _entry(status: str, letter: Letter, note: str = "") -> dict:
    """Запись журнала. Тема и лишние колонки таблицы (группа, источник адреса) нужны для следующих писем цепочки."""
    entry = {"status": status, "time": _now(), "note": note, "subject": letter.subject}
    if letter.meta:
        entry["meta"] = letter.meta
    return entry


def run(path: str, send: bool = False, limit: int = 20, pause: float = 30.0, user: str = "",
        password: str = "", from_name: str = "", port: int = SMTP_PORT, sender=None, log=print,
        sleep=time.sleep, max_temp_errors: int = 3, stoplist=frozenset(), daily_limit: int = 0,
        unsubscribe: bool = False, now=None) -> int:
    """Возвращает код выхода: 0 - готово, 1 - ошибка в таблице или настройках,
    2 - mail.ru не принял вход (логин/пароль), 3 - mail.ru отказал или ограничил отправку,
    5 - несколько временных отказов по получателям подряд."""
    try:
        letters, problems = read_letters(path)
    except LettersError as exc:
        log(f"Ошибка: {exc}")
        return 1
    for number, reason in problems:
        log(f"  строка {number}: {reason} - пропускаем")

    state_file = state_path(path)
    state = load_state(state_file)
    todo = [letter for letter in letters if letter.email.lower() not in state]
    skipped_done = len(letters) - len(todo)
    stopped = [letter for letter in todo if is_stopped(letter.email, stoplist)]
    if stopped:
        todo = [letter for letter in todo if not is_stopped(letter.email, stoplist)]
        log(f"В стоп-листе: {len(stopped)} адресов, им письма не уйдут.")
    if limit:
        todo = todo[:limit]
    if daily_limit:
        used = sent_last_24h(os.path.dirname(os.path.abspath(path)), now or datetime.now(timezone.utc))
        room = max(0, daily_limit - used)
        log(f"Дневной лимит {daily_limit}: за последние 24 часа уже отправлено {used}, можно ещё {room}.")
        todo = todo[:room]
    log(f"Писем в таблице: {len(letters)}; уже обработано раньше: {skipped_done}; "
        f"в этом запуске: {len(todo)}; строк с ошибками: {len(problems)}.")

    if not send:
        for number, letter in enumerate(todo, 1):
            log(f"  {number}. {letter.email} - «{letter.subject}»")
        log("Проверка без отправки: ничего не ушло. Чтобы отправить, добавьте --send.")
        return 1 if problems else 0

    if not todo:
        log("Отправлять нечего.")
        return 0
    if sender is None:
        if not user or not password:
            log("Не заданы MAILRU_USER и MAILRU_APP_PASSWORD (адрес ящика и «пароль для внешних приложений»). "
                "Впишите их в файл local.env рядом с mailer.py (см. LOCAL.md) или задайте переменными окружения.")
            return 1
        if not check_email(user):
            log("MAILRU_USER должен быть полным адресом ящика, например name@mail.ru.")
            return 1
        sender = MailruSender(user, password, port=port)

    code, temp_in_a_row, done = 0, 0, 0
    try:
        for number, letter in enumerate(todo, 1):
            if number > 1:
                sleep(pause)                    # пауза между письмами: репутация ящика важнее скорости
            try:
                sender.send(build_message(letter, sender.user, from_name, unsubscribe))
            except smtplib.SMTPAuthenticationError as exc:
                log(f"mail.ru не принял логин/пароль ({reply_text(exc)}). Нужен «пароль для внешних приложений», "
                    "а не обычный пароль от ящика. Остановлено.")
                code = 2
                break
            except smtplib.SMTPRecipientsRefused as exc:
                replies = list(exc.recipients.values())
                permanent = all(str(reply[0]).startswith("5") for reply in replies)
                note = reply_text(exc) if not replies else f"{replies[0][0]} " + replies[0][1].decode("utf-8", "replace")[:150]
                if permanent:
                    state[letter.email.lower()] = _entry(REJECTED, letter, note)
                    save_state(state_file, state)
                    log(f"[{number}/{len(todo)}] {letter.email}: адрес не принят ({note}) - пропускаем")
                    temp_in_a_row = 0
                else:
                    temp_in_a_row += 1
                    log(f"[{number}/{len(todo)}] {letter.email}: временный отказ ({note}) - попробуем в следующий запуск")
                    if temp_in_a_row >= max_temp_errors:
                        log(f"{temp_in_a_row} временных отказов подряд - останавливаемся. Повторите позже.")
                        code = 5
                        break
                continue
            except (smtplib.SMTPException, OSError) as exc:
                log(f"mail.ru не принял письмо или оборвал связь ({reply_text(exc)}). Останавливаемся: "
                    "если это лимит отправки (например 451 Ratelimit exceeded), продолжите позже, "
                    "а следующий запуск начнёт с этого письма.")
                code = 3
                break
            state[letter.email.lower()] = _entry(SENT, letter)
            save_state(state_file, state)
            done += 1
            temp_in_a_row = 0
            log(f"[{number}/{len(todo)}] {letter.email}: отправлено")
    finally:
        sender.close()

    sent_total = sum(1 for entry in state.values() if entry["status"] == SENT)
    log(f"Готово. Отправлено в этом запуске: {done}; всего по журналу {state_file}: {sent_total}.")
    return code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Отправка писем из таблицы через mail.ru (SMTP)")
    parser.add_argument("letters", help="CSV с колонками «Почта», «Тема», «Текст»")
    parser.add_argument("--send", action="store_true",
                        help="реально отправить (без этого ключа - только проверка таблицы)")
    parser.add_argument("--limit", type=int, default=20,
                        help="сколько писем отправить за запуск (по умолчанию 20; 0 - все)")
    parser.add_argument("--pause", type=float, default=30.0, help="пауза между письмами, с (по умолчанию 30)")
    parser.add_argument("--from-name", default="",
                        help="имя отправителя в поле «От» (по умолчанию MAIL_FROM_NAME из local.env)")
    parser.add_argument("--port", type=int, default=SMTP_PORT, choices=(465, 587),
                        help="порт smtp.mail.ru: 465 (SSL, по умолчанию) или 587 (STARTTLS)")
    parser.add_argument("--daily-limit", type=int, default=40,
                        help="не больше писем за последние 24 часа по всем таблицам letters*.sent.json в папке "
                             "(по умолчанию 40; для нового ящика правила рассылки советуют 30-50; 0 - без лимита)")
    parser.add_argument("--stoplist", default=None,
                        help="файл стоп-листа: адрес или @домен в строке (по умолчанию stoplist.txt рядом с таблицей, "
                             "если он есть)")
    parser.add_argument("--env", default=ENV_FILE,
                        help="файл с MAILRU_USER и MAILRU_APP_PASSWORD (по умолчанию local.env рядом с mailer.py)")
    args = parser.parse_args(argv)
    try:
        load_env_file(args.env)
    except (OSError, ValueError) as exc:
        print(f"Ошибка: не удалось прочитать {args.env}: {getattr(exc, 'strerror', None) or exc}")
        return 1
    stop_path = args.stoplist or os.path.join(os.path.dirname(os.path.abspath(args.letters)), "stoplist.txt")
    try:
        stoplist = read_stoplist(stop_path)
    except FileNotFoundError:
        if args.stoplist:
            print(f"Ошибка: нет файла стоп-листа {args.stoplist}")
            return 1
        stoplist = frozenset()                  # файла нет - стоп-лист пока пуст
    except (OSError, ValueError) as exc:
        print(f"Ошибка: не удалось прочитать стоп-лист {stop_path}: {getattr(exc, 'strerror', None) or exc}")
        return 1
    try:
        return run(args.letters, args.send, args.limit, args.pause,
                   os.environ.get("MAILRU_USER", ""), os.environ.get("MAILRU_APP_PASSWORD", ""),
                   args.from_name or os.environ.get("MAIL_FROM_NAME", ""), args.port,
                   stoplist=stoplist, daily_limit=args.daily_limit, unsubscribe=True)
    except KeyboardInterrupt:
        print("Остановлено вами. Отправленное записано в журнал.")
        return 4


if __name__ == "__main__":
    sys.exit(main())
