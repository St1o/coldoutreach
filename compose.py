"""Сборка писем рассылки: таблица с /leads + тексты агентов (.md) -> letters_<этап>.csv для mailer.py.

Агенты рассылки пишут шаблоны с подстановками ({{группа_словами}}, {{боль_1}}, {{боль_2}}, {{ссылка_на_опрос}},
{{подпись}}) и блоки боли по группам видов деятельности. Адресов и ФИО они не видят. Эта программа работает на
вашем компьютере и подставляет значения:

    * группа получателя - по первым двум знакам основного ОКВЭД (таблица групп в файле сегмента, раздел 4);
      группа 12 и неизвестный ОКВЭД - запасной вариант;
    * ФИО директора и название компании в письма не попадают: из таблицы читаются только почта, ОКВЭД и
      источник почты;
    * письма идут только на адреса из ЕГРЮЛ (не из контактов Checko), без повторов и без стоп-листа;
    * этап 1 - первое письмо; этап 2 (дожим) - тем, кому первое отправлено 3 рабочих дня назад и больше;
      этап 3 (закрывающее) - тем, кому дожим отправлен и с первого письма прошло 7 дней. Дожим и закрывающее
      не получают те, чей адрес есть в файле ответов опроса. Не больше трёх писем на адрес;
    * нет ссылки на форму или подписи - письма не собираются.

Все файлы партии лежат в одной папке рядом с таблицей (она не должна быть в папке проекта агентов):
    leads_<дата>.csv          таблица с /leads
    stoplist.txt              адреса и @домены, которым писать нельзя
    responses.csv             выгрузка ответов опроса (адреса ищутся по всему файлу)
    letters_1.csv, letters_2.csv, letters_3.csv     то, что собирает эта программа
    letters_1.sent.json, ...  журналы отправки (их ведёт mailer.py; по ним считаются этапы 2 и 3)

Запуск:
    python compose.py leads_2026-10-09.csv --letters <письма>.md --segment <сегмент>.md --preview
    python compose.py leads_2026-10-09.csv --letters ... --segment ... --limit 150
    python compose.py leads_2026-10-09.csv --letters ... --segment ... --stage 2
    python compose.py leads_2026-10-09.csv --letters ... --segment ... --test-to me@mail.ru,me2@gmail.com
Ссылку на форму, подпись и пути к файлам агентов можно один раз вписать в local.env (рядом с mailer.py):
SURVEY_LINK=..., MAIL_SIGNATURE=..., OUTREACH_LETTERS=..., OUTREACH_SEGMENT=...; тогда --letters, --segment, --link
и --signature не нужны. Параметры командной строки главнее local.env.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import mailer

OUT_FIELDS = ["Почта", "Тема", "Текст", "Группа", "Источник адреса"]
LEAD_COLUMNS = {"email": "Почта", "okved": "ОКВЭД (код)", "source": "Источник почты"}
SOURCE_REGISTER = "ЕГРЮЛ"
FALLBACK_ID = "12"
FIRST_ID, MIN_BUSINESS_DAYS, MIN_DAYS_TO_LAST = 1, 3, 7       # дожим через 3 рабочих дня, закрывающее через 7 дней
KNOWN_PLACEHOLDERS = {"группа_словами", "боль_1", "боль_2", "ссылка_на_опрос", "подпись"}
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_EMPTY_WORDS_CLAUSE = re.compile(r",\s*вид деятельности\s*[—–-]\s*\{\{\s*группа_словами\s*\}\}")
_LINK = re.compile(r"https?://\S+|(?:docs\.google\.com|forms\.gle)/\S*")
_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")


class ComposeError(Exception):
    """Письма собрать нельзя; текст понятен пользователю."""


@dataclass
class Group:
    id: str
    name: str
    classes: frozenset          # первые два знака ОКВЭД; у запасной группы пусто

    @property
    def words(self) -> str:
        """{{группа_словами}}: название группы маленькими буквами; у запасной группы пусто."""
        return "" if self.id == FALLBACK_ID else self.name[:1].lower() + self.name[1:]


@dataclass
class Texts:
    subjects: list                                    # [(текст, рекомендована)]
    templates: dict                                   # этап -> текст шаблона
    pain1: dict = field(default_factory=dict)         # ключ группы -> блок боли письма 1
    pain2: dict = field(default_factory=dict)
    fallback1: str = ""
    fallback2: str = ""


# --- разбор файлов агентов -------------------------------------------------------------------------------

def _key(name: str) -> str:
    return re.sub(r"\(.*?\)", "", name).split(":")[0].strip().casefold()


def _sections(text: str):
    """Файл -> [(заголовок «## …» без решёток, строки до следующего «## »)]."""
    result, heading, lines = [], None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if heading is not None:
                result.append((heading, lines))
            heading, lines = line[3:].strip(), []
        elif heading is not None:
            lines.append(line)
    if heading is not None:
        result.append((heading, lines))
    return result


def _fenced(lines):
    inside, block = False, []
    for line in lines:
        if line.strip().startswith("```"):
            if inside:
                return "\n".join(block).strip("\n")
            inside = True
        elif inside:
            block.append(line)
    return ""


def parse_segment(text: str) -> list:
    """Таблица групп (раздел «4. …» файла сегмента) -> [Group]. Запасная группа 12 есть всегда."""
    groups, section = [], None
    for heading, lines in _sections(text):
        if re.match(r"4\.", heading):
            section = lines
    if section is None:
        raise ComposeError("в файле сегмента нет раздела «4. …» с таблицей групп видов деятельности")
    for line in section:
        if not line.lstrip().startswith("|"):
            continue
        cell = line.strip().strip("|").split("|", 1)[0].strip()
        found = re.match(r"^(\d+[а-яa-z]?)\.\s+(.+)$", cell)
        if not found:
            continue
        group_id, rest = found.groups()
        with_classes = re.match(r"^(.+?)\s*\(([\d\s,–—-]+)\)\s*$", rest)
        if group_id == FALLBACK_ID or not with_classes:
            if group_id == FALLBACK_ID:
                groups.append(Group(FALLBACK_ID, rest.split(":")[0].strip(), frozenset()))
            continue
        classes = set()
        for token in with_classes.group(2).split(","):
            token = token.strip()
            if re.match(r"^\d+\s*[–—-]\s*\d+$", token):
                low, high = (int(part) for part in re.split(r"\s*[–—-]\s*", token))
                classes.update(range(low, high + 1))
            elif token.isdigit():
                classes.add(int(token))
        groups.append(Group(group_id, with_classes.group(1).strip(), frozenset(classes)))
    if not any(group.id == FALLBACK_ID for group in groups):
        groups.append(Group(FALLBACK_ID, "Запасной", frozenset()))
    if len(groups) < 2:
        raise ComposeError("в таблице групп файла сегмента не нашлось ни одной группы с классами ОКВЭД")
    return groups


def parse_letters(text: str) -> Texts:
    """Файл писем -> Texts: темы, шаблоны трёх писем, блоки боли по группам, запасные блоки."""
    subjects, templates, texts = [], {}, Texts([], {})
    for heading, lines in _sections(text):
        if heading.startswith("Темы"):
            for line in lines:
                found = re.match(r"^\s*\d+\.\s+(.*\S)\s*$", line)
                if found:
                    raw = found.group(1)
                    subjects.append((re.sub(r"\s*\(рекомендую\)\s*", "", raw).strip(), "(рекомендую)" in raw))
        elif re.match(r"Письмо 1\b", heading):
            templates[1] = _fenced(lines)
        elif heading.startswith("Дожим"):
            templates[2] = _fenced(lines)
            for line in lines:
                found = re.match(r"^\*([^*\n]+?):\*\s*(.+?)\s*$", line)
                if found:
                    _store(texts, 2, found.group(1), found.group(2))
        elif heading.startswith("Закрывающее"):
            templates[3] = _fenced(lines)
        elif heading.startswith("Блоки боли для письма 1"):
            name, parts = None, []
            for line in lines + ["**"]:
                found = re.match(r"^\*\*(.+?)\*\*\s*$", line)
                if found or line == "**":
                    if name is not None and parts:
                        _store(texts, 1, name, " ".join(parts))
                    name, parts = (found.group(1) if found else None), []
                elif name is not None and line.strip():
                    parts.append(line.strip())
    texts.subjects, texts.templates = subjects, templates
    return texts


def _store(texts: Texts, stage: int, name: str, block: str) -> None:
    key = _key(name)
    if key.startswith("запасной"):
        if stage == 1:
            texts.fallback1 = block
        else:
            texts.fallback2 = block
    else:
        (texts.pain1 if stage == 1 else texts.pain2)[key] = block


def check_texts(texts: Texts, groups: list) -> None:
    known = {_key(group.name) for group in groups}
    for key in list(texts.pain1) + list(texts.pain2):
        if key not in known:
            raise ComposeError(f"блок боли «{key}» из файла писем не найден в таблице групп файла сегмента: "
                               "названия должны совпадать")
    for stage in (1, 2, 3):
        if not texts.templates.get(stage):
            raise ComposeError(f"в файле писем нет шаблона письма {stage} (блок ``` под заголовком "
                               "«Письмо 1», «Дожим», «Закрывающее»)")
    if not texts.subjects:
        raise ComposeError("в файле писем нет тем (нумерованный список под заголовком «Темы»)")
    if not texts.fallback1 or not texts.fallback2:
        raise ComposeError("в файле писем нет запасных блоков боли («Запасной вариант» для письма 1 и для дожима)")


# --- значения и подстановка ------------------------------------------------------------------------------

def check_link(link: str) -> str:
    link = (link or "").strip()
    if not link:
        raise ComposeError("не задана ссылка на опрос (--link или SURVEY_LINK в local.env): письма без неё не отправляем")
    if "forms.gle" in link:
        raise ComposeError("ссылка forms.gle - сокращённая, это спам-признак: нужна полная docs.google.com/forms/…")
    if not link.startswith("https://docs.google.com/forms/") or re.search(r"\s", link):
        raise ComposeError("ссылка на опрос должна быть полной и начинаться с https://docs.google.com/forms/")
    if re.search(r"/edit\b", link):
        raise ComposeError("это ссылка редактирования формы; нужна ссылка для заполнения (…/viewform)")
    return link


def check_signature(signature: str) -> str:
    signature = (signature or "").replace("\\n", "\n").strip()
    if not signature or "заполнить" in signature.casefold() or "{{" in signature:
        raise ComposeError("не задана подпись (--signature или MAIL_SIGNATURE в local.env): письма без неё не отправляем")
    return signature


def pick_subject(texts: Texts, number: int = 0) -> str:
    if number:
        if not 1 <= number <= len(texts.subjects):
            raise ComposeError(f"тем в файле писем {len(texts.subjects)}, а запрошена тема {number}")
        return texts.subjects[number - 1][0]
    for subject, recommended in texts.subjects:
        if recommended:
            return subject
    return texts.subjects[0][0]


def fill(template: str, values: dict) -> str:
    if not values.get("группа_словами"):
        template = _EMPTY_WORDS_CLAUSE.sub("", template)

    def put(match):
        name = match.group(1)
        if name not in KNOWN_PLACEHOLDERS:
            raise ComposeError(f"в шаблоне неизвестная подстановка {{{{{name}}}}}")
        value = values.get(name, "")
        if not value:
            raise ComposeError(f"подстановка {{{{{name}}}}} пустая и запасного варианта для неё нет")
        return value

    text = _PLACEHOLDER.sub(put, template)
    if "{{" in text or "}}" in text:
        raise ComposeError("в письме остались незаполненные подстановки {{…}}")
    links = _LINK.findall(text)
    if len(links) != 1 or "docs.google.com/forms/" not in links[0]:
        raise ComposeError(f"в письме должна быть ровно одна ссылка на docs.google.com/forms/, найдено: {len(links)}")
    return text


def okved_class(code: str):
    """«62.01», «01.11», «1.11» (ведущий ноль потерян в Excel), «1,11» -> 62, 1, 1, 1; не разобрать -> None."""
    found = re.match(r"^\s*(\d{1,2})(?:[.,]|\s*$)", code or "")
    return int(found.group(1)) if found else None


class Composer:
    def __init__(self, groups: list, texts: Texts, link: str, signature: str, include_unwritten: bool = False):
        self.groups = {group.id: group for group in groups}
        self.by_class = {}
        for group in groups:
            for cls in group.classes:
                self.by_class.setdefault(cls, group.id)
        self.texts, self.link, self.signature = texts, check_link(link), check_signature(signature)
        self.include_unwritten = include_unwritten

    def group_for(self, okved: str) -> str:
        return self.by_class.get(okved_class(okved), FALLBACK_ID)

    def is_written(self, group_id: str) -> bool:
        """Блок боли для письма 1 написан (или это запасная группа)."""
        return group_id == FALLBACK_ID or _key(self.groups[group_id].name) in self.texts.pain1

    def letter_text(self, stage: int, group_id: str) -> str:
        group = self.groups[group_id]
        key = _key(group.name)
        pain1 = self.texts.pain1.get(key) if group_id != FALLBACK_ID else None
        pain2 = self.texts.pain2.get(key) if group_id != FALLBACK_ID else None
        return fill(self.texts.templates[stage], {
            "группа_словами": group.words,
            "боль_1": pain1 or self.texts.fallback1,
            "боль_2": pain2 or self.texts.fallback2,
            "ссылка_на_опрос": self.link,
            "подпись": self.signature,
        })


# --- вход: таблица, стоп-лист, ответы, журналы ------------------------------------------------------------

def read_leads(path: str) -> list:
    """Таблица /leads -> [{"email", "okved", "source"}]; ФИО и названия компаний не читаются."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except OSError as exc:
        raise ComposeError(f"не удалось открыть {path}: {exc.strerror or exc}") from exc
    header = text.split("\n", 1)[0]
    delimiter = max(";,\t", key=header.count) if any(d in header for d in ";,\t") else ";"
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        raise ComposeError("таблица пустая")
    names = [name.strip() for name in rows[0]]
    missing = [column for column in LEAD_COLUMNS.values() if column not in names]
    if missing:
        raise ComposeError("в таблице нет колонок: " + ", ".join(missing) + ". Выгрузите таблицу заново на странице "
                           "/leads: в новой выгрузке есть ОКВЭД и источник почты")
    index = {key: names.index(column) for key, column in LEAD_COLUMNS.items()}
    leads = []
    for row in rows[1:]:
        if any(cell.strip() for cell in row):
            leads.append({key: (row[i].strip() if i < len(row) else "") for key, i in index.items()})
    return leads


def read_responded(path: str) -> frozenset:
    """Выгрузка ответов опроса -> все адреса почты в файле (искать по всему файлу, колонка может быть любой)."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return frozenset(address.lower() for address in _EMAIL.findall(fh.read()))
    except OSError as exc:
        raise ComposeError(f"не удалось открыть файл ответов {path}: {exc.strerror or exc}") from exc


def journal_path(folder: str, stage: int) -> str:
    return os.path.join(folder, f"letters_{stage}.sent.json")


def _entry_date(entry: dict):
    try:
        return datetime.fromisoformat(entry["time"]).astimezone().date()
    except (KeyError, ValueError):
        return None


def business_days_since(start: date, today: date) -> int:
    count, day = 0, start
    while day < today:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return count


# --- сборка ----------------------------------------------------------------------------------------------

@dataclass
class Result:
    letters: list = field(default_factory=list)       # [(почта, тема, текст, группа, источник)]
    skipped: Counter = field(default_factory=Counter)
    unwritten: Counter = field(default_factory=Counter)     # группа -> сколько писем не собрано


def compose_first(composer: Composer, leads: list, subject: str, folder: str, stoplist, responded,
                  limit: int = 0) -> Result:
    result = Result()
    handled = set()
    for stage in (1, 2, 3):
        handled.update(mailer.load_state(journal_path(folder, stage)))
    seen = set()
    for lead in leads:
        address = lead["email"].lower()
        if not mailer.check_email(lead["email"]):
            result.skipped["некорректный адрес"] += 1
        elif lead["source"] != SOURCE_REGISTER:
            result.skipped["почта не из ЕГРЮЛ (контакты Checko или источник неизвестен)"] += 1
        elif address in seen:
            result.skipped["повтор адреса (одна цепочка на адрес)"] += 1
        elif address in handled:
            result.skipped["уже получал письма из этой рассылки"] += 1
        elif mailer.is_stopped(address, stoplist):
            result.skipped["в стоп-листе"] += 1
        elif address in responded:
            result.skipped["уже заполнил опрос"] += 1
        else:
            group_id = composer.group_for(lead["okved"])
            if not composer.is_written(group_id) and not composer.include_unwritten:
                result.unwritten[group_id] += 1
                seen.add(address)
                continue
            seen.add(address)
            result.letters.append((lead["email"], subject, composer.letter_text(1, group_id), group_id,
                                   lead["source"]))
    if limit:
        result.letters = result.letters[:limit]
    return result


def compose_next(composer: Composer, stage: int, folder: str, stoplist, responded, today: date,
                 min_days: int = 0) -> Result:
    """Этап 2 или 3 по журналам предыдущих этапов."""
    result = Result()
    previous = mailer.load_state(journal_path(folder, stage - 1))
    first = mailer.load_state(journal_path(folder, 1))
    later = set()
    for number in range(stage, 4):
        later.update(mailer.load_state(journal_path(folder, number)))
    for address, entry in previous.items():
        if entry.get("status") != mailer.SENT:
            continue
        if address in later:
            result.skipped[f"письмо этапа {stage} уже отправлялось"] += 1
            continue
        started = _entry_date(first.get(address, entry))
        if started is None:
            result.skipped["в журнале нет даты первого письма"] += 1
            continue
        if stage == 2:
            due = business_days_since(started, today) >= (min_days or MIN_BUSINESS_DAYS)
        else:
            due = (today - started).days >= (min_days or MIN_DAYS_TO_LAST)
        if not due:
            result.skipped["срок ещё не подошёл"] += 1
            continue
        if mailer.is_stopped(address, stoplist):
            result.skipped["в стоп-листе"] += 1
            continue
        if address in responded:
            result.skipped["заполнил опрос"] += 1
            continue
        meta, subject = entry.get("meta", {}), entry.get("subject", "")
        group_id = meta.get("Группа", "")
        if group_id not in composer.groups or not subject:
            result.skipped["в журнале нет группы или темы (письмо отправлено не этой программой)"] += 1
            continue
        result.letters.append((address, subject, composer.letter_text(stage, group_id), group_id,
                               meta.get("Источник адреса", "")))
    return result


def compose_test(composer: Composer, subject: str, stage: int, addresses: list) -> Result:
    """Пробные письма: по одному на адрес, группы по кругу (сначала написанные, в конце запасная)."""
    written = [group_id for group_id in composer.groups if group_id != FALLBACK_ID and composer.is_written(group_id)]
    cycle = written + [FALLBACK_ID]
    result = Result()
    for number, address in enumerate(addresses):
        group_id = cycle[number % len(cycle)]
        result.letters.append((address, subject, composer.letter_text(stage, group_id), group_id, "тест"))
    return result


def write_letters(path: str, letters: list) -> None:
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(OUT_FIELDS)
        writer.writerows(letters)
    os.replace(tmp, path)


def project_root(letters_path: str):
    """Папка проекта агентов: ближайший родитель файла писем, где лежит CLAUDE.md."""
    folder = os.path.dirname(os.path.abspath(letters_path))
    for _ in range(8):
        if os.path.exists(os.path.join(folder, "CLAUDE.md")):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return None


def inside(folder: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(folder), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:                      # разные диски
        return False


def main(argv=None, today=None, log=print) -> int:
    parser = argparse.ArgumentParser(description="Сборка писем рассылки из таблицы /leads и текстов агентов")
    parser.add_argument("leads", help="таблица leads_<дата>.csv; рядом с ней лежат и появляются остальные файлы партии")
    parser.add_argument("--letters", default="", help="файл писем агентов (02-Write/output/….md) или OUTREACH_LETTERS")
    parser.add_argument("--segment", default="",
                        help="файл сегмента (01-Segment/output/….md): таблица групп ОКВЭД, или OUTREACH_SEGMENT")
    parser.add_argument("--stage", type=int, choices=(1, 2, 3), default=1,
                        help="1 - первое письмо, 2 - дожим, 3 - закрывающее (по умолчанию 1)")
    parser.add_argument("--link", default="", help="полная ссылка на опрос docs.google.com/forms/… (или SURVEY_LINK)")
    parser.add_argument("--signature", default="", help="подпись: имя и должность (или MAIL_SIGNATURE)")
    parser.add_argument("--subject", type=int, default=0, help="номер темы из файла писем (по умолчанию рекомендованная)")
    parser.add_argument("--limit", type=int, default=0, help="сколько писем в партии (этап 1; 0 - все подходящие)")
    parser.add_argument("--min-days", type=int, default=0,
                        help="этап 2: рабочих дней после первого письма (по умолчанию 3); "
                             "этап 3: дней после первого письма (по умолчанию 7)")
    parser.add_argument("--stoplist", default=None, help="стоп-лист (по умолчанию stoplist.txt рядом с таблицей)")
    parser.add_argument("--responded", default=None,
                        help="выгрузка ответов опроса (по умолчанию responses.csv рядом с таблицей, если он есть)")
    parser.add_argument("--include-unwritten", action="store_true",
                        help="группам, для которых блок боли ещё не написан, подставить запасной вариант "
                             "(по умолчанию такие письма не собираются)")
    parser.add_argument("--preview", action="store_true", help="показать по одному примеру письма на группу")
    parser.add_argument("--test-to", default="",
                        help="адреса через запятую: собрать letters_test.csv с пробными письмами им (таблица не читается)")
    parser.add_argument("--env", default=mailer.ENV_FILE, help="файл local.env")
    args = parser.parse_args(argv)

    try:
        mailer.load_env_file(args.env)
        letters_path = args.letters or os.environ.get("OUTREACH_LETTERS", "")
        segment_path = args.segment or os.environ.get("OUTREACH_SEGMENT", "")
        if not letters_path or not segment_path:
            raise ComposeError("не указаны файлы агентов: --letters и --segment (или OUTREACH_LETTERS и OUTREACH_SEGMENT "
                               "в local.env)")
        folder = os.path.dirname(os.path.abspath(args.leads))
        root = project_root(letters_path)
        if root and inside(folder, root):
            raise ComposeError(f"таблица с адресами лежит в папке проекта агентов ({root}). Адреса в проект не кладём: "
                               "перенесите таблицу и эту работу в отдельную папку вне проекта")
        with open(letters_path, encoding="utf-8-sig") as fh:
            texts = parse_letters(fh.read())
        with open(segment_path, encoding="utf-8-sig") as fh:
            groups = parse_segment(fh.read())
        check_texts(texts, groups)
        composer = Composer(groups, texts, args.link or os.environ.get("SURVEY_LINK", ""),
                            args.signature or os.environ.get("MAIL_SIGNATURE", ""), args.include_unwritten)
        subject = pick_subject(texts, args.subject)

        stop_path = args.stoplist or os.path.join(folder, "stoplist.txt")
        resp_path = args.responded or os.path.join(folder, "responses.csv")
        stoplist = mailer.read_stoplist(stop_path) if os.path.exists(stop_path) else frozenset()
        if args.stoplist and not os.path.exists(stop_path):
            raise ComposeError(f"нет файла стоп-листа {stop_path}")
        responded = read_responded(resp_path) if (args.responded or os.path.exists(resp_path)) else frozenset()
        when = today or date.today()

        if args.test_to:
            addresses = [a.strip() for a in args.test_to.split(",") if a.strip()]
            bad = [a for a in addresses if not mailer.check_email(a)]
            if not addresses or bad:
                raise ComposeError("--test-to: нужны корректные адреса через запятую (свои ящики)")
            result = compose_test(composer, subject, args.stage, addresses)
            out = os.path.join(folder, "letters_test.csv")
        elif args.stage == 1:
            result = compose_first(composer, read_leads(args.leads), subject, folder, stoplist, responded, args.limit)
            out = os.path.join(folder, "letters_1.csv")
        else:
            result = compose_next(composer, args.stage, folder, stoplist, responded, when, args.min_days)
            out = os.path.join(folder, f"letters_{args.stage}.csv")
    except (ComposeError, OSError, ValueError) as exc:
        log(f"Ошибка: {getattr(exc, 'strerror', None) or exc}")
        return 1

    names = {group_id: group.name for group_id, group in composer.groups.items()}
    log(f"Этап {args.stage}: тема «{subject}»; стоп-лист: {len(stoplist)}; адресов в ответах опроса: {len(responded)}.")
    for reason, number in result.skipped.most_common():
        log(f"  пропущено: {reason} - {number}")
    for group_id, number in sorted(result.unwritten.items()):
        log(f"  не собрано: для группы {group_id} «{names[group_id]}» ещё нет блока боли - {number} "
            "(напишите его или добавьте --include-unwritten)")
    if not result.letters:
        log("Писать некому: ни одного письма не собрано, файл не создан.")
        return 0
    per_group = Counter(letter[3] for letter in result.letters)
    log(f"Собрано писем: {len(result.letters)}. По группам: "
        + "; ".join(f"{gid} {names[gid]} - {number}" for gid, number in sorted(per_group.items(), key=lambda i: i[0])))
    if args.preview:
        shown = set()
        for letter in result.letters:
            if letter[3] not in shown:
                shown.add(letter[3])
                log(f"\n----- пример, группа {letter[3]} «{names[letter[3]]}», тема: {letter[1]} -----\n{letter[2]}")
        log("")
    write_letters(out, result.letters)
    log(f"Файл: {out}")
    check = f'python mailer.py "{out}"'
    log(f"Дальше: проверка {check} , затем {check} --send --limit 5")
    return 0


if __name__ == "__main__":
    sys.exit(main())
