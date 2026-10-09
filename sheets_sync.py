"""Запись результатов в Google-таблицу через веб-приложение Google Apps Script (без Google Cloud и карты).

Скрипт (google_sheet_script.js) вставляется в вашу таблицу: Расширения -> Apps Script. Он принимает
строки по секретному паролю и добавляет их на листы «Компании» (только организации с почтой) и
«Проверенные» (все проверенные ИНН: по ним сервер не тратит платные запросы повторно).

Тот же класс читает таблицу из Python:
    from sheets_sync import SheetsSync
    rows = SheetsSync(URL, TOKEN).table()          # список словарей: «Название», «ФИО директора», «Почта», ...

Пароль и адрес попадают в запросы, поэтому в сообщениях об ошибках они заменяются на ***.
"""
from typing import Optional

import requests

COMPANIES = "companies"     # лист «Компании»
CHECKED = "checked"         # лист «Проверенные»


class SheetsError(Exception):
    """Google-таблица недоступна или ответила ошибкой."""


class SheetsSync:
    def __init__(self, url: str, token: str, session: Optional[requests.Session] = None, timeout: float = 60.0):
        self.url, self.token = url, token
        self.session = session or requests.Session()
        self.timeout = timeout

    def _scrub(self, text) -> str:
        text = str(text)
        for secret in (self.token, self.url):
            if secret:
                text = text.replace(secret, "***")
        return text[:200]

    def _call(self, method: str, **kwargs) -> dict:
        try:
            response = self.session.request(method, self.url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise SheetsError(f"нет связи со скриптом Google: {self._scrub(exc)}") from None
        try:
            payload = response.json()
        except ValueError:
            raise SheetsError("скрипт Google ответил не так, как ожидалось: проверьте адрес и что у веб-приложения "
                              "доступ «Все» (HTTP %s)" % response.status_code) from None
        if not isinstance(payload, dict):
            raise SheetsError("скрипт Google вернул неожиданный ответ")
        if payload.get("error"):
            raise SheetsError(self._scrub(f"скрипт Google: {payload['error']}"))
        return payload

    def table(self, sheet: str = COMPANIES) -> list:
        """Строки листа как список словарей по заголовкам колонок (все значения - строки)."""
        payload = self._call("GET", params={"token": self.token, "sheet": sheet})
        header = payload.get("header") or []
        return [dict(zip(header, row)) for row in payload.get("rows") or []]

    def checked_inns(self) -> set:
        """ИНН, которые уже проверены раньше (по ним платные запросы не повторяются)."""
        payload = self._call("GET", params={"token": self.token, "sheet": CHECKED})
        return {str(row[0]) for row in payload.get("rows") or [] if row}

    def append(self, rows: list) -> int:
        """Добавить строки [{inn, status, date, name, director, email, okved}]; уже известные ИНН скрипт пропускает.
        Возвращает, сколько строк добавлено."""
        payload = self._call("POST", json={"token": self.token, "rows": rows})
        return int(payload.get("added") or 0)
