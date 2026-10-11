"""Сборка писем рассылки: таблица /leads + тексты агентов (.md) -> letters_<этап>.csv для mailer.py.
Порядок работы и все команды: LOCAL.md, раздел «Рассылка».

    python compose.py leads_<дата>.csv --limit 150 --preview        # этап 1, первое письмо
    python compose.py <папка данных> --limit 15 --preview           # то же по всем сохранённым таблицам leads_<дата>.csv
    python compose.py leads_<дата>.csv --stage 2 --preview          # дожим (3 рабочих дня после первого письма)
    python compose.py leads_<дата>.csv --stage 3                    # закрывающее (7 дней после первого)
    python compose.py leads_<дата>.csv --test-to me@mail.ru         # пробные письма на свои ящики

Вместо таблицы можно указать папку данных: тогда берутся все таблицы leads_<дата>.csv в ней, от старых дат к новым
(ключ --date оставляет одну дату), а письма собираются тем, кому из этих компаний ещё ничего не отправляли. Так
отправляют по уже собранной базе, без нового запроса к Checko (сайт может работать, но для сборки писем он не нужен).
Из таблицы читаются только почта, ОКВЭД и источник почты (ФИО и названия в письма не попадают). Группа - по первым
двум знакам ОКВЭД (таблица групп в файле сегмента), запасная группа 12 - для неизвестного. Только адреса из ЕГРЮЛ,
без повторов, стоп-листа и ответивших на опрос. Нет ссылки на форму (и подписи, если в шаблоне есть {{подпись}}),
остались {{…}} или больше одной ссылки - письма не собираются. Этапы 2 и 3 строятся по журналам отправки.
Ссылка, подпись и пути к файлам агентов берутся из local.env: SURVEY_LINK, MAIL_SIGNATURE, OUTREACH_LETTERS,
OUTREACH_SEGMENT (ключи командной строки главнее).

Варианты фраз (необязательно): раздел «## Варианты фраз» файла писем («### место» и строки «- вариант»); в шаблонах это
{{приветствие}}, {{призыв_1}} и т. п. Программа подставляет варианты по кругу (соседние письма разные), приветствие - по
часу сборки письма (пометки «утро:», «день:», «вечер:»), место «тема» раздаёт темы. Базовая проверка на повторы:
средняя разница между письмами одной группы не меньше --min-unique (по умолчанию 25%), иначе письма не собираются.

Карта ОКВЭД (необязательно): файл okved-map.md агента рядом с файлом сегмента (или --okved-map, OUTREACH_OKVED_MAP).
Агент решает по смыслу, к какой группе относится каждый код ОКВЭД (или «не писать»); решение по коду главнее таблицы
классов. Коды, по которым решения ещё нет, программа выписывает в okved_to_classify.md (коды и названия видов
деятельности, без адресов и названий компаний): этот файл передают агенту.
"""
import argparse
import csv
import io
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import random

from mailing import config, journal
from mailing.agentmd import (
    EXCLUDE_ID, FALLBACK_ID, SUBJECT_SLOT, TIME_TAGS, ComposeError, Texts, _key, check_texts, okved_digits,
    parse_form_rules, parse_letters, parse_okved_map, parse_segment)

OUT_FIELDS = ["Почта", "Тема", "Текст", "Группа", "Источник адреса"]
LEAD_COLUMNS = {"email": "Почта", "okved": "ОКВЭД (код)", "source": "Источник почты"}
NAME_COLUMN = "Название"                          # из него берётся только форма организации (ООО, МБУ…); название дальше не идёт
OKVED_NAME_COLUMN = "ОКВЭД (название)"            # нужна только для списка кодов агенту; в старых выгрузках её может не быть
OKVED_MAP_NAME, OKVED_REPORT_NAME = "okved-map.md", "okved_to_classify.md"
LEADS_TABLE = re.compile(r"leads_(\d{4}-\d{2}-\d{2})\.csv")        # таблица, которую сайт сохраняет в папку данных
SOURCE_REGISTER = "ЕГРЮЛ"
FIRST_ID, MIN_BUSINESS_DAYS, MIN_DAYS_TO_LAST = 1, 3, 7       # дожим через 3 рабочих дня, закрывающее через 7 дней
KNOWN_PLACEHOLDERS = {"группа_словами", "боль_1", "боль_2", "ссылка_на_опрос", "подпись"}
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_EMPTY_WORDS_CLAUSE = re.compile(r",\s*(?:вид деятельности|сфера деятельности|сфера)\s*[—–-]\s*\{\{\s*группа_словами\s*\}\}")
SUBJECT_WORDS = "группа_кратко"      # подстановка только в вариантах темы: «{{группа_кратко}}» -> «торговля»
# Бесплатные почты: на таком домене сидят разные люди и компании. На любом другом домене одна организация: одно письмо
# на домен (два одинаковых письма подряд на ящики одного домена похожи на автоматическую рассылку).
PUBLIC_DOMAINS = frozenset("""
    mail.ru bk.ru list.ru inbox.ru internet.ru yandex.ru ya.ru yandex.com yandex.by yandex.kz yandex.ua rambler.ru ro.ru
    autorambler.ru myrambler.ru lenta.ru gmail.com googlemail.com outlook.com hotmail.com live.com msn.com icloud.com
    me.com yahoo.com yahoo.ru ukr.net mail.ua tut.by qip.ru pochta.ru
""".split())
MIN_UNIQUE = 25.0           # средняя разница между письмами одной группы, % (--min-unique; 0 - не проверять)
_LINK = re.compile(r"https?://\S+|(?:docs\.google\.com|forms\.gle)/\S*")
_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")


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


def uses_placeholder(texts: Texts, name: str) -> bool:
    pieces = (list(texts.templates.values()) + list(texts.fallback_templates.values())
              + [text for items in texts.variants.values() for _, text in items])
    return any(match.group(1) == name for template in pieces for match in _PLACEHOLDER.finditer(template))


def check_signature(signature: str) -> str:
    signature = (signature or "").replace("\\n", "\n").strip()
    if not signature or "заполнить" in signature.casefold() or "{{" in signature:
        raise ComposeError("в шаблоне есть подстановка {{подпись}}, но подпись не задана (--signature или MAIL_SIGNATURE "
                           "в local.env): задайте её или уберите подстановку из шаблона")
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


def day_period(hour: int) -> str:
    """Час -> «утро» (5-10), «день» (11-16), «вечер» (с 17 и ночью): для приветствий с пометкой времени суток."""
    return "утро" if 5 <= hour < 11 else "день" if 11 <= hour < 17 else "вечер"


def check_variants(variants: dict) -> None:
    """Раздел «Варианты фраз» файла писем: у каждого места есть варианты на любое время суток, в вариантах только
    обычные подстановки ({{группа_словами}}, {{ссылка_на_опрос}}, {{подпись}}), вложенных мест нет."""
    for slot, items in variants.items():
        if slot in KNOWN_PLACEHOLDERS:
            raise ComposeError(f"в разделе вариантов место «{slot}» совпадает с обычной подстановкой: назовите иначе")
        if not items:
            raise ComposeError(f"в разделе вариантов у места «{slot}» нет ни одного варианта (строки вида «- текст»)")
        for period in TIME_TAGS:
            if not any(tag in ("", period) for tag, _ in items):
                raise ComposeError(f"у места «{slot}» нет варианта для времени суток «{period}»: добавьте вариант без пометки")
        allowed = {SUBJECT_WORDS} if slot == SUBJECT_SLOT else KNOWN_PLACEHOLDERS
        if slot == SUBJECT_SLOT and all(SUBJECT_WORDS in text for _, text in items):
            raise ComposeError("у места «тема» нужен хотя бы один вариант без {{группа_кратко}}: у запасной группы "
                               "названия нет")
        for _, text in items:
            for found in _PLACEHOLDER.finditer(text):
                if found.group(1) not in allowed:
                    raise ComposeError(f"в варианте места «{slot}» неизвестная подстановка {{{{{found.group(1)}}}}}: "
                                       + ("в теме можно только {{группа_кратко}}" if slot == SUBJECT_SLOT else
                                          "в вариантах можно только обычные подстановки, не другие места"))


def fill(template: str, values: dict, slots: dict = None) -> str:
    if slots:                               # места с вариантами {{приветствие}} и т. п. заменяются выбранным вариантом
        template = _PLACEHOLDER.sub(lambda m: slots.get(m.group(1).casefold(), m.group(0)), template)
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


# Сокращения форм организаций, которые программа узнаёт в начале названия (Checko пишет: ООО "ТЕККИН", МБУ МП "КЦМИ").
# Первое слово названия, которого здесь нет, формой не считается: оно может быть частью самого названия.
KNOWN_FORMS = frozenset("""
    ООО ОАО ЗАО ПАО НАО АО ИП КФХ ПК СПК ТОО
    МБУ МАУ МКУ МУП МУ ГБУ ГАУ ГКУ ГУП ГУ ФГБУ ФГАУ ФГКУ ФГУП ФБУ КГБУ КГАУ КГКУ ОГБУ БУ АУ КУ
    МБОУ МАОУ МКОУ МБДОУ МАДОУ МКДОУ ГБОУ ГАОУ ГБПОУ ГАПОУ ФГБОУ ФГАОУ ГБДОУ МБУК МБУДО МАУДО МБУЗ ГБУЗ КГБУЗ
    АНО АНОО НКО НП ТСЖ ТСН СНТ ГСК ЖСК ТОС РОО МОО ОО ЧУ ЧОУ НОУ НОЧУ ЧУДО АНПО
""".split())
OTHER_FORM = "другая"                              # название есть, но начинается не с известного сокращения
DEFAULT_FORMS = frozenset({"ООО"})                 # пока агент не решил иначе: сегмент «только ООО» (решение владельца 2026-10-07)
_FORM_WORD = re.compile(r"^[\s«\"“<(]*([А-ЯЁ]{2,8})(?![А-ЯЁа-яё])")
_FULL_OOO = re.compile(r"^[\s«\"“<(]*общество\s+с\s+ограниченной\s+ответственностью", re.I)


def org_form(name):
    """Название компании -> форма организации: «ООО "ТЕККИН"» -> «ООО», «МБУ МП "КЦМИ"» -> «МБУ»; не из известных ->
    «другая»; колонки с названием нет (None) -> None, форма не проверяется. Название нигде не сохраняется."""
    if name is None:
        return None
    if _FULL_OOO.match(name):
        return "ООО"
    found = _FORM_WORD.match(name)
    return found.group(1) if found and found.group(1) in KNOWN_FORMS else OTHER_FORM


def okved_class(code: str):
    """«62.01», «01.11», «1.11» (ведущий ноль потерян в Excel), «1,11» -> 62, 1, 1, 1; не разобрать -> None."""
    found = re.match(r"^\s*(\d{1,2})(?:[.,]|\s*$)", code or "")
    return int(found.group(1)) if found else None


class Composer:
    def __init__(self, groups: list, texts: Texts, link: str, signature: str, include_unwritten: bool = False,
                 okved_map: dict = None, form_rules: dict = None, now: datetime = None):
        check_variants(texts.variants)
        self.now = now or datetime.now()               # время суток для приветствий: по часу сборки письма
        self._counts = Counter()                       # (этап, группа) -> сколько писем уже собрано: варианты идут по кругу
        self.groups = {group.id: group for group in groups}
        self.okved_map = okved_map or {}
        self.form_rules = form_rules or {}             # форма -> писать (True) или не писать (False), решение агента
        self.by_class = {}
        for group in groups:
            for cls in group.classes:
                self.by_class.setdefault(cls, group.id)
        self.texts, self.link = texts, check_link(link)
        # Подпись нужна, только если в шаблонах есть {{подпись}}; нет такой подстановки - нет и подписи
        self.signature = check_signature(signature) if uses_placeholder(texts, "подпись") else ""
        self.include_unwritten = include_unwritten

    def form_decided(self, form) -> bool:
        return form is None or form in self.form_rules or form in DEFAULT_FORMS

    def form_allowed(self, form) -> bool:
        """Пишем ли организации такой формы: решение агента, иначе только ООО. Формы нет в таблице (None) - не проверяем."""
        if form is None:
            return True
        return self.form_rules[form] if form in self.form_rules else form in DEFAULT_FORMS

    def decision(self, okved: str):
        """Решение агента по коду (карта ОКВЭД): номер группы или EXCLUDE_ID; самый длинный подходящий код главнее
        («47.91.2», затем «47.91», «47.9», «47»). Решения нет - None."""
        digits = okved_digits(okved)
        for size in range(len(digits), 1, -1):
            found = self.okved_map.get(digits[:size])
            if found:
                return found
        return None

    def group_for(self, okved: str) -> str:
        """Группа письма: решение агента по коду, иначе таблица классов (первые два знака), иначе запасная.
        EXCLUDE_ID - агент решил не писать этому виду деятельности."""
        return self.decision(okved) or self.by_class.get(okved_class(okved), FALLBACK_ID)

    def is_written(self, group_id: str) -> bool:
        """Блок боли для письма 1 написан (или это запасная группа)."""
        return group_id == FALLBACK_ID or _key(self.groups[group_id].name) in self.texts.pain1

    def pick_variant(self, slot: str, stage: int, group_id: str) -> str:
        """Вариант фразы для места: по кругу среди подходящих по времени суток. Письма одной группы и этапа идут подряд,
        поэтому соседние письма получают разные варианты; одно и то же письмо при той же сборке получает тот же вариант."""
        period = day_period(self.now.hour)
        options = [text for tag, text in self.texts.variants[slot] if tag in ("", period)]
        return options[(self._counts[(stage, group_id)] + sum(map(ord, slot))) % len(options)]

    def subject_variant(self, group_id: str):
        """Тема из места «тема» раздела вариантов (по кругу); места нет - None, тогда тема одна, рекомендованная.
        {{группа_кратко}} в теме заменяется названием вида деятельности; у запасной группы берутся темы без неё."""
        if SUBJECT_SLOT not in self.texts.variants:
            return None
        short = self.groups[group_id].short
        options = [text for _, text in self.texts.variants[SUBJECT_SLOT] if short or "{{" + SUBJECT_WORDS + "}}" not in text]
        text = options[(self._counts[(1, group_id)] + sum(map(ord, SUBJECT_SLOT))) % len(options)]
        return text.replace("{{" + SUBJECT_WORDS + "}}", short)

    def letter_text(self, stage: int, group_id: str) -> str:
        group = self.groups[group_id]
        key = _key(group.name)
        pain1 = self.texts.pain1.get(key) if group_id != FALLBACK_ID else None
        pain2 = self.texts.pain2.get(key) if group_id != FALLBACK_ID else None
        # Запасная группа и группа без своего блока боли: свой шаблон письма, если агент его написал
        own = group_id == FALLBACK_ID or not self.is_written(group_id)
        template = (self.texts.fallback_templates.get(stage) if own else None) or self.texts.templates[stage]
        slots = {name.casefold(): self.pick_variant(name.casefold(), stage, group_id)
                 for name in {m.group(1) for m in _PLACEHOLDER.finditer(template)} if name.casefold() in self.texts.variants}
        text = fill(template, {
            "группа_словами": group.words,
            "боль_1": pain1 or self.texts.fallback1,
            "боль_2": pain2 or self.texts.fallback2,
            "ссылка_на_опрос": self.link,
            "подпись": self.signature,
        }, slots)
        self._counts[(stage, group_id)] += 1
        return text


# --- вход: таблица, стоп-лист, ответы, журналы ------------------------------------------------------------

def read_leads(path: str) -> list:
    """Таблица /leads -> [{"email", "okved", "okved_name", "source", "form"}]; ФИО не читаются, от названия остаётся
    только форма организации."""
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
    if OKVED_NAME_COLUMN in names:
        index["okved_name"] = names.index(OKVED_NAME_COLUMN)
    name_at = names.index(NAME_COLUMN) if NAME_COLUMN in names else None
    leads = []
    for row in rows[1:]:
        if any(cell.strip() for cell in row):
            lead = {key: (row[i].strip() if i < len(row) else "") for key, i in index.items()}
            lead.setdefault("okved_name", "")
            if name_at is None:
                lead["form"] = None                                    # старая выгрузка без названия: форму не проверяем
            else:
                lead["form"] = org_form(row[name_at] if name_at < len(row) else "")
            leads.append(lead)
    return leads


def iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError("нужна дата в виде ГГГГ-ММ-ДД") from None


def read_folder_leads(folder: str, day: str = ""):
    """Все таблицы leads_<дата>.csv в папке данных, от старых дат к новым (day - только эта дата) ->
    (компании, даты прочитанных таблиц, даты пропущенных). Таблица без нужных колонок (старая выгрузка) пропускается
    с пометкой, а не останавливает остальные."""
    try:
        names = sorted(name for name in os.listdir(folder) if LEADS_TABLE.fullmatch(name))
    except OSError as exc:
        raise ComposeError(f"не удалось открыть папку {folder}: {exc.strerror or exc}") from exc
    if day:
        names = [name for name in names if LEADS_TABLE.fullmatch(name).group(1) == day]
    if not names:
        raise ComposeError((f"в папке нет таблицы leads_{day}.csv" if day else "в папке нет таблиц leads_<дата>.csv")
                           + ": сначала соберите компании на странице /leads (вставьте текст Checko, нажмите «Запустить»)")
    leads, used, skipped = [], [], []
    for name in names:
        when = LEADS_TABLE.fullmatch(name).group(1)
        try:
            leads.extend(read_leads(os.path.join(folder, name)))
        except ComposeError:
            skipped.append(when)
        else:
            used.append(when)
    if not used:
        raise ComposeError("ни одну таблицу прочитать не удалось (нет колонок ОКВЭД и источника почты): выгрузите компании "
                           "на странице /leads заново, в новой выгрузке они есть")
    return leads, used, skipped


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
    unmapped: Counter = field(default_factory=Counter)      # (код ОКВЭД, его название) -> компаний без решения агента
    by_map: int = 0                                         # сколько подходящих адресов отнесено по карте ОКВЭД
    unknown_forms: Counter = field(default_factory=Counter)  # форма организации без решения агента -> компаний


def compose_first(composer: Composer, leads: list, subject: str, folder: str, stoplist, responded,
                  limit: int = 0) -> Result:
    result = Result()
    handled = set()
    for stage in (1, 2, 3):
        handled.update(journal.load_state(journal_path(folder, stage)))
    seen, seen_domains = set(), {address.rsplit("@", 1)[-1] for address in handled} - PUBLIC_DOMAINS
    for lead in leads:
        address = lead["email"].lower()
        domain = address.rsplit("@", 1)[-1]
        if not config.check_email(lead["email"]):
            result.skipped["некорректный адрес"] += 1
        elif lead["source"] != SOURCE_REGISTER:
            result.skipped["почта не из ЕГРЮЛ (контакты Checko или источник неизвестен)"] += 1
        elif not composer.form_allowed(lead.get("form")):
            form = lead["form"]
            label = "не определена по названию" if form == OTHER_FORM else f"«{form}»"
            if composer.form_decided(form):
                result.skipped[f"форма организации {label}: агент решил не писать"] += 1
            else:
                result.unknown_forms[form] += 1
                result.skipped[f"форма организации {label}: решения агента ещё нет, по умолчанию пишем только ООО"] += 1
        elif address in seen:
            result.skipped["повтор адреса (одна цепочка на адрес)"] += 1
        elif address in handled:
            result.skipped["уже получал письма из этой рассылки"] += 1
        elif domain in seen_domains:
            result.skipped["второй адрес той же организации (тот же домен почты): одно письмо на домен"] += 1
        elif config.is_stopped(address, stoplist):
            result.skipped["в стоп-листе"] += 1
        elif address in responded:
            result.skipped["уже заполнил опрос"] += 1
        else:
            group_id = composer.group_for(lead["okved"])
            if composer.decision(lead["okved"]) is None:
                result.unmapped[(lead["okved"], lead.get("okved_name", ""))] += 1
            else:
                result.by_map += 1
            if group_id == EXCLUDE_ID:
                result.skipped["агент решил не писать этому виду деятельности (карта ОКВЭД)"] += 1
                seen.add(address)
                continue
            if not composer.is_written(group_id) and not composer.include_unwritten:
                result.unwritten[group_id] += 1
                seen.add(address)
                seen_domains.add(domain) if domain not in PUBLIC_DOMAINS else None
                continue
            seen.add(address)
            if domain not in PUBLIC_DOMAINS:
                seen_domains.add(domain)
            result.letters.append((lead["email"], composer.subject_variant(group_id) or subject,
                                   composer.letter_text(1, group_id), group_id, lead["source"]))
    if limit:
        result.letters = result.letters[:limit]
    return result


def compose_next(composer: Composer, stage: int, folder: str, stoplist, responded, today: date,
                 min_days: int = 0) -> Result:
    """Этап 2 или 3 по журналам предыдущих этапов."""
    result = Result()
    previous = journal.load_state(journal_path(folder, stage - 1))
    first = journal.load_state(journal_path(folder, 1))
    later = set()
    for number in range(stage, 4):
        later.update(journal.load_state(journal_path(folder, number)))
    for address, entry in previous.items():
        if entry.get("status") != journal.SENT:
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
        if config.is_stopped(address, stoplist):
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


def write_okved_report(folder: str, composer: Composer, unmapped: Counter, unknown_forms: Counter = None) -> str:
    """Что осталось решить агенту -> okved_to_classify.md в папке данных: коды ОКВЭД с названиями видов деятельности и
    формы организаций (только сокращение вроде «МБУ»): ни адресов, ни названий компаний, ни ФИО. Решать нечего - старый
    файл удаляется. Возвращает путь или пустую строку."""
    unknown_forms = unknown_forms or Counter()
    path = os.path.join(folder, OKVED_REPORT_NAME)
    if not unmapped and not unknown_forms:
        if os.path.exists(path):
            os.remove(path)
        return ""
    lines = ["# Что осталось решить агенту (outreach-segment)", "",
             "Составлено программой compose.py. Здесь только коды и названия видов деятельности и сокращения форм "
             "организаций: адресов, названий компаний и ФИО нет. Передайте файл агенту: задача «Карта ОКВЭД» "
             "(01-Segment/instructions.md). Ответ агента - файл okved-map.md в папке 01-Segment/output.", ""]
    if unknown_forms:
        lines += ["## Формы организаций без решения", "",
                  "Пока агент не решил иначе, программа пишет только ООО. Для каждой формы нужно решение «писать» или "
                  "«не писать»; если для формы письмо должно звучать иначе, скажите об этом в заметке.", "",
                  "| Форма | Компаний |", "|---|---|"]
        lines += [f"| {'(не определена по названию)' if form == OTHER_FORM else form} | {number} |"
                  for form, number in sorted(unknown_forms.items(), key=lambda item: (-item[1], item[0]))]
        lines.append("")
    if unmapped:
        lines += ["## Виды деятельности (ОКВЭД) без решения", "",
                  "Колонка «Группа сейчас» показывает, куда код попадёт без решения агента (по первым двум знакам).", "",
                  "| Код | Название вида деятельности | Компаний | Группа сейчас |", "|---|---|---|---|"]
        for (code, name), number in sorted(unmapped.items(), key=lambda item: (-item[1], item[0])):
            now = composer.groups[composer.by_class.get(okved_class(code), FALLBACK_ID)]
            lines.append("| " + " | ".join((code or "(пусто)", (name or "(название не указано)").replace("|", "/"), str(number),
                                            f"{now.id}. {now.name}")) + " |")
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return path


def _shingles(text: str, size: int = 3) -> set:
    """Письмо -> набор троек подряд идущих слов (без ссылки): так сравнивают тексты на повторы."""
    words = re.findall(r"\w+", _LINK.sub(" ", text).casefold())
    return {tuple(words[i:i + size]) for i in range(max(1, len(words) - size + 1))}


def uniqueness_by_group(letters: list, max_pairs: int = 500) -> dict:
    """Базовая проверка на повторы: группа -> (средняя разница между письмами, наименьшая разница, число пар), в процентах.
    Разница двух писем = 100% минус доля общих троек слов (коэффициент Жаккара). Сравниваются письма одной группы:
    у разных групп свои тексты. Группы из одного письма пропускаются. Пар много - берётся одна и та же выборка."""
    by_group = {}
    for letter in letters:
        by_group.setdefault(letter[3], []).append(_shingles(letter[2]))
    report = {}
    for group_id, sets in by_group.items():
        count = len(sets)
        if count < 2:
            continue
        all_pairs = count * (count - 1) // 2
        if all_pairs <= max_pairs:
            pairs = [(i, j) for i in range(count) for j in range(i + 1, count)]
        else:
            rng = random.Random(0)
            pairs = [tuple(sorted(rng.sample(range(count), 2))) for _ in range(max_pairs)]
        diffs = [100.0 * (1 - len(sets[i] & sets[j]) / len(sets[i] | sets[j])) for i, j in pairs]
        report[group_id] = (sum(diffs) / len(diffs), min(diffs), len(diffs))
    return report


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
    parser.add_argument("leads", help="таблица leads_<дата>.csv или папка данных (тогда все таблицы leads_<дата>.csv в ней); "
                                      "рядом лежат и появляются остальные файлы партии")
    parser.add_argument("--date", default=None, type=iso_date,
                        help="с папкой данных: взять только таблицу за эту дату регистрации (ГГГГ-ММ-ДД)")
    parser.add_argument("--letters", default="", help="файл писем агентов (02-Write/output/….md) или OUTREACH_LETTERS")
    parser.add_argument("--segment", default="",
                        help="файл сегмента (01-Segment/output/….md): таблица групп ОКВЭД, или OUTREACH_SEGMENT")
    parser.add_argument("--stage", type=int, choices=(1, 2, 3), default=1,
                        help="1 - первое письмо, 2 - дожим, 3 - закрывающее (по умолчанию 1)")
    parser.add_argument("--link", default="", help="полная ссылка на опрос docs.google.com/forms/… (или SURVEY_LINK)")
    parser.add_argument("--signature", default="",
                        help="подпись (или MAIL_SIGNATURE); нужна, только если в шаблонах есть {{подпись}}")
    parser.add_argument("--subject", type=int, default=0, help="номер темы из файла писем (по умолчанию рекомендованная)")
    parser.add_argument("--limit", type=int, default=0, help="сколько писем в партии (этап 1; 0 - все подходящие)")
    parser.add_argument("--min-days", type=int, default=0,
                        help="этап 2: рабочих дней после первого письма (по умолчанию 3); "
                             "этап 3: дней после первого письма (по умолчанию 7)")
    parser.add_argument("--stoplist", default=None, help="стоп-лист (по умолчанию stoplist.txt рядом с таблицей)")
    parser.add_argument("--responded", default=None,
                        help="выгрузка ответов опроса (по умолчанию responses.csv рядом с таблицей, если он есть)")
    parser.add_argument("--okved-map", default="",
                        help="карта ОКВЭД от агента (okved-map.md) или OUTREACH_OKVED_MAP; по умолчанию okved-map.md "
                             "рядом с файлом сегмента, если он есть")
    parser.add_argument("--include-unwritten", action="store_true",
                        help="группам, для которых блок боли ещё не написан, подставить запасной вариант "
                             "(по умолчанию такие письма не собираются)")
    parser.add_argument("--min-unique", type=float, default=MIN_UNIQUE,
                        help="базовая проверка на повторы: средняя разница между письмами одной группы, не меньше этого "
                             f"процента (по умолчанию {MIN_UNIQUE:g}; 0 - не проверять). Меньше - письма не собираются: "
                             "нужны варианты формулировок в файле писем (раздел «Варианты фраз»)")
    parser.add_argument("--preview", action="store_true", help="показать по одному примеру письма на группу")
    parser.add_argument("--test-to", default="",
                        help="адреса через запятую: собрать letters_test.csv с пробными письмами им (таблица не читается)")
    parser.add_argument("--env", default=config.ENV_FILE, help="файл local.env")
    args = parser.parse_args(argv)

    try:
        config.load_env_file(args.env)
        letters_path = args.letters or os.environ.get("OUTREACH_LETTERS", "")
        segment_path = args.segment or os.environ.get("OUTREACH_SEGMENT", "")
        if not letters_path or not segment_path:
            raise ComposeError("не указаны файлы агентов: --letters и --segment (или OUTREACH_LETTERS и OUTREACH_SEGMENT "
                               "в local.env)")
        by_folder = os.path.isdir(args.leads)
        if args.date and not by_folder:
            raise ComposeError("--date работает, только когда вместо таблицы указана папка данных")
        folder = os.path.abspath(args.leads) if by_folder else os.path.dirname(os.path.abspath(args.leads))
        root = project_root(letters_path)
        if root and inside(folder, root):
            raise ComposeError(f"таблица с адресами лежит в папке проекта агентов ({root}). Адреса в проект не кладём: "
                               "перенесите таблицу и эту работу в отдельную папку вне проекта")
        with open(letters_path, encoding="utf-8-sig") as fh:
            texts = parse_letters(fh.read())
        with open(segment_path, encoding="utf-8-sig") as fh:
            groups = parse_segment(fh.read())
        check_texts(texts, groups)
        map_path = args.okved_map or os.environ.get("OUTREACH_OKVED_MAP", "")
        if not map_path and os.path.exists(os.path.join(os.path.dirname(os.path.abspath(segment_path)), OKVED_MAP_NAME)):
            map_path = os.path.join(os.path.dirname(os.path.abspath(segment_path)), OKVED_MAP_NAME)
        okved_map, form_rules = {}, {}
        if map_path:
            with open(map_path, encoding="utf-8-sig") as fh:
                map_text = fh.read()
            okved_map, form_rules = parse_okved_map(map_text, groups), parse_form_rules(map_text)
        composer = Composer(groups, texts, args.link or os.environ.get("SURVEY_LINK", ""),
                            args.signature or os.environ.get("MAIL_SIGNATURE", ""), args.include_unwritten, okved_map,
                            form_rules)
        subject = pick_subject(texts, args.subject)

        stop_path = args.stoplist or os.path.join(folder, "stoplist.txt")
        resp_path = args.responded or os.path.join(folder, "responses.csv")
        stoplist = config.read_stoplist(stop_path) if os.path.exists(stop_path) else frozenset()
        if args.stoplist and not os.path.exists(stop_path):
            raise ComposeError(f"нет файла стоп-листа {stop_path}")
        responded = read_responded(resp_path) if (args.responded or os.path.exists(resp_path)) else frozenset()
        when = today or date.today()

        tables_note = ""
        if args.test_to:
            addresses = [a.strip() for a in args.test_to.split(",") if a.strip()]
            bad = [a for a in addresses if not config.check_email(a)]
            if not addresses or bad:
                raise ComposeError("--test-to: нужны корректные адреса через запятую (свои ящики)")
            result = compose_test(composer, subject, args.stage, addresses)
            out = os.path.join(folder, "letters_test.csv")
        elif args.stage == 1:
            if by_folder:
                leads, used, skipped = read_folder_leads(folder, args.date or "")
                tables_note = (f"Таблицы выгрузки (даты регистрации): {', '.join(used)}; компаний в них: {len(leads)}."
                               + (f" Пропущены без колонок ОКВЭД и источника почты: {', '.join(skipped)}." if skipped else ""))
            else:
                leads, tables_note = read_leads(args.leads), ""
            result = compose_first(composer, leads, subject, folder, stoplist, responded, args.limit)
            out = os.path.join(folder, "letters_1.csv")
        else:
            result = compose_next(composer, args.stage, folder, stoplist, responded, when, args.min_days)
            out = os.path.join(folder, f"letters_{args.stage}.csv")
    except (ComposeError, OSError, ValueError) as exc:
        log(f"Ошибка: {getattr(exc, 'strerror', None) or exc}")
        return 1

    names = {group_id: group.name for group_id, group in composer.groups.items()}
    subject_note = (f"темы по кругу ({len(composer.texts.variants[SUBJECT_SLOT])} шт.)"
                    if SUBJECT_SLOT in composer.texts.variants and args.stage == 1 else f"тема «{subject}»")
    log(f"Этап {args.stage}: {subject_note}; стоп-лист: {len(stoplist)}; адресов в ответах опроса: {len(responded)}.")
    if tables_note:
        log(tables_note)
    for reason, number in result.skipped.most_common():
        log(f"  пропущено: {reason} - {number}")
    for group_id, number in sorted(result.unwritten.items()):
        log(f"  не собрано: для группы {group_id} «{names[group_id]}» ещё нет блока боли - {number} "
            "(напишите его или добавьте --include-unwritten)")
    if args.stage == 1 and not args.test_to:
        report = write_okved_report(folder, composer, result.unmapped, result.unknown_forms)
        if result.unknown_forms:
            log("  формы организаций без решения агента: "
                + ", ".join(f"{'не определена' if form == OTHER_FORM else form} - {number}"
                            for form, number in sorted(result.unknown_forms.items())) + "; пока пишем только ООО")
        if result.by_map:
            log(f"  карта ОКВЭД: решение агента есть у {result.by_map} из {result.by_map + sum(result.unmapped.values())} "
                "подходящих адресов")
        if result.unmapped:
            log(f"  ОКВЭД без решения агента: кодов {len(result.unmapped)}, компаний {sum(result.unmapped.values())}; "
                "они идут по таблице классов")
        if report:
            log(f"  Список для агента: {OKVED_REPORT_NAME} в папке данных")
    if not result.letters:
        log("Писать некому: ни одного письма не собрано, файл не создан.")
        return 0
    per_group = Counter(letter[3] for letter in result.letters)
    log(f"Собрано писем: {len(result.letters)}. По группам: "
        + "; ".join(f"{gid} {names[gid]} - {number}" for gid, number in sorted(per_group.items(), key=lambda i: i[0])))
    if args.stage == 1 and not args.test_to:         # группы без своего текста: пусть будет видно, что письмо запасное
        fallback = Counter(letter[3] for letter in result.letters if not composer.is_written(letter[3]))
        for group_id, number in sorted(fallback.items()):
            log(f"  запасной вариант текста: для группы {group_id} «{names[group_id]}» блок боли ещё не написан - писем {number}")
    if not args.test_to and args.min_unique > 0:
        report = uniqueness_by_group(result.letters)
        for group_id, (mean, lowest, pairs) in sorted(report.items()):
            log(f"  уникальность писем группы {group_id}: в среднем {mean:.0f}% отличий, наименьшее {lowest:.0f}% "
                f"(сравнено пар: {pairs}; порог {args.min_unique:g}%)")
        if not composer.texts.variants and any(number > 1 for number in per_group.values()):
            log("  внимание: в файле писем нет раздела «Варианты фраз», письма одной группы одинаковые (спам-признак). "
                "Попросите агента outreach-write добавить варианты.")
        weak = sorted(group_id for group_id, (mean, _, _) in report.items() if mean < args.min_unique)
        if weak and composer.texts.variants:
            log(f"Ошибка: письма группы {', '.join(weak)} слишком похожи друг на друга (порог {args.min_unique:g}% отличий): "
                "почтовые сервисы считают такую рассылку спамом. Файл писем не создан. Нужно больше вариантов формулировок "
                "(агент outreach-write, раздел «Варианты фраз»).")
            return 1
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
