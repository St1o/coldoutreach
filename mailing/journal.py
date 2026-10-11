"""Журнал отправки <таблица>.sent.json: кому что отправлено, по нему же считаются дневной лимит и этапы 2-3."""
import json
import os
from datetime import datetime, timedelta, timezone


SENT = "sent"             # письмо принято сервером
REJECTED = "rejected"     # сервер отказал по адресу получателя (5xx): повторно не шлём


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


def sent_last_24h(directory: str, now=None) -> int:
    """Сколько писем ушло за последние 24 часа по журналам всех таблиц letters*.sent.json в папке."""
    now = now or datetime.now(timezone.utc)
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


def make_entry(status: str, subject: str, meta: dict, note: str = "") -> dict:
    """Запись журнала. Тема и лишние колонки таблицы (группа, источник адреса) нужны для следующих писем цепочки."""
    entry = {"status": status, "time": now_iso(), "note": note, "subject": subject}
    if meta:
        entry["meta"] = meta
    return entry
