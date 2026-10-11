"""Кнопка «Отправить» на странице /leads: собрать письма первого этапа и запустить отправку в фоне.

Работают те же программы, что и в командной строке: compose.py собирает письма из таблицы и текстов агентов,
mailer.py отправляет (пауза между письмами, дневной лимит, журнал, стоп-лист, одна отправка на папку).
Сервис только вызывает их и пересказывает результат без адресов. Адрес и пароль ящика берутся из окружения
(local.env), на страницу и в ответы не попадают. Дожим и закрывающее письмо (этапы 2 и 3) по-прежнему
собирает compose.py: LOCAL.md, раздел «Рассылка».
"""
import contextlib
import io
import math
import os
import re
from datetime import datetime, timezone

import compose
import mailer
from mailing import config as mailcfg, journal, mailboxes as boxes
from mailing.lock import lock_holder
from mailing.table import Letter, LettersError

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
        """Хотя бы один ящик подключён полностью (адрес и пароль для внешних приложений)."""
        return any(box.ready for box in boxes.load_mailboxes())

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
        configured = boxes.load_mailboxes()
        if not any(box.ready for box in configured):
            raise SendError(400, "Ящик не настроен: впишите MAILRU_USER и MAILRU_APP_PASSWORD в local.env.")
        issues = boxes.problems(configured)
        if issues:
            raise SendError(400, " ".join(issues))
        holder = lock_holder(self.data_dir)
        if holder:
            raise SendError(409, f"Отправка уже идёт (процесс {holder}). Дождитесь окончания.")

    def mailbox_infos(self, selected=None, now=None) -> list:
        """Подключённые ящики и что каждый может отправить сегодня: для страниц /leads и /mail. Паролей нет."""
        now = now or datetime.now(timezone.utc)
        settings, ceiling = mailcfg.load_settings(self.settings_path), self._daily()
        chosen = None if selected is None else {box.id for box in selected}
        infos = []
        for box in boxes.load_mailboxes():
            if not box.ready:
                continue
            state = boxes.mailbox_state(self.data_dir, box.id, settings, ceiling, now)
            infos.append({"id": box.id, "label": box.label, "address": box.user, "first_message": state["first_message"] or "",
                          "source": state["source"], "day": state["day"], "quota": state["quota"],
                          "sent_today": state["sent_today"], "sent_24h": state["sent_24h"], "ceiling": ceiling,
                          "room": None if state["room"] == math.inf else state["room"], "text": boxes.describe_state(state),
                          "selected": chosen is None or box.id in chosen})
        return infos

    def _select(self, mailbox_count, only_mailbox):
        try:
            return boxes.select(boxes.load_mailboxes(), mailbox_count, only_mailbox or None)
        except ValueError as exc:
            raise SendError(400, str(exc)) from exc

    def prepare(self, leads_path: str, count, include_unwritten: bool = False, mailbox_count=None, only_mailbox: str = "") -> dict:
        """Собирает письма (файл letters_1.csv) и рассказывает, что уйдёт: числа, группы, по примеру письма на группу,
        и с каких ящиков сколько писем уйдёт сегодня (прогрев: у нового ящика на сегодня только его квота)."""
        number = self._check_count(count)
        self._check_ready(leads_path)
        selected = self._select(mailbox_count, only_mailbox)
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
        infos = [info for info in self.mailbox_infos(selected) if info["selected"]]
        rooms = {info["id"]: math.inf if info["room"] is None else info["room"] for info in infos}
        plan, _ = boxes.assign([Letter(f"{n}@x", "", "") for n in range(letters)], rooms)       # кому сколько уйдёт сегодня
        planned = {info["id"]: sum(1 for _, own in plan if own == info["id"]) for info in infos}
        for info in infos:
            info["planned"] = planned[info["id"]]
        room = None if any(value == math.inf for value in rooms.values()) else sum(rooms.values())
        return {"letters": letters, "summary": summary, "examples": examples, "daily_limit": daily,
                "sent_24h": used, "room": room, "today": len(plan), "mailboxes": infos}

    def start(self, leads_path: str, count, include_unwritten: bool = False, expected=None, mailbox_count=None,
              only_mailbox: str = "") -> dict:
        """Собирает письма заново и запускает отправку в фоне. expected - сколько писем показали пользователю:
        если таблица за это время изменилась, отправка не начинается."""
        info = self.prepare(leads_path, count, include_unwritten, mailbox_count, only_mailbox)
        if info["letters"] == 0:
            raise SendError(400, "Писем для отправки нет: " + "; ".join(info["summary"]))
        if expected is not None and info["letters"] != expected:
            raise SendError(409, f"Пока вы смотрели, набор писем изменился: теперь их {info['letters']}, а не {expected}. "
                                 "Нажмите «Отправить» ещё раз и проверьте.")
        if info["today"] == 0:
            raise SendError(400, "Сегодня отправлять нечего: квота всех выбранных ящиков на сегодня исчерпана ("
                                 + "; ".join(f"{box['label']}: {box['text']}" for box in info["mailboxes"])
                                 + "). Нажмите «Отправить» завтра.")
        argv = [self.letters_path, "--send", "--background", "--limit", str(info["letters"])]
        if mailbox_count is not None:
            argv += ["--mailboxes", str(int(mailbox_count))]
        if only_mailbox:
            argv += ["--mailbox", str(only_mailbox)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self._mailer(argv)
        if code != 0:
            raise SendError(400, out.getvalue().strip() or "Отправку запустить не удалось.")
        split = ", ".join(f"{box['label']} - {box['planned']}" for box in info["mailboxes"] if box["planned"])
        later = f" Остальные {info['letters'] - info['today']} уйдут в следующие дни той же кнопкой." \
            if info["today"] < info["letters"] else ""
        return {"started": True, "letters": info["letters"], "today": info["today"], "daily_limit": info["daily_limit"],
                "mailboxes": info["mailboxes"],
                "message": f"Отправка запущена в фоне: сегодня уйдёт {info['today']} из {info['letters']} ({split}), письма идут "
                           f"по очереди с паузой 30-75 с.{later} Страницу можно закрыть: отправка идёт сама, а дневной "
                           "лимит, прогрев ящиков и журнал не дадут отправить лишнего."}

    def status(self) -> dict:
        """Идёт ли отправка и сводка по журналу (без адресов) и по ящикам."""
        running = bool(lock_holder(self.data_dir))
        lines = []
        if os.path.exists(self.letters_path):
            try:
                lines = mailer.status_lines(self.letters_path, self._daily())
            except LettersError:
                lines = []
        return {"running": running, "lines": lines, "ready": self.mail_ready(), "mailboxes": self.mailbox_infos()}
