"""Официальный API Checko: сведения об организации по ИНН (без капчи, по ключу).

Запрос:  GET https://api.checko.ru/v2/company?key=<КЛЮЧ>&inn=<ИНН>&source=true
Ответ:   {"data": {...}, "source_data": {...}, "meta": {"status": "ok"|"error", "message": ...}}

Из ответа берутся:
    * название  - data.НаимСокр / data.НаимПолн;
    * директор  - data.Руковод[].ФИО (первый с ФИО);
    * основной ОКВЭД - data.ОКВЭД (код и название); если там пусто, из исходных данных ЕГРЮЛ
                  (СвОКВЭДОсн: КодОКВЭД / НаимОКВЭД);
    * почта     - сначала из исходных данных ЕГРЮЛ (source_data, это та же почта, что в выписке),
                  иначе из data.Контакты.Емэйл (контакты «из открытых источников», могут быть
                  неполными или устаревшими - такие случаи помечаются).

Ключ в адрес запроса попадает, поэтому во всех сообщениях об ошибках он заменяется на ***.
Лимит API - 32 запроса в секунду с адреса, бесплатно 100 запросов в сутки (по документации Checko).
"""
import re
from dataclasses import dataclass
from typing import Optional

import requests

from egrul_pdf import find_email

API_URL = "https://api.checko.ru/v2/company"

_EMAIL_KEY_RE = re.compile(r"почт|e-?mail", re.IGNORECASE)
_NOT_FOUND_WORDS = ("не найден",)
_QUOTA_WORDS = ("лимит", "баланс", "средств", "превыш")


class CheckoApiError(Exception):
    """Ошибка обращения к API Checko."""


class CheckoApiNotFound(CheckoApiError):
    """Организации с таким ИНН нет в базе Checko."""


class CheckoApiQuotaExceeded(CheckoApiError):
    """Исчерпан лимит запросов или баланс: дальше стучаться бессмысленно."""


@dataclass
class CompanyInfo:
    name: str = ""
    director: str = ""
    email: str = ""
    email_from_register: bool = False      # True - почта из ЕГРЮЛ, False - из контактов Checko
    okved: str = ""                        # код основного вида деятельности, например «62.01»
    okved_name: str = ""


def _strings(node):
    """Все строки внутри вложенной структуры."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _register_email(node) -> str:
    """Почта из исходных данных ЕГРЮЛ: значения у ключей вида «...Почт...» / «E-mail»."""
    if isinstance(node, dict):
        for key, value in node.items():
            if _EMAIL_KEY_RE.search(str(key)):
                email = find_email(" ".join(_strings(value)))
                if email:
                    return email
            email = _register_email(value)
            if email:
                return email
    elif isinstance(node, list):
        for item in node:
            email = _register_email(item)
            if email:
                return email
    return ""


def _first(node: dict, *keys) -> str:
    for key in keys:
        value = node.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def _find_key(node, key):
    """Первое значение по ключу во вложенной структуре (в разборе ЕГРЮЛ ключ лежит глубоко)."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


def _main_okved(payload: dict):
    """(код, название) основного ОКВЭД. Сначала поле Checko, затем исходные данные ЕГРЮЛ."""
    own = (payload.get("data") or {}).get("ОКВЭД")
    if isinstance(own, dict):
        code, name = _first(own, "Код"), _first(own, "Наим", "Наименование", "Назв")
        if code:
            return code, name
    elif isinstance(own, str) and own.strip():
        code, _, name = own.strip().partition(" ")
        return code, name.strip()

    main = _find_key(payload.get("source_data"), "СвОКВЭДОсн")
    if isinstance(main, dict):
        attrs = main.get("@attributes") if isinstance(main.get("@attributes"), dict) else main
        return _first(attrs, "КодОКВЭД"), _first(attrs, "НаимОКВЭД")
    return "", ""


def parse_company(payload: dict) -> CompanyInfo:
    data = payload.get("data") or {}
    info = CompanyInfo(name=data.get("НаимСокр") or data.get("НаимПолн") or "")

    for person in data.get("Руковод") or []:
        fio = (person.get("ФИО") or "").strip()
        if fio:
            info.director = fio.title() if fio.isupper() else fio
            break

    info.okved, info.okved_name = _main_okved(payload)

    info.email = _register_email(payload.get("source_data"))
    info.email_from_register = bool(info.email)
    if not info.email:
        contacts = (data.get("Контакты") or {}).get("Емэйл") or []
        info.email = find_email(" ".join(_strings(contacts)))
    return info


class CheckoApi:
    def __init__(self, key: str, session: Optional[requests.Session] = None, timeout: float = 20.0):
        self.key = key
        self.session = session or requests.Session()
        self.timeout = timeout

    def _scrub(self, text) -> str:
        return str(text).replace(self.key, "***") if self.key else str(text)

    def company(self, inn: str) -> dict:
        """Ответ API по ИНН (JSON). Бросает CheckoApiNotFound / CheckoApiQuotaExceeded / CheckoApiError."""
        try:
            response = self.session.get(
                API_URL, params={"key": self.key, "inn": inn, "source": "true"}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise CheckoApiError(f"Нет связи с API Checko: {self._scrub(exc)}"[:200]) from None

        status = response.status_code
        if status in (402, 429):
            raise CheckoApiQuotaExceeded(f"HTTP {status}: лимит запросов или баланс")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        message = ""
        if isinstance(payload, dict):
            message = self._scrub((payload.get("meta") or {}).get("message") or "")
        if status == 404:
            raise CheckoApiNotFound(message or "HTTP 404")
        if status >= 400:
            raise CheckoApiError(f"HTTP {status}" + (f": {message}" if message else ""))
        if not isinstance(payload, dict):
            raise CheckoApiError("Ответ API не в формате JSON")

        meta_status = (payload.get("meta") or {}).get("status")
        if meta_status == "error":
            lowered = message.lower()
            if any(word in lowered for word in _QUOTA_WORDS):
                raise CheckoApiQuotaExceeded(message)
            if any(word in lowered for word in _NOT_FOUND_WORDS):
                raise CheckoApiNotFound(message)
            raise CheckoApiError(message or "API вернул ошибку")
        if not payload.get("data"):
            raise CheckoApiNotFound(message or "в ответе нет данных")
        return payload
