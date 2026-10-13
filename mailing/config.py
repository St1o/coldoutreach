"""Настройки рассылки: local.env, settings.json (лимит с сайта), стоп-лист, проверка и скрытие адресов."""
import json
import os
import re
from datetime import date, datetime, timedelta, timezone


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))       # корень проекта, рядом с mailer.py
MSK = timezone(timedelta(hours=3))                                        # «сутки» прогрева считаем по Москве, как и в mail_service
ENV_FILE = os.path.join(ROOT, "local.env")
SETTINGS_FILE = os.path.join(ROOT, "settings.json")                       # пишет страница /mail
DEFAULT_DAILY_LIMIT = 40
SMTP_PORTS = (465, 587)                                                    # 465 - SSL сразу, 587 - STARTTLS (запасной путь)
IMAP_HOST, IMAP_PORT = "imap.mail.ru", 993                                 # копия письма в «Отправленные» (mailing/sentcopy.py)
DAILY_LIMIT_KEY = "daily_limit_per_mailbox"
DAILY_LIMIT_MIN, DAILY_LIMIT_MAX = 1, 50          # лимит с /mail: выше 50 не ставится; предел недели прогрева задан в mailboxes.py
_EMAIL_RE = re.compile(r"^[^@\s<>,;\"'()\[\]]+@[^@\s<>,;\"'()\[\]]+\.[^@\s<>,;\"'()\[\]]+$")


def check_email(address: str) -> bool:
    return bool(_EMAIL_RE.match(address))


_STOP_UNTIL = re.compile(r"(?:^|\s)до\s+(\S+)\s*$")           # «@yandex.ru @ya.ru до 2026-10-16» в конце строки
_STOP_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def read_stoplist(path: str, today: date = None) -> frozenset:
    """Стоп-лист: адреса и домены целиком (@example.ru). Можно по одному в строке, можно несколько в строке через запятую,
    точку с запятой или пробел; пустые строки и всё после # пропускаются.
    Срок: строка, оканчивающаяся на «до ГГГГ-ММ-ДД» (например «@yandex.ru до 2026-10-16»), действует по эту дату включительно
    (по Москве), со следующего дня её записи пропадают сами; файл при этом не меняется. Строки без срока действуют всегда.
    Непонятная дата после «до» - ValueError с номером строки: лучше остановиться, чем молча снять запрет или не понять срок."""
    today = today or datetime.now(MSK).date()
    entries = set()
    with open(path, encoding="utf-8-sig") as fh:
        for number, line in enumerate(fh, 1):
            text = line.split("#", 1)[0].lower()
            until = _STOP_UNTIL.search(text)
            if until:
                token = until.group(1)
                try:
                    last_day = date.fromisoformat(token) if _STOP_DAY.match(token) else None
                except ValueError:
                    last_day = None
                if last_day is None:
                    raise ValueError(f"в строке {number} после слова «до» нужна дата в виде ГГГГ-ММ-ДД "
                                     "(например «@yandex.ru до 2026-10-16»)")
                if today > last_day:
                    continue                                     # срок вышел: записи этой строки больше не действуют
                text = text[:until.start()]
            entries.update(part for part in re.split(r"[,;\s]+", text) if part)
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


def load_settings(path: str = "") -> dict:
    """Настройки, сохранённые на сайте (страница /mail). Нет файла или он испорчен - пустые настройки."""
    try:
        with open(path or SETTINGS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(values: dict, path: str = "") -> None:
    path = path or SETTINGS_FILE
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(values, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def parse_daily_limit(raw) -> int:
    """Лимит писем в сутки на один ящик: целое число от 1 до 50, иначе ValueError."""
    try:
        number = int(str(raw).strip())
    except ValueError:
        number = 0
    if not DAILY_LIMIT_MIN <= number <= DAILY_LIMIT_MAX:
        raise ValueError(f"Лимит должен быть целым числом от {DAILY_LIMIT_MIN} до {DAILY_LIMIT_MAX}.")
    return number


def resolve_daily_limit(cli_value=None, settings_path: str = "", environ=None):
    """(лимит, откуда): ключ --daily-limit, затем настройка с сайта, затем MAIL_DAILY_LIMIT из local.env, затем 40.
    Испорченное значение с сайта пропускается; испорченное в local.env - ValueError."""
    if cli_value is not None:
        return cli_value, "ключ --daily-limit"
    try:
        return parse_daily_limit(load_settings(settings_path).get(DAILY_LIMIT_KEY)), "сайт"
    except ValueError:
        pass
    raw = (environ if environ is not None else os.environ).get("MAIL_DAILY_LIMIT", "")
    if str(raw).strip():
        return int(raw), "local.env"
    return DEFAULT_DAILY_LIMIT, "по умолчанию"


def mask_email(address: str) -> str:
    """name@mail.ru -> n***@mail.ru: для журналов и отчётов, которые читают не только вы."""
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def redact(text: str) -> str:
    return re.sub(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+", lambda m: mask_email(m.group(0)), text)


def resolve_port(cli_value=None, environ=None) -> int:
    """Порт smtp.mail.ru: ключ --port, затем MAILRU_PORT из local.env, иначе 465. Допустимы 465 и 587; иное - ValueError."""
    raw = cli_value if cli_value is not None else (environ if environ is not None else os.environ).get("MAILRU_PORT", "")
    if raw in (None, ""):
        return SMTP_PORTS[0]
    try:
        number = int(str(raw).strip())
    except ValueError:
        number = 0
    if number not in SMTP_PORTS:
        raise ValueError("Порт SMTP: 465 или 587 (MAILRU_PORT в local.env или ключ --port).")
    return number


def resolve_sent_copy(environ=None) -> bool:
    """Сохранять ли копию отправленного письма в «Отправленные»: SAVE_SENT_COPY в local.env, по умолчанию да; 0 - выключить."""
    raw = (environ if environ is not None else os.environ).get("SAVE_SENT_COPY", "")
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "нет")


def resolve_imap(environ=None):
    """(хост, порт) IMAP mail.ru: MAILRU_IMAP_HOST и MAILRU_IMAP_PORT из local.env, иначе imap.mail.ru и 993.
    Неверное значение - ValueError с понятным текстом (отправку оно не останавливает: см. mailing/sentcopy.py)."""
    environ = environ if environ is not None else os.environ
    host = str(environ.get("MAILRU_IMAP_HOST", "")).strip() or IMAP_HOST
    if re.search(r"[\s/]", host):
        raise ValueError("MAILRU_IMAP_HOST в local.env должен быть именем сервера, например imap.mail.ru.")
    raw = str(environ.get("MAILRU_IMAP_PORT", "")).strip()
    try:
        port = int(raw) if raw else IMAP_PORT
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        raise ValueError("MAILRU_IMAP_PORT в local.env должен быть числом, например 993.")
    return host, port
