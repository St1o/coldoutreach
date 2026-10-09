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
import xlsx_table
from checko_api import CheckoApi
from checko_updates import PastedPages

MIN_TOKEN_LENGTH = 12
STATE_LABELS = {
    0: "Готово.",
    3: "Checko недоступен с этого сервера, подробности в журнале ниже.",
    4: "Остановлено вами. Готовая часть сохранена.",
    7: "В вставленном тексте нет организаций за выбранную дату. Какие даты есть, написано в журнале ниже: укажите одну из них.",
    6: "Лимит запросов API Checko исчерпан. Готовая часть сохранена: продолжите завтра или смените тариф.",
    5: "Остановлено: несколько ошибок подряд. Причина в журнале ниже (проверьте CHECKO_API_KEY); повторите позже.",
}
API_PAUSE = 0.05            # секунд между запросами к API Checko: не больше 20 в секунду при лимите API 32


class JobRunning(Exception):
    """Выгрузка уже идёт."""


class LeadsJob:
    def __init__(self, token: str = "", run=leads.run, out_dir: Optional[str] = None,
                 api_key: str = "", api=None):
        self.token = token if len(token or "") >= MIN_TOKEN_LENGTH else ""
        self._run = run
        self._api = api or (CheckoApi(api_key) if api_key else None)
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
            self._thread = threading.Thread(target=self._work, args=(target, limit, self._out, text), daemon=True)
            self._thread.start()

    def _work(self, target, limit, out, text=""):
        extra = {"checko": PastedPages(text)} if text.strip() else {}
        try:
            code = self._run(target, out, limit=limit, pause=API_PAUSE, log=self._lines.append,
                             should_stop=self._stop.is_set,
                             process=lambda company: leads.process_company_api(self._api, company), **extra)
            state = "done" if code == 0 else "stopped"
            message = STATE_LABELS.get(code, f"Завершено с кодом {code}.")
        except Exception as exc:        # любая неожиданность не должна оставлять «идёт»
            state, message = "error", f"Ошибка: {exc}"
        with self._lock:
            self._state, self._message = state, message

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
        rows = []
        if out:
            saved = leads.load_state(leads.state_path(out))
            counts = {status: leads.count(saved, status) for status in counts}
            rows = leads.table_rows(saved)
        return {
            "state": state,
            "message": message,
            "date": target.isoformat() if target else "",
            "limit": limit,
            "counts": counts,
            "header": leads.WEB_HEADER,
            "rows": rows,                      # таблица целиком: страница показывает её по мере заполнения
            "log": list(self._lines),
            "has_table": bool(out and os.path.exists(out) and counts[leads.OK]),
            "api_available": self.api_available,
        }

    def table(self):
        """(имя файла .xlsx, содержимое) или None, если выгрузку ещё не запускали."""
        out = self._out
        if not out or not os.path.exists(out):
            return None
        rows = leads.table_rows(leads.load_state(leads.state_path(out)))
        return os.path.splitext(os.path.basename(out))[0] + ".xlsx", xlsx_table.build(leads.WEB_HEADER, rows)
