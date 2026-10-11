"""Фоновая выгрузка «новые организации -> таблица» для сайта (страница /leads).

Выгрузка идёт в отдельном потоке и дольше, чем живёт один запрос, поэтому страница
опрашивает состояние. Одновременно работает только одна выгрузка. Доступ закрыт паролем
(переменная окружения LEADS_TOKEN, не короче 12 знаков); без пароля страница отключена.
"""
import hmac
import os
import re
import tempfile
import threading
from collections import deque
from datetime import date
from typing import Optional

import leads
import xlsx_table
from checko_api import CheckoApi
from checko_updates import PastedPages
from mailing import journal

MIN_TOKEN_LENGTH = 12
STATE_LABELS = {
    0: "Готово.",
    3: "Checko недоступен с этого сервера, подробности в журнале ниже.",
    4: "Остановлено вами. Готовая часть сохранена.",
    7: "В вставленном тексте нет организаций за выбранную дату. Какие даты есть, написано в журнале ниже: укажите одну из них.",
    6: "Лимит запросов API Checko исчерпан. Готовая часть сохранена: продолжите завтра или смените тариф.",
    5: ("Данные не получены: запросы к Checko закончились ошибками. Причина в журнале ниже. Если там «соединение», "
        "«SSL» или «прокси» - дело в интернете, VPN, прокси или антивирусе: запустите check_checko.py (правой кнопкой -> "
        "Run) и пришлите, что он напишет. Если «HTTP 401» или «403» - проверьте ключ CHECKO_API_KEY в local.env."),
}
EMAIL_AT = leads.WEB_HEADER.index("Почта")
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
        self._restore_last_output()

    def _path_for(self, day: date) -> str:
        return os.path.join(self._out_dir, f"leads_{day.isoformat()}.csv")

    def _restore_last_output(self) -> None:
        """После перезапуска сайта запоминаем последнюю выгрузку из папки данных: запросы без даты (скачать таблицу,
        отправить письма) берут её. На странице таблица показывается только за выбранную дату, см. status(day)."""
        try:
            names = sorted(name for name in os.listdir(self._out_dir)
                           if re.fullmatch(r"leads_\d{4}-\d{2}-\d{2}\.csv", name))
        except OSError:
            return
        if not names:
            return
        newest = names[-1]
        try:
            self._target = date.fromisoformat(newest[len("leads_"):-len(".csv")])
        except ValueError:
            return
        self._out = os.path.join(self._out_dir, newest)

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
            self._out = self._path_for(target)
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

    def _refresh(self, out: str, busy: bool) -> None:
        """Колонки «Первое письмо», «Дожим», «Закрывающее» в файле таблицы догоняют журнал отправки. Пока таблицу
        пишет идущая выгрузка, её не трогаем: выгрузка сама запишет их при следующем сохранении."""
        if busy:
            return
        try:
            leads.refresh_table(out)
        except OSError:
            pass                                  # диск недоступен: таблица на странице всё равно строится из журнала

    def _busy_with(self, day: Optional[date]) -> bool:
        with self._lock:
            return self._state == "running" and day is not None and day == self._target

    def status(self, day: Optional[date] = None) -> dict:
        """Состояние выгрузки и таблица за день `day`. Без даты таблицы в ответе нет: страница показывает компании
        только за выбранную дату, а не все выгрузки подряд."""
        with self._lock:
            state, message = self._state, self._message
            target, limit = self._target, self._limit
        counts = {leads.OK: 0, leads.NO_EMAIL: 0, leads.RETRY: 0}
        rows, sent = [], {}
        out = self._path_for(day) if day else None
        if out:
            self._refresh(out, self._busy_with(day))
            saved = leads.load_state(leads.state_path(out))
            counts = {status: leads.count(saved, status) for status in counts}
            rows = leads.table_rows(saved, journal.sent_stages(self._out_dir))
            emails = {row[EMAIL_AT].lower() for row in rows}
            # в ответ идут только адреса этой таблицы, а не всё, что когда-либо отправляли
            sent = {address: entry for address, entry in journal.sent_overview(self._out_dir).items()
                    if address in emails}
        return {
            "state": state,
            "message": message,
            "date": target.isoformat() if target else "",
            "view_date": day.isoformat() if day else "",
            "limit": limit,
            "counts": counts,
            "header": leads.WEB_HEADER,
            "rows": rows,                      # таблица выбранной даты: страница показывает её по мере заполнения
            "sent": sent,                      # адрес -> последнее отправленное письмо (для текста и времени в строке)
            "log": list(self._lines),
            "has_table": bool(out and os.path.exists(out) and counts[leads.OK]),
            "api_available": self.api_available,
        }

    def _table_file(self, day: Optional[date]) -> Optional[str]:
        out = self._path_for(day) if day else self._out
        return out if out and os.path.exists(out) else None

    def output_path(self, day: Optional[date] = None) -> Optional[str]:
        """Таблица за день `day` (без даты - текущей или последней выгрузки), если файл есть и в нём есть организации
        с почтой."""
        out = self._table_file(day)
        if not out:
            return None
        return out if leads.count(leads.load_state(leads.state_path(out)), leads.OK) else None

    def table(self, day: Optional[date] = None):
        """(имя файла .xlsx, содержимое) за день `day` (без даты - текущей или последней выгрузки) или None, если
        выгрузки ещё не было."""
        out = self._table_file(day)
        if not out:
            return None
        self._refresh(out, self._busy_with(day or self._target))
        rows = leads.table_rows(leads.load_state(leads.state_path(out)), journal.sent_stages(self._out_dir))
        return os.path.splitext(os.path.basename(out))[0] + ".xlsx", xlsx_table.build(leads.WEB_HEADER, rows)
