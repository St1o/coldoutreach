"""Фоновая выгрузка «новые организации -> таблица» для сайта (страница /leads).

Выгрузка идёт в отдельном потоке и дольше, чем живёт один запрос, поэтому страница
опрашивает состояние. Одновременно работает только одна выгрузка. Доступ закрыт паролем
(переменная окружения LEADS_TOKEN, не короче 12 знаков); без пароля страница отключена.
"""
import hmac
import os
import tempfile
import threading
from collections import deque
from datetime import date
from typing import Optional

import leads
from checko_api import CheckoApi
from checko_updates import PastedPages
from sheets_sync import SheetsError

MIN_TOKEN_LENGTH = 12
STATE_LABELS = {
    0: "Готово.",
    3: "Checko недоступен с этого сервера, подробности в журнале ниже.",
    4: "Остановлено вами. Готовая часть сохранена.",
    6: "Лимит запросов API Checko исчерпан. Готовая часть сохранена: продолжите завтра или смените тариф.",
    5: "Остановлено: несколько ошибок подряд. Причина в журнале ниже (проверьте CHECKO_API_KEY); повторите позже.",
}
SHEET_BATCH = 10            # строк в одной записи в Google-таблицу: не чаще, чем нужно, и не теряем много при сбое
API_PAUSE = 0.05            # секунд между запросами к API Checko: не больше 20 в секунду при лимите API 32


class JobRunning(Exception):
    """Выгрузка уже идёт."""


class LeadsJob:
    def __init__(self, token: str = "", run=leads.run, out_dir: Optional[str] = None,
                 api_key: str = "", api=None, sheets=None):
        self.token = token if len(token or "") >= MIN_TOKEN_LENGTH else ""
        self._run = run
        self._api = api or (CheckoApi(api_key) if api_key else None)
        self._sheets = sheets                    # SheetsSync или None: тогда результаты только в скачиваемой таблице
        self._pending: list = []                 # строки, ещё не записанные в Google-таблицу
        self._out_dir = out_dir or tempfile.gettempdir()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._lines = deque(maxlen=60)
        self._thread: Optional[threading.Thread] = None
        self._state = "idle"          # idle | running | done | stopped | error
        self._message = ""
        self._target: Optional[date] = None
        self._limit = 0
        self._out: Optional[str] = None

    @property
    def api_available(self) -> bool:
        return self._api is not None

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def check_token(self, candidate: str) -> bool:
        return self.enabled and hmac.compare_digest(
            (candidate or "").encode("utf-8"), self.token.encode("utf-8"))

    def start(self, target: date, limit: int, text: str = "") -> None:
        with self._lock:
            if self._state == "running":
                raise JobRunning()
            self._state, self._message = "running", ""
            self._stop.clear()
            self._target, self._limit = target, limit
            self._out = os.path.join(self._out_dir, f"leads_{target.isoformat()}.csv")
            self._lines.clear()
            self._pending = []
            self._thread = threading.Thread(target=self._work, args=(target, limit, self._out, text), daemon=True)
            self._thread.start()

    def _work(self, target, limit, out, text=""):
        extra = {"checko": PastedPages(text)} if text.strip() else {}
        state, message = "error", ""
        try:
            if self._sheets:
                extra.update(self._prepare_sheet(target, out))
            code = self._run(target, out, limit=limit, pause=API_PAUSE, log=self._lines.append,
                             should_stop=self._stop.is_set,
                             process=lambda company: leads.process_company_api(self._api, company), **extra)
            state = "done" if code == 0 else "stopped"
            message = STATE_LABELS.get(code, f"Завершено с кодом {code}.")
        except Exception as exc:        # любая неожиданность не должна оставлять «идёт»
            state, message = "error", f"Ошибка: {exc}"
        if self._sheets and self._pending and not self._flush():
            message += " Часть строк не записалась в Google-таблицу (причина в журнале): скачайте таблицу кнопкой ниже."
        with self._lock:
            self._state, self._message = state, message

    def _prepare_sheet(self, target, out) -> dict:
        """Какие ИНН уже проверены (по ним платных запросов нет) и что писать в таблицу по ходу работы.
        Недоступная таблица - ошибка ДО первого платного запроса. Результаты, которые остались только
        в состоянии на сервере (прошлый сбой записи), дописываются в таблицу."""
        known = self._sheets.checked_inns()
        for inn, entry in leads.load_state(leads.state_path(out)).items():
            if entry.get("status") in (leads.OK, leads.NO_EMAIL) and inn not in known:
                self._pending.append(self._sheet_row(inn, entry, target))
        if self._pending:
            self._flush()
        return {"skip": known, "on_result": lambda company, entry: self._queue(company, entry, target)}

    @staticmethod
    def _sheet_row(inn, entry, target) -> dict:
        return {"inn": inn, "status": entry["status"], "date": target.isoformat(), "name": entry.get("name", ""),
                "director": entry.get("director", ""), "email": entry.get("email", ""), "okved": entry.get("okved", "")}

    def _queue(self, company, entry, target):
        self._pending.append(self._sheet_row(company.inn, entry, target))
        if len(self._pending) >= SHEET_BATCH:
            self._flush()

    def _flush(self) -> bool:
        """Записать накопленное в Google-таблицу. Не вышло - строки остаются и уйдут со следующей порцией."""
        if not self._pending:
            return True
        try:
            added = self._sheets.append(self._pending)
        except SheetsError as exc:
            self._lines.append(f"Google-таблица: не записалось ({exc}); повторю со следующей порцией.")
            return False
        self._lines.append(f"Google-таблица: записано строк {added} из {len(self._pending)}.")
        self._pending = []
        return True

    def stop(self) -> bool:
        """Просит остановить идущую выгрузку (готовая часть сохраняется). True - она шла."""
        with self._lock:
            running = self._state == "running"
        if running:
            self._stop.set()
        return running

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def status(self) -> dict:
        with self._lock:
            state, message = self._state, self._message
            target, limit, out = self._target, self._limit, self._out
        counts = {leads.OK: 0, leads.NO_EMAIL: 0, leads.RETRY: 0}
        if out:
            saved = leads.load_state(leads.state_path(out))
            counts = {status: leads.count(saved, status) for status in counts}
        return {
            "state": state,
            "message": message,
            "date": target.isoformat() if target else "",
            "limit": limit,
            "counts": counts,
            "log": list(self._lines),
            "has_table": bool(out and os.path.exists(out) and counts[leads.OK]),
            "api_available": self.api_available,
            "sheets": self._sheets is not None,
        }

    def table(self):
        """(имя файла, содержимое CSV) или None."""
        out = self._out
        if not out or not os.path.exists(out):
            return None
        with open(out, "rb") as fh:
            return os.path.basename(out), fh.read()
