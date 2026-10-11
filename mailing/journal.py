"""Журнал отправки <таблица>.sent.json: кому что отправлено, по нему же считаются дневной лимит и этапы 2-3.

Каждая запись помнит, с какого ящика ушло письмо (поле mailbox: номер ящика «1», «2»…). Записи, сделанные до
подключения второго ящика, поля не имеют: они ушли с единственного тогда ящика «1»."""
import json
import os
import re
from datetime import date, datetime, timedelta, timezone

from .config import MSK


SENT = "sent"             # письмо принято сервером
REJECTED = "rejected"     # сервер отказал по адресу получателя (5xx): повторно не шлём
DEFAULT_MAILBOX = "1"     # ящик записей без поля mailbox


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


def entry_mailbox(entry: dict) -> str:
    return str(entry.get("mailbox") or DEFAULT_MAILBOX)


def _sent_times(directory: str, mailbox=None):
    """Моменты принятых сервером писем по журналам всех таблиц letters*.sent.json в папке (от ящика mailbox, если задан)."""
    try:
        names = os.listdir(directory)
    except OSError:                                   # папки данных ещё нет: писем не было
        return
    for name in names:
        if not (name.startswith("letters") and name.endswith(".sent.json")):
            continue
        for entry in load_state(os.path.join(directory, name)).values():
            if entry.get("status") != SENT or (mailbox is not None and entry_mailbox(entry) != str(mailbox)):
                continue
            try:
                yield datetime.fromisoformat(entry["time"])
            except (KeyError, ValueError):
                continue


def sent_last_24h(directory: str, now=None, mailbox=None) -> int:
    """Сколько писем ушло за последние 24 часа по журналам всех таблиц letters*.sent.json в папке.
    mailbox - считать только письма этого ящика; без него считаются все."""
    now = now or datetime.now(timezone.utc)
    return sum(1 for when in _sent_times(directory, mailbox) if timedelta(0) <= now - when < timedelta(hours=24))


def sent_on_day(directory: str, day: date, mailbox=None) -> int:
    """Сколько писем ящика ушло в календарный день day (по Москве): на этом счёте стоит прогрев."""
    return sum(1 for when in _sent_times(directory, mailbox) if when.astimezone(MSK).date() == day)


def first_sent_day(directory: str, mailbox=None):
    """День (по Москве) самого раннего письма ящика по журналам папки или None, если с него ещё ничего не уходило."""
    days = [when.astimezone(MSK).date() for when in _sent_times(directory, mailbox)]
    return min(days) if days else None


def stage_of(letters_path: str) -> int:
    """Номер этапа по имени таблицы letters_2.csv -> 2; 0, если имя другое (letters.csv, letters_test.csv)."""
    found = re.fullmatch(r"letters_(\d+)\.csv", os.path.basename(letters_path))
    return int(found.group(1)) if found else 0


def previous_mailboxes(directory: str, stage: int) -> dict:
    """Адрес (маленькими буквами) -> ящик, с которого ушло его письмо прошлого этапа (последнее по номеру этапа).
    Дожим и закрывающее идут с того же ящика, что и первое письмо: цепочка не должна менять отправителя."""
    result = {}
    for earlier in range(1, stage):
        for address, entry in load_state(os.path.join(directory, f"letters_{earlier}.sent.json")).items():
            if entry.get("status") == SENT:
                result[address.lower()] = entry_mailbox(entry)
    return result


def sent_overview(directory: str) -> dict:
    """Адрес (маленькими буквами) -> {"stage", "status", "time"} по журналам первого, второго и третьего письма
    в папке данных: самое позднее принятое письмо; если принятых нет, отказ по адресу. Для таблицы на сайте."""
    overview = {}
    for stage in (1, 2, 3):
        entries = load_state(os.path.join(directory, f"letters_{stage}.sent.json"))
        for address, entry in entries.items():
            status = entry.get("status")
            if status not in (SENT, REJECTED):
                continue
            current = overview.get(address)
            if status == SENT or current is None:
                overview[address] = {"stage": stage, "status": status, "time": entry.get("time", "")}
    return overview


def sent_stages(directory: str) -> dict:
    """Адрес (маленькими буквами) -> {этап: время} по журналам первого, второго и третьего письма в папке данных:
    только письма, принятые сервером. Отказ по адресу сюда не входит. Для колонок «Первое письмо», «Дожим»,
    «Закрывающее» в таблице выгрузки."""
    stages = {}
    for stage in (1, 2, 3):
        for address, entry in load_state(os.path.join(directory, f"letters_{stage}.sent.json")).items():
            if entry.get("status") == SENT:
                stages.setdefault(address, {})[stage] = entry.get("time", "")
    return stages


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_entry(status: str, subject: str, meta: dict, note: str = "", mailbox: str = "") -> dict:
    """Запись журнала. Тема и лишние колонки таблицы (группа, источник адреса) нужны для следующих писем цепочки,
    ящик - чтобы считать лимит и прогрев по каждому ящику отдельно и слать дожим с того же ящика."""
    entry = {"status": status, "time": now_iso(), "note": note, "subject": subject}
    if mailbox:
        entry["mailbox"] = str(mailbox)
    if meta:
        entry["meta"] = meta
    return entry
