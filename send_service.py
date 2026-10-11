"""Кнопка «Отправить» на странице /leads: собрать письма первого этапа и запустить отправку в фоне.

Работают те же программы, что и в командной строке: compose.py собирает письма из таблицы и текстов агентов,
mailer.py отправляет (пауза между письмами, дневной лимит, журнал, стоп-лист, одна отправка на папку).
Сервис только вызывает их и пересказывает результат без адресов. Адрес и пароль ящика берутся из окружения
(local.env), на страницу и в ответы не попадают. Дожим и закрывающее письмо (этапы 2 и 3) по-прежнему
собирает compose.py: LOCAL.md, раздел «Рассылка».
"""
import contextlib
import io
import os
import re

import compose
import mailer
from mailing import config as mailcfg, journal
from mailing.lock import lock_holder
from mailing.table import LettersError

MIN_COUNT, MAX_COUNT = 1, mailcfg.DAILY_LIMIT_MAX
LETTERS_NAME = "letters_1.csv"
_COUNT = re.compile(r"Собрано писем: (\d+)")
_CLI_HINT = " (напишите его или добавьте --include-unwritten)"      # подсказка для командной строки
_PAGE_HINT = " (чтобы отправить и им, поставьте галочку про запасной вариант под кнопкой)"
_EXAMPLE = re.compile(r"^\n?----- пример, группа (\S+) «(.*?)», тема: (.*?) -----\n(.*)$", re.S)


class SendError(Exception):
    """Ошибка с кодом ответа сайта и понятным пользователю текстом (адресов в нём нет)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class SendService:
    def __init__(self, data_dir: str, settings_path: str = "", compose_main=compose.main, mailer_main=mailer.main):
        self.data_dir = os.path.abspath(data_dir)
        self.settings_path = settings_path
        self._compose = compose_main
        self._mailer = mailer_main

    @property
    def letters_path(self) -> str:
        return os.path.join(self.data_dir, LETTERS_NAME)

    @staticmethod
    def mail_ready() -> bool:
        return mailcfg.check_email(os.environ.get("MAILRU_USER", "")) and bool(os.environ.get("MAILRU_APP_PASSWORD"))

    def _daily(self):
        try:
            return mailcfg.resolve_daily_limit(settings_path=self.settings_path)[0]
        except ValueError:
            return mailcfg.DEFAULT_DAILY_LIMIT

    def _check_count(self, count) -> int:
        try:
            number = int(count)
        except (TypeError, ValueError):
            number = 0
        if not MIN_COUNT <= number <= MAX_COUNT:
            raise SendError(400, f"Сколько писем отправить: целое число от {MIN_COUNT} до {MAX_COUNT}.")
        return number

    def _check_ready(self, leads_path: str) -> None:
        if not leads_path or os.path.dirname(os.path.abspath(leads_path)) != self.data_dir:
            raise SendError(400, "Таблицы пока нет: сначала выгрузите компании.")
        if not self.mail_ready():
            raise SendError(400, "Ящик не настроен: впишите MAILRU_USER и MAILRU_APP_PASSWORD в local.env.")
        holder = lock_holder(self.data_dir)
        if holder:
            raise SendError(409, f"Отправка уже идёт (процесс {holder}). Дождитесь окончания.")

    def prepare(self, leads_path: str, count, include_unwritten: bool = False) -> dict:
        """Собирает письма (файл letters_1.csv) и рассказывает, что уйдёт: числа, группы, по примеру письма на группу."""
        number = self._check_count(count)
        self._check_ready(leads_path)
        lines = []
        argv = [leads_path, "--limit", str(number), "--preview"] + (["--include-unwritten"] if include_unwritten else [])
        code = self._compose(argv, log=lines.append)
        if code != 0:
            raise SendError(400, "\n".join(line for line in lines if line.strip()) or "Письма собрать не удалось.")
        letters, summary, examples = 0, [], []
        for line in lines:
            found = _COUNT.search(line)
            if found:
                letters = int(found.group(1))
            example = _EXAMPLE.match(line)
            if example:
                examples.append({"group": example.group(1), "name": example.group(2),
                                 "subject": example.group(3), "text": example.group(4).strip()})
            elif line.strip() and not line.startswith(("Файл:", "Дальше:")):
                summary.append(line.strip().replace(_CLI_HINT, _PAGE_HINT))
        used = journal.sent_last_24h(self.data_dir)
        daily = self._daily()
        return {"letters": letters, "summary": summary, "examples": examples, "daily_limit": daily,
                "sent_24h": used, "room": max(0, daily - used) if daily else None}

    def start(self, leads_path: str, count, include_unwritten: bool = False, expected=None) -> dict:
        """Собирает письма заново и запускает отправку в фоне. expected - сколько писем показали пользователю:
        если таблица за это время изменилась, отправка не начинается."""
        info = self.prepare(leads_path, count, include_unwritten)
        if info["letters"] == 0:
            raise SendError(400, "Писем для отправки нет: " + "; ".join(info["summary"]))
        if expected is not None and info["letters"] != expected:
            raise SendError(409, f"Пока вы смотрели, набор писем изменился: теперь их {info['letters']}, а не {expected}. "
                                 "Нажмите «Отправить» ещё раз и проверьте.")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self._mailer([self.letters_path, "--send", "--background", "--limit", str(info["letters"])])
        if code != 0:
            raise SendError(400, out.getvalue().strip() or "Отправку запустить не удалось.")
        return {"started": True, "letters": info["letters"], "daily_limit": info["daily_limit"],
                "message": f"Отправка запущена в фоне: писем {info['letters']}, между письмами пауза 30 с. "
                           "Страницу можно закрыть: отправка идёт сама, а дневной лимит и журнал не дадут отправить лишнего."}

    def status(self) -> dict:
        """Идёт ли отправка и сводка по журналу (без адресов)."""
        running = bool(lock_holder(self.data_dir))
        lines = []
        if os.path.exists(self.letters_path):
            try:
                lines = mailer.status_lines(self.letters_path, self._daily())
            except LettersError:
                lines = []
        return {"running": running, "lines": lines, "ready": self.mail_ready()}
