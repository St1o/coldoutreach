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

MIN_TOKEN_LENGTH = 12
STATE_LABELS = {
    0: "Готово.",
    2: ("ФНС потребовала капчу: это защита от частых запросов. Выгрузка остановлена, готовая часть "
        "сохранена. Подождите, поставьте паузу побольше и запустите снова: проверенные организации "
        "повторно не проверяются."),
    3: "Checko недоступен с этого сервера, подробности в журнале ниже.",
    4: "Остановлено вами. Готовая часть сохранена.",
    6: "Лимит запросов API Checko исчерпан. Готовая часть сохранена: продолжите завтра или смените тариф.",
    5: "Остановлено: несколько ошибок подряд, сайт ФНС отвечает не как обычно. Причина в журнале ниже; повторите позже.",
}
CAPTCHA_WAIT = 600          # секунд: при капче ФНС ждём и продолжаем
API_PAUSE = 0.3             # секунд между запросами к API Checko (лимит API - 32 в секунду)


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

    def start(self, target: date, limit: int, text: str = "", pause: float = 6.0,
              source: str = "egrul") -> None:
        with self._lock:
            if self._state == "running":
                raise JobRunning()
            self._state, self._message = "running", ""
            self._stop.clear()
            self._target, self._limit = target, limit
            self._out = os.path.join(self._out_dir, f"leads_{target.isoformat()}.csv")
            self._lines.clear()
            self._thread = threading.Thread(target=self._work, args=(target, limit, self._out, text, pause, source), daemon=True)
            self._thread.start()

    def _work(self, target, limit, out, text="", pause=6.0, source="egrul"):
        extra = {"checko": PastedPages(text)} if text.strip() else {}
        if source == "api":                         # API не ограничивает капчей: пауза нужна только для вежливости
            extra["process"] = lambda company: leads.process_company_api(self._api, company)
            pause = API_PAUSE
        try:
            code = self._run(target, out, limit=limit, pause=pause, log=self._lines.append,
                            captcha_wait=CAPTCHA_WAIT, should_stop=self._stop.is_set, **extra)
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
        }

    def rows(self) -> list:
        """Строки таблицы (организации с почтой) в том же порядке, что в CSV. ИНН - ключ строки:
        по нему страница помнит, какие строки уже скопированы."""
        if not self._out:
            return []
        saved = leads.load_state(leads.state_path(self._out))
        return [{"inn": inn, "name": entry["name"], "director": entry["director"], "email": entry["email"]}
                for inn, entry in saved.items() if entry["status"] == leads.OK]

    def table(self):
        """(имя файла, содержимое CSV) или None."""
        out = self._out
        if not out or not os.path.exists(out):
            return None
        with open(out, "rb") as fh:
            return os.path.basename(out), fh.read()
