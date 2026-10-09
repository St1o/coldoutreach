"""Список новых организаций с https://checko.ru/company/updates за нужный день.

Страница «Новые организации» отсортирована от новых к старым, по 100 строк на странице.
В каждой строке есть дата регистрации, ОГРН и ИНН. Алгоритм:
    1. Загрузить первую страницу и найти в пагинации адрес второй, чтобы узнать
       шаблон адресов страниц (если не нашёлся - ?page=N).
    2. На каждой странице взять строки с нужной датой регистрации.
    3. Остановиться, когда на странице не осталось строк с этой датой или новее.

Разбор не зависит от CSS-классов: берётся видимый текст страницы и ищется шаблон
«Дата регистрации <день> <месяц> <год> ОГРН <...> ИНН <...>».
"""
import re
import time
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin

import requests

from egrul_inn_search import validate_inn

BASE_URL = "https://checko.ru"
UPDATES_URL = BASE_URL + "/company/updates"

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

ITEM_RE = re.compile(
    r"Дата регистрации\s+(\d{1,2})\s+([а-яё]+)\s+(\d{4})(?:\s+года)?\s+"
    r"ОГРН\s+(\d{13,15})\s+ИНН\s+(\d{10}|\d{12})(?!\d)",
    re.IGNORECASE,
)


class CheckoError(Exception):
    """Не удалось получить или разобрать страницу Checko."""


class CheckoBlocked(CheckoError):
    """Сайт не отдаёт страницу скрипту (403/429/защита от ботов)."""


@dataclass
class NewCompany:
    inn: str
    ogrn: str
    reg_date: date
    name: str = ""       # название со страницы Checko (если удалось сопоставить)


class _PageParser(HTMLParser):
    """Собирает видимый текст и ссылки (адрес, текст)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = []
        self.links = []
        self._skip = 0
        self._href = None
        self._link_text = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._link_text = []

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._link_text).split())))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self.chunks.append(data)
        if self._href is not None:
            self._link_text.append(data)


_NUMBERED_RE = re.compile(r"^\s*\d{1,4}\s*[.)]\s*(.*)$")


def _name_before(segment: str) -> str:
    """Название организации из текста перед строкой «Дата регистрации...»: после номера «12.» в начале
    строки (сам номер может стоять отдельной строкой). Не нашли уверенно - пустая строка."""
    lines = [line.strip() for line in segment.splitlines() if line.strip()]
    for index in range(len(lines) - 1, -1, -1):
        match = _NUMBERED_RE.match(lines[index])
        if match:
            name = match.group(1).strip()
            if not name and index + 1 < len(lines):
                name = lines[index + 1]
            return name if re.search(r"[A-Za-zА-Яа-яЁё]{2}", name) and len(name) <= 300 else ""
    return ""


def items_from_text(text: str, names: bool = False) -> list:
    """Строки «Дата регистрации ... ОГРН ... ИНН ...» из видимого текста страницы.

    names=True: ещё и название каждой организации (для текста, скопированного со страницы вручную)."""
    items = []
    previous_end = 0
    for match in ITEM_RE.finditer(text):
        segment = text[previous_end:match.start()]
        previous_end = match.end()
        day, month, year, ogrn, inn = match.groups()
        month_no = MONTHS.get(month.lower())
        if not month_no or not validate_inn(inn):
            continue
        try:
            reg_date = date(int(year), month_no, int(day))
        except ValueError:
            continue
        items.append(NewCompany(inn=inn, ogrn=ogrn, reg_date=reg_date, name=_name_before(segment) if names else ""))
    return items


def parse_page(html: str):
    """-> (список NewCompany, ссылки пагинации [(адрес, номер)])."""
    parser = _PageParser()
    parser.feed(html)
    parser.close()
    items = items_from_text(" ".join(parser.chunks))

    # Названия: ссылки на организации по порядку; берём, только если их ровно столько же, сколько строк
    names = [text_ for href, text_ in parser.links
             if "/company/" in href and not href.rstrip("/").endswith("/updates")
             and re.search(r"[A-Za-zА-Яа-яЁё]{2}", text_)]
    if len(names) == len(items):
        for item, name in zip(items, names):
            item.name = name

    pager = [(href, text_) for href, text_ in parser.links if text_.isdigit() and href]
    return items, pager


def page_url_template(pager, page_url: str) -> Optional[str]:
    """Шаблон адреса страницы по ссылке на страницу «2»; None, если не нашли."""
    for href, number in pager:
        if number != "2":
            continue
        url = urljoin(page_url, href)
        matches = list(re.finditer(r"(?<!\d)2(?!\d)", url))
        if matches:
            last = matches[-1]
            return url[:last.start()] + "{n}" + url[last.end():]
    return None


class CheckoClient:
    def __init__(self, session: Optional[requests.Session] = None, timeout: float = 20.0):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru-RU,ru;q=0.9",
        })

    def get_html(self, url: str) -> str:
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise CheckoError(f"Ошибка запроса {url}: {exc}") from exc
        if response.status_code in (403, 429, 503):
            raise CheckoBlocked(f"Checko не отдаёт страницу ({response.status_code}): {url}")
        if response.status_code >= 400:
            raise CheckoError(f"Checko ответил {response.status_code}: {url}")
        return response.content.decode("utf-8", errors="replace")

    def new_companies(self, target: date, max_pages: int = 25, pause: float = 1.5) -> list:
        """Организации, зарегистрированные в день `target` (по порядку на сайте, без повторов)."""
        found, seen, template = [], set(), None
        for page in range(1, max_pages + 1):
            if page == 1:
                url = UPDATES_URL
            else:
                url = (template or UPDATES_URL + "?page={n}").replace("{n}", str(page))
            items, pager = parse_page(self.get_html(url))
            if page == 1:
                if not items:
                    raise CheckoError(
                        "На странице Checko не найдено ни одной организации: возможно, "
                        "изменилась вёрстка или сайт показал проверку на робота.")
                template = page_url_template(pager, url)
            if not items:
                break
            for item in items:
                if item.reg_date == target and item.inn not in seen:
                    seen.add(item.inn)
                    found.append(item)
            if not any(item.reg_date >= target for item in items):
                break               # дальше только более старые регистрации
            if page < max_pages:
                time.sleep(pause)
        return found


class PastedPages:
    """Те же организации из текста, который пользователь скопировал со страниц Checko в
    своём браузере (Ctrl+A, Ctrl+C). Нужен, когда Checko не отдаёт страницы серверу."""

    def __init__(self, text: str):
        self.text = text

    def new_companies(self, target: date, max_pages: int = 25) -> list:
        found, seen = [], set()
        for item in items_from_text(self.text, names=True):
            if item.reg_date == target and item.inn not in seen:
                seen.add(item.inn)
                found.append(item)
        return found
