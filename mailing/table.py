"""Таблица писем (CSV: Почта, Тема, Текст + любые лишние колонки)."""
import csv
import io
from dataclasses import dataclass, field

from .config import check_email


FIELDS = ["Почта", "Тема", "Текст"]
_ALIASES = {"почта": 0, "email": 0, "e-mail": 0, "to": 0,
            "тема": 1, "subject": 1,
            "текст": 2, "body": 2}
MAX_SUBJECT = 200
MAX_BODY = 20_000


class LettersError(Exception):
    """Таблицу нельзя прочитать (нет файла или нужных колонок)."""


@dataclass
class Letter:
    email: str
    subject: str
    body: str
    meta: dict = field(default_factory=dict)      # прочие колонки таблицы: «Группа», «Источник адреса» и т. д.


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
