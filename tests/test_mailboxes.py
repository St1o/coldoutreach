"""Несколько ящиков: подключение, прогрев (7 писем в первый день, потом на 3 больше), раздача по очереди, паузы."""
import contextlib
import io
import math
import os
import random
import smtplib
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import mailer
from mailing import config as cfg, journal
from mailing import mailboxes as boxes
from mailing.mailboxes import Mailbox
from mailing.smtp import MailruSender
from mailing.table import Letter
from tests.test_mailer import FakeServer, TempTable, table

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)          # 12:00 по Москве, 12 октября
FIRST, SECOND = "first@mail.ru", "second@mail.ru"
PW1, PW2 = "PASSWORD-ONE-111", "PASSWORD-TWO-222"


def two_boxes():
    return [Mailbox("1", FIRST, PW1, "Пётр"), Mailbox("2", SECOND, PW2, "Пётр")]


def letters(count, prefix="u"):
    return [Letter(f"{prefix}{i}@x.ru", "Тема", "Текст") for i in range(count)]


def sent_entry(day_utc, mailbox=None, hour=9):
    entry = {"status": "sent", "time": f"{day_utc}T{hour:02d}:00:00+00:00", "note": "", "subject": "Т"}
    if mailbox:
        entry["mailbox"] = mailbox
    return entry


class LoadMailboxesTest(unittest.TestCase):
    def test_numbered_variables_make_numbered_mailboxes(self):
        env = {"MAILRU_USER": FIRST, "MAILRU_APP_PASSWORD": PW1, "MAIL_FROM_NAME": "Пётр",
               "MAILRU_USER_2": SECOND, "MAILRU_APP_PASSWORD_2": PW2}
        found = boxes.load_mailboxes(env)
        self.assertEqual([(b.id, b.user, b.ready) for b in found], [("1", FIRST, True), ("2", SECOND, True)])
        self.assertEqual(found[1].from_name, "Пётр")                        # имя второго по умолчанию как у первого
        env["MAIL_FROM_NAME_2"] = "Анна"
        self.assertEqual(boxes.load_mailboxes(env)[1].from_name, "Анна")

    def test_password_is_never_in_repr_or_label(self):
        box = boxes.load_mailboxes({"MAILRU_USER_2": SECOND, "MAILRU_APP_PASSWORD_2": PW2})[0]
        self.assertEqual(box.id, "2")                                       # одного второго ящика достаточно
        self.assertNotIn(PW2, repr(box) + box.label)

    def test_nothing_configured_means_no_mailboxes(self):
        self.assertEqual(boxes.load_mailboxes({}), [])
        self.assertEqual(boxes.load_mailboxes({"MAILRU_USER": " ", "MAILRU_APP_PASSWORD": ""}), [])

    def test_half_filled_and_duplicate_mailboxes_are_reported_without_secrets(self):
        half = boxes.load_mailboxes({"MAILRU_USER": FIRST, "MAILRU_APP_PASSWORD": PW1, "MAILRU_USER_2": SECOND})
        text = " ".join(boxes.problems(half))
        self.assertIn("MAILRU_APP_PASSWORD_2", text)
        self.assertNotIn(PW1, text)
        same = boxes.load_mailboxes({"MAILRU_USER": FIRST, "MAILRU_APP_PASSWORD": PW1,
                                     "MAILRU_USER_2": FIRST.upper(), "MAILRU_APP_PASSWORD_2": PW2})
        self.assertIn("с одним адресом", " ".join(boxes.problems(same)))
        self.assertEqual(boxes.problems(two_boxes()), [])
        self.assertIn("полный адрес", " ".join(boxes.problems([Mailbox("2", "не адрес", PW2)])))


class SelectTest(unittest.TestCase):
    def test_default_is_all_ready_mailboxes(self):
        self.assertEqual([b.id for b in boxes.select(two_boxes())], ["1", "2"])

    def test_count_takes_the_first_n_and_more_than_exist_means_all(self):
        self.assertEqual([b.id for b in boxes.select(two_boxes(), count=1)], ["1"])
        self.assertEqual([b.id for b in boxes.select(two_boxes(), count=5)], ["1", "2"])

    def test_only_picks_exact_mailboxes(self):
        self.assertEqual([b.id for b in boxes.select(two_boxes(), only="2")], ["2"])
        self.assertEqual([b.id for b in boxes.select(two_boxes(), only="2, 1")], ["1", "2"])

    def test_wrong_requests_are_errors_in_plain_words(self):
        for kwargs in ({"count": 0}, {"count": "много"}, {"only": "3"}, {"only": ","}, {"count": 1, "only": "2"}):
            with self.assertRaises(ValueError, msg=kwargs):
                boxes.select(two_boxes(), **kwargs)
        with self.assertRaises(ValueError):
            boxes.select([])
        with self.assertRaises(ValueError):
            boxes.select([Mailbox("1", FIRST, "")])                         # без пароля ящик не готов


class WarmupQuotaTest(unittest.TestCase):
    def test_seven_on_the_first_day_then_three_more_every_day_until_the_week_cap(self):
        self.assertEqual([boxes.warmup_quota(day) for day in range(1, 8)], [7, 10, 13, 15, 15, 15, 15])
        self.assertEqual(boxes.warmup_quota(8), 28)                        # новая неделя: 28 = 7 + 3 * 7, под пределом 30

    def test_never_above_the_daily_limit_and_zero_before_the_first_day(self):
        self.assertEqual([boxes.warmup_quota(day, 12) for day in (1, 2, 3, 4, 9)], [7, 10, 12, 12, 12])
        self.assertEqual(boxes.warmup_quota(0, 40), 0)
        self.assertEqual(boxes.warmup_quota(-3), 0)

    def test_day_number_counts_from_the_first_message(self):
        today = date(2026, 10, 12)
        self.assertEqual(boxes.warmup_day(date(2026, 10, 12), today), 1)
        self.assertEqual(boxes.warmup_day(date(2026, 10, 10), today), 3)
        self.assertEqual(boxes.warmup_day(None, today), 1)                  # писем ещё не было: первый день - сегодня
        self.assertEqual(boxes.warmup_day(date(2026, 10, 14), today), -1)   # дата в будущем


class WeekCapTest(unittest.TestCase):
    """Жёсткий предел по неделям прогрева: дни 1-7 не больше 15, дни 8-14 не больше 30, с дня 15 не больше 40."""

    def test_the_values_are_named_constants_next_to_the_ramp(self):
        self.assertEqual((boxes.WARMUP_FIRST_DAY, boxes.WARMUP_STEP), (7, 3))
        self.assertEqual(boxes.WARMUP_WEEK_CAPS, ((7, 15), (14, 30)))
        self.assertEqual(boxes.WARMUP_CAP_LATER, 40)

    def test_cap_and_week_number_by_day(self):
        days = (0, -2, 1, 7, 8, 14, 15, 16, 99)
        self.assertEqual([boxes.week_cap(day) for day in days], [0, 0, 15, 15, 30, 30, 40, 40, 40])
        self.assertEqual([boxes.week_number(day) for day in days], [0, 0, 1, 1, 2, 2, 3, 3, 3])

    def test_a_cap_beats_the_ramp_exactly_where_the_ramp_would_overshoot(self):
        self.assertEqual(boxes.warmup_quota(3), 13)                          # ещё под пределом 15: рост как был
        self.assertEqual(boxes.warmup_quota(4), 15)                          # 16 > 15
        self.assertEqual(boxes.warmup_quota(7), 15)                          # 25 > 15
        self.assertEqual(boxes.warmup_quota(8), 28)                          # 28 <= 30
        self.assertEqual(boxes.warmup_quota(9), 30)                          # 31 > 30
        self.assertEqual(boxes.warmup_quota(14), 30)                         # 46 > 30
        self.assertEqual(boxes.warmup_quota(15), 40)                         # 49 > 40
        self.assertEqual(boxes.warmup_quota(300), 40)

    def test_the_setting_on_the_site_can_only_lower_the_cap_never_raise_it(self):
        self.assertEqual(boxes.warmup_quota(5, 10), 10)                      # ниже предела недели 15: действует он
        self.assertEqual(boxes.warmup_quota(5, 50), 15)                      # выше предела: поднять его нельзя
        self.assertEqual(boxes.warmup_quota(10, 50), 30)
        self.assertEqual(boxes.warmup_quota(20, 50), 40)
        self.assertEqual(boxes.warmup_quota(20, 25), 25)
        self.assertEqual(boxes.warmup_quota(10, 0), 30)                      # 0 - «без лимита» на /mail, предел недели остаётся

    def test_the_site_cannot_set_a_limit_above_50(self):
        self.assertEqual((cfg.DAILY_LIMIT_MIN, cfg.DAILY_LIMIT_MAX), (1, 50))
        self.assertEqual(cfg.parse_daily_limit(50), 50)
        for bad in (51, 200, 0, -1):
            with self.assertRaises(ValueError, msg=bad):
                cfg.parse_daily_limit(bad)


class MailboxStateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def journal(self, name, entries):
        journal.save_state(os.path.join(self.dir.name, name), entries)

    def state(self, mailbox="2", settings=None, ceiling=40, **kw):
        return boxes.mailbox_state(self.dir.name, mailbox, settings or {}, ceiling, NOW, **kw)

    def test_a_new_mailbox_may_send_seven_today(self):
        state = self.state()
        self.assertEqual((state["day"], state["quota"], state["sent_today"], state["room"]), (1, 7, 0, 7))
        self.assertIsNone(state["first_message"])
        self.assertEqual(state["source"], boxes.SOURCE_NONE)

    def test_letters_sent_today_use_up_the_quota(self):
        self.journal("letters_1.sent.json", {f"s{i}@x.ru": sent_entry("2026-10-12", "2") for i in range(5)})
        state = self.state()
        self.assertEqual((state["day"], state["sent_today"], state["room"]), (1, 5, 2))
        self.assertEqual(state["source"], boxes.SOURCE_JOURNAL)

    def test_the_next_day_allows_three_more(self):
        self.journal("letters_1.sent.json", {f"s{i}@x.ru": sent_entry("2026-10-11", "2", hour=10) for i in range(7)})
        state = self.state()
        self.assertEqual((state["day"], state["quota"], state["sent_today"], state["first_message"]), (2, 10, 0, "2026-10-11"))
        self.assertEqual(state["room"], 10)

    def test_each_mailbox_is_counted_on_its_own_and_old_records_belong_to_the_first(self):
        self.journal("letters_1.sent.json", {
            "a@x.ru": sent_entry("2026-10-12"),                 # без поля mailbox: ящик 1
            "b@x.ru": sent_entry("2026-10-12", "1"),
            "c@x.ru": sent_entry("2026-10-12", "2")})
        self.assertEqual(self.state("1")["sent_today"], 2)
        self.assertEqual(self.state("2")["sent_today"], 1)

    def test_a_day_is_a_moscow_calendar_day(self):
        self.journal("letters_1.sent.json", {"a@x.ru": sent_entry("2026-10-11", "2", hour=21)})   # 00:00 12 октября по Москве
        self.assertEqual(self.state()["day"], 1)
        self.assertEqual(self.state()["first_message"], "2026-10-12")

    def test_the_date_set_on_the_site_beats_the_journal(self):
        self.journal("letters_1.sent.json", {"a@x.ru": sent_entry("2026-10-11", "2")})
        state = self.state(settings={"mailboxes": {"2": {"first_message": "2026-10-08"}}})
        self.assertEqual((state["day"], state["quota"], state["source"]), (5, 15, boxes.SOURCE_SETTING))   # 19 по росту, 15 по неделе

    def test_the_daily_limit_still_caps_the_quota_and_the_rolling_day(self):
        settings = {"mailboxes": {"2": {"first_message": "2026-09-01"}}}      # давно прогретый ящик
        self.assertEqual(self.state(settings=settings, ceiling=40)["room"], 40)
        self.journal("letters_1.sent.json", {f"s{i}@x.ru": sent_entry("2026-10-11", "2", hour=15) for i in range(38)})
        state = self.state(settings=settings, ceiling=40)                     # вчера 15:00 UTC: ещё в скользящих сутках
        self.assertEqual((state["sent_24h"], state["sent_today"], state["room"]), (38, 0, 2))

    def test_state_names_the_week_and_its_cap_for_every_stage_of_the_warmup(self):
        def at(days_ago, **kw):
            first = (date(2026, 10, 12) - timedelta(days=days_ago)).isoformat()
            return self.state(settings={"mailboxes": {"2": {"first_message": first}}}, **kw)

        self.assertEqual([(at(d)["day"], at(d)["week"], at(d)["week_cap"]) for d in (0, 6, 7, 13, 14, 60)],
                         [(1, 1, 15), (7, 1, 15), (8, 2, 30), (14, 2, 30), (15, 3, 40), (61, 3, 40)])
        state = at(8, ceiling=50)                                         # 9-й день: рост даёт 31, предел недели 30
        self.assertEqual((state["quota"], state["room"]), (30, 30))
        state = at(8, ceiling=20)                                         # потолок с /mail ниже предела: действует он
        self.assertEqual((state["quota"], state["room"]), (20, 20))
        self.assertEqual((self.state("1", warmup=False)["week"], self.state("1", warmup=False)["week_cap"]), (None, None))

    def test_a_future_date_means_no_letters_until_it_comes(self):
        state = self.state(settings={"mailboxes": {"2": {"first_message": "2026-10-14"}}})
        self.assertEqual((state["day"], state["quota"], state["room"]), (-1, 0, 0))
        self.assertEqual((state["week"], state["week_cap"]), (0, 0))
        self.assertIn("прогрев начнётся 2026-10-14", boxes.describe_state(state))

    def test_without_warmup_only_the_daily_limit_counts(self):
        state = self.state("1", warmup=False)
        self.assertEqual(state["room"], 40)
        self.assertIsNone(state["quota"])
        self.assertEqual(self.state("1", ceiling=0, warmup=False)["room"], math.inf)
        self.assertIn("можно ещё 40", boxes.describe_state(state))

    def test_broken_settings_are_ignored(self):
        for bad in ({"mailboxes": 5}, {"mailboxes": {"2": "x"}}, {"mailboxes": {"2": {"first_message": "вчера"}}}):
            self.assertEqual(self.state(settings=bad)["day"], 1, bad)

    def test_description_has_no_secrets_and_names_the_numbers(self):
        text = boxes.describe_state(self.state())
        self.assertIn("день прогрева 1", text)
        self.assertIn("на сегодня можно 7", text)
        self.assertIn("осталось 7", text)
        self.assertIn("неделя 1, жёсткий предел недели 15", text)

    def test_description_shows_which_week_cap_acts(self):
        def text(days_ago):
            first = (date(2026, 10, 12) - timedelta(days=days_ago)).isoformat()
            return boxes.describe_state(self.state(settings={"mailboxes": {"2": {"first_message": first}}}))

        self.assertIn("день прогрева 5 (неделя 1, жёсткий предел недели 15), на сегодня можно 15", text(4))
        self.assertIn("день прогрева 10 (неделя 2, жёсткий предел недели 30), на сегодня можно 30", text(9))
        self.assertIn("день прогрева 20 (неделя 3 и дальше, жёсткий предел недели 40), на сегодня можно 40", text(19))


class JournalOfAMissingFolderTest(unittest.TestCase):
    def test_a_folder_that_does_not_exist_yet_means_no_letters(self):
        missing = os.path.join(tempfile.gettempdir(), "нет-такой-папки-данных")
        self.assertEqual(journal.sent_last_24h(missing, NOW, "1"), 0)
        self.assertIsNone(journal.first_sent_day(missing, "1"))
        self.assertEqual(boxes.mailbox_state(missing, "2", {}, 40, NOW)["room"], 7)


class SetFirstMessageTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "settings.json")

    def test_date_is_saved_next_to_the_other_settings_and_can_be_cleared(self):
        cfg.save_settings({cfg.DAILY_LIMIT_KEY: 25}, self.path)
        self.assertEqual(boxes.set_first_message(self.path, "2", " 2026-10-12 "), date(2026, 10, 12))
        self.assertEqual(cfg.load_settings(self.path),
                         {cfg.DAILY_LIMIT_KEY: 25, "mailboxes": {"2": {"first_message": "2026-10-12"}}})
        self.assertIsNone(boxes.set_first_message(self.path, "2", ""))
        self.assertEqual(cfg.load_settings(self.path), {cfg.DAILY_LIMIT_KEY: 25, "mailboxes": {}})

    def test_bad_date_is_an_error_and_changes_nothing(self):
        for bad in ("завтра", "2026-13-01", "12.10.2026"):
            with self.assertRaises(ValueError, msg=bad):
                boxes.set_first_message(self.path, "2", bad)
        self.assertFalse(os.path.exists(self.path))


class AssignTest(unittest.TestCase):
    def ids(self, plan):
        return [own for _, own in plan]

    def test_letters_go_to_the_mailboxes_in_turn(self):
        plan, waiting = boxes.assign(letters(5), {"1": 10, "2": 10})
        self.assertEqual(self.ids(plan), ["1", "2", "1", "2", "1"])
        self.assertEqual(len(waiting), 0)

    def test_a_full_mailbox_is_skipped_and_the_other_carries_on(self):
        plan, _ = boxes.assign(letters(7), {"1": 1, "2": 10})
        self.assertEqual(self.ids(plan), ["1", "2", "2", "2", "2", "2", "2"])

    def test_the_new_mailbox_gets_no_more_than_its_room(self):
        plan, _ = boxes.assign(letters(30), {"1": 20, "2": 7})
        self.assertEqual((self.ids(plan).count("1"), self.ids(plan).count("2")), (20, 7))
        self.assertEqual(len(plan), 27)                                       # остальное - в следующие дни

    def test_limit_counts_letters_not_mailboxes(self):
        plan, _ = boxes.assign(letters(30), {"1": 20, "2": 20}, limit=5)
        self.assertEqual(self.ids(plan), ["1", "2", "1", "2", "1"])

    def test_unlimited_room_works_like_any_other(self):
        plan, _ = boxes.assign(letters(4), {"1": math.inf, "2": math.inf})
        self.assertEqual(self.ids(plan), ["1", "2", "1", "2"])

    def test_nothing_goes_when_there_is_no_room_anywhere(self):
        self.assertEqual(boxes.assign(letters(3), {"1": 0, "2": 0})[0], [])

    def test_a_follow_up_goes_from_the_same_mailbox_or_waits(self):
        batch = letters(4)
        preferred = {"u0@x.ru": "2", "u1@x.ru": "2", "u2@x.ru": "1"}
        plan, waiting = boxes.assign(batch, {"1": 5, "2": 1}, preferred)
        self.assertEqual([(l.email, own) for l, own in plan], [("u0@x.ru", "2"), ("u2@x.ru", "1"), ("u3@x.ru", "1")])
        self.assertEqual(dict(waiting), {"2": 1})                             # у ящика 2 место кончилось: u1 ждёт
        plan, waiting = boxes.assign(batch, {"1": 5}, preferred)              # ящик 2 не выбран
        self.assertEqual([l.email for l, _ in plan], ["u2@x.ru", "u3@x.ru"])
        self.assertEqual(dict(waiting), {"2": 2})


class RunWithMailboxesTest(unittest.TestCase):
    def setUp(self):
        self.logs, self.sleeps = [], []
        self.servers = {"1": FakeServer(), "2": FakeServer()}

    def senders(self):
        return {"1": MailruSender(FIRST, PW1, connect=lambda *a: self.servers["1"]),
                "2": MailruSender(SECOND, PW2, connect=lambda *a: self.servers["2"])}

    def table(self, count, name="letters_1.csv", directory=None):
        directory = directory or tempfile.TemporaryDirectory()
        if not isinstance(directory, str):
            self.addCleanup(directory.cleanup)
            directory = directory.name
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(table(*[f"u{i}@x.ru;Тема {i};Текст" for i in range(count)]))
        self.path, self.folder = path, directory
        return path

    def run_batch(self, count=6, name="letters_1.csv", selected=None, new=True, **kw):
        path = self.table(count, name) if new else self.path
        args = dict(send=True, limit=0, pause=30, log=self.logs.append, sleep=self.sleeps.append, now=NOW,
                    mailboxes=selected or two_boxes(), senders=self.senders(), daily_limit=40,
                    settings_path=os.path.join(self.folder, "нет.json"))
        args.update(kw)
        return mailer.run(path, **args)

    def output(self):
        return "\n".join(self.logs)

    def test_two_mailboxes_send_in_turn_with_a_pause_between_every_two_letters(self):
        self.assertEqual(self.run_batch(6), 0)
        self.assertEqual([m["To"] for m in self.servers["1"].sent], ["u0@x.ru", "u2@x.ru", "u4@x.ru"])
        self.assertEqual([m["To"] for m in self.servers["2"].sent], ["u1@x.ru", "u3@x.ru", "u5@x.ru"])
        self.assertEqual(self.sleeps, [30] * 5)                        # между письмами, не перед первым и не после последнего
        self.assertEqual(self.servers["1"].logins, [(FIRST, PW1)])
        self.assertEqual(self.servers["2"].logins, [(SECOND, PW2)])
        self.assertTrue(self.servers["1"].quit_called and self.servers["2"].quit_called)
        self.assertIn("ящик 1 - 3, ящик 2 - 3", self.output())

    def test_the_sender_address_of_each_letter_is_its_own_mailbox(self):
        self.run_batch(2, unsubscribe=True)
        for number, address in (("1", FIRST), ("2", SECOND)):
            msg = self.servers[number].sent[0]
            self.assertIn(address, msg["From"])
            self.assertEqual(msg["List-Unsubscribe"], f"<mailto:{address}?subject=unsubscribe>")

    def test_the_journal_remembers_the_mailbox(self):
        self.run_batch(4)
        state = journal.load_state(journal.state_path(self.path))
        self.assertEqual({address: entry["mailbox"] for address, entry in state.items()},
                         {"u0@x.ru": "1", "u1@x.ru": "2", "u2@x.ru": "1", "u3@x.ru": "2"})

    def test_pauses_are_random_between_pause_and_pause_times_one_plus_jitter(self):
        self.run_batch(8, jitter=1.5, rng=random.Random(7))
        self.assertEqual(len(self.sleeps), 7)
        self.assertTrue(all(30 <= s <= 75 for s in self.sleeps), self.sleeps)
        self.assertGreater(len(set(self.sleeps)), 3)                    # не одно и то же число

    def test_the_new_mailbox_stops_at_its_first_day_quota_and_the_old_one_carries_on(self):
        self.table(30)
        settings = os.path.join(self.folder, "settings.json")                # ящик 1 давно в работе: ему 40 в сутки
        cfg.save_settings({"mailboxes": {"1": {"first_message": "2026-09-01"}}}, settings)
        self.run_batch(0, new=False, settings_path=settings)
        self.assertEqual(len(self.servers["2"].sent), 7)                 # новый ящик: 7 в первый день
        self.assertEqual(len(self.servers["1"].sent), 23)                # остальное - первый ящик
        self.assertIn("Ящик 2: день прогрева 1 (неделя 1, жёсткий предел недели 15), на сегодня можно 7", self.output())

    def test_the_week_cap_stops_a_mailbox_even_when_the_ramp_and_the_site_limit_would_allow_more(self):
        self.table(30)
        settings = os.path.join(self.folder, "settings.json")
        first = (boxes.today_msk() - timedelta(days=5)).isoformat()                              # 6-й день: рост 22, предел 15
        cfg.save_settings({"mailboxes": {"1": {"first_message": first}}}, settings)
        # настоящее «сейчас» (now=None): журнал пишет настоящее время, и второй запуск должен видеть письма первого
        self.run_batch(0, new=False, selected=[two_boxes()[0]], settings_path=settings, daily_limit=40, now=None)
        self.assertEqual(len(self.servers["1"].sent), 15)
        self.assertIn("жёсткий предел недели 15", self.output())
        self.assertEqual(len(journal.load_state(journal.state_path(self.path))), 15)            # остальные ждут следующих дней
        self.logs.clear()
        self.run_batch(0, new=False, selected=[two_boxes()[0]], settings_path=settings, daily_limit=40, now=None)
        self.assertEqual(len(self.servers["1"].sent), 15)                                       # второй запуск в тот же день: ничего
        self.assertIn("Отправлять нечего", self.output())

    def test_nothing_goes_when_every_quota_is_used_up(self):
        self.table(5)
        journal.save_state(os.path.join(self.folder, "letters_8.sent.json"),
                          {f"s{i}-{own}@y.ru": sent_entry("2026-10-12", own) for i in range(7) for own in ("1", "2")})
        self.run_batch(0, new=False)
        self.assertEqual((self.servers["1"].sent, self.servers["2"].sent), ([], []))
        self.assertIn("Квота ящиков на сегодня исчерпана", self.output())
        self.assertIn("Отправлять нечего", self.output())

    def test_one_selected_mailbox_does_all_the_work_within_its_own_quota(self):
        self.run_batch(10, selected=[two_boxes()[1]])
        self.assertEqual(self.servers["1"].sent, [])
        self.assertEqual(len(self.servers["2"].sent), 7)
        self.assertNotIn("ящик 1", self.output())

    def test_dry_run_shows_the_split_and_sends_nothing(self):
        self.run_batch(4, send=False)
        text = self.output()
        self.assertIn("[ящик 1] u0@x.ru", text)
        self.assertIn("[ящик 2] u1@x.ru", text)
        self.assertEqual((self.servers["1"].logins, self.servers["2"].logins), ([], []))
        self.assertFalse(os.path.exists(journal.state_path(self.path)))

    def test_authentication_failure_names_the_mailbox_and_stops_everything(self):
        class BadLogin(FakeServer):
            def login(self, user, password):
                raise smtplib.SMTPAuthenticationError(535, b"Application password is REQUIRED")

        self.servers["2"] = BadLogin()
        self.assertEqual(self.run_batch(4), 2)
        self.assertIn("не принял логин/пароль (ящик 2)", self.output())
        self.assertEqual([m["To"] for m in self.servers["1"].sent], ["u0@x.ru"])
        self.assertEqual(list(journal.load_state(journal.state_path(self.path))), ["u0@x.ru"])

    def test_rate_limit_stops_at_once_and_names_the_mailbox(self):
        self.servers["1"] = FakeServer(fail={"u2@x.ru": smtplib.SMTPDataError(451, b"Ratelimit exceeded")})
        self.assertEqual(self.run_batch(6), 3)
        self.assertIn("(ящик 1)", self.output())
        self.assertEqual(len(self.servers["2"].sent), 1)

    def test_masked_run_has_no_addresses_and_no_passwords(self):
        self.run_batch(4, mask=True)
        text = self.output()
        for secret in ("u0@x.ru", FIRST, SECOND, PW1, PW2):
            self.assertNotIn(secret, text)
        self.assertIn("u***@x.ru: отправлено (ящик 1)", text)

    def test_follow_ups_go_from_the_mailbox_that_sent_the_first_letter(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        journal.save_state(os.path.join(folder.name, "letters_1.sent.json"), {
            "u0@x.ru": sent_entry("2026-10-05", "2"), "u1@x.ru": sent_entry("2026-10-05", "2"),
            "u2@x.ru": sent_entry("2026-10-05", "1"), "u3@x.ru": sent_entry("2026-10-05")})   # без поля: ящик 1
        self.table(4, "letters_2.csv", folder.name)
        self.run_batch(0, new=False)
        self.assertEqual(sorted(m["To"] for m in self.servers["1"].sent), ["u2@x.ru", "u3@x.ru"])
        self.assertEqual(sorted(m["To"] for m in self.servers["2"].sent), ["u0@x.ru", "u1@x.ru"])

    def test_a_follow_up_waits_when_its_mailbox_is_not_selected_for_this_run(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        journal.save_state(os.path.join(folder.name, "letters_1.sent.json"),
                           {"u0@x.ru": sent_entry("2026-10-05", "2"), "u1@x.ru": sent_entry("2026-10-05", "1")})
        self.table(2, "letters_2.csv", folder.name)
        self.run_batch(0, new=False, selected=[two_boxes()[0]], configured=two_boxes())
        self.assertEqual([m["To"] for m in self.servers["1"].sent], ["u1@x.ru"])
        self.assertIn("ждут своего ящика 2: 1", self.output())
        self.assertEqual(self.servers["2"].sent, [])

    def test_the_first_letters_table_has_no_preference(self):
        self.run_batch(2, name="letters_1.csv")
        self.assertEqual((len(self.servers["1"].sent), len(self.servers["2"].sent)), (1, 1))

    def test_missing_password_of_a_selected_mailbox_stops_before_any_letter(self):
        self.table(2)
        broken = [Mailbox("1", FIRST, PW1), Mailbox("2", SECOND, "")]
        self.assertEqual(self.run_batch(0, new=False, selected=broken, senders=None), 1)
        self.assertIn("ящик 2: не заданы", self.output())
        self.assertNotIn(PW1, self.output())


class MainWithMailboxesTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in list(os.environ):
            if key.startswith(("MAILRU_", "MAIL_")):
                os.environ.pop(key)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.table = TempTable(table("a@b.ru;Т;Х", "c@b.ru;Т;Х", "d@b.ru;Т;Х"))
        self.addCleanup(self.table.close)
        self.env = os.path.join(self.dir.name, "local.env")
        self.settings = os.path.join(self.dir.name, "нет.json")

    def write_env(self, second=True, extra=""):
        text = f"MAILRU_USER={FIRST}\nMAILRU_APP_PASSWORD={PW1}\nMAIL_FROM_NAME=Пётр\n"
        if second:
            text += f"MAILRU_USER_2={SECOND}\nMAILRU_APP_PASSWORD_2={PW2}\n"
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write(text + extra)

    def main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mailer.main([self.table.path, "--env", self.env, "--settings", self.settings, *args])
        return code, out.getvalue()

    def run_kwargs(self, *args):
        with mock.patch.object(mailer, "run", return_value=0) as fake_run:
            self.assertEqual(self.main(*args)[0], 0)
        return fake_run.call_args.kwargs

    def test_all_mailboxes_by_default_and_the_pause_is_random_by_default(self):
        self.write_env()
        kwargs = self.run_kwargs("--send")
        self.assertEqual([b.id for b in kwargs["mailboxes"]], ["1", "2"])
        self.assertEqual(kwargs["jitter"], mailer.DEFAULT_JITTER)
        self.assertEqual(self.run_kwargs("--send", "--jitter", "0")["jitter"], 0)

    def test_count_and_only_choose_mailboxes(self):
        self.write_env()
        self.assertEqual([b.id for b in self.run_kwargs("--send", "--mailboxes", "1")["mailboxes"]], ["1"])
        self.assertEqual([b.id for b in self.run_kwargs("--send", "--mailbox", "2")["mailboxes"]], ["2"])
        self.assertEqual([b.id for b in self.run_kwargs("--send", "--mailboxes", "9")["mailboxes"]], ["1", "2"])

    def test_wrong_choices_are_refused_before_anything_is_sent(self):
        self.write_env()
        for args in (("--mailboxes", "0"), ("--mailbox", "3"), ("--mailboxes", "1", "--mailbox", "2")):
            with mock.patch.object(mailer, "run") as fake_run:
                code, text = self.main("--send", *args)
            self.assertEqual(code, 1, args)
            self.assertIn("Ошибка", text)
            fake_run.assert_not_called()

    def test_half_filled_second_mailbox_blocks_a_real_send_but_not_a_check(self):
        self.write_env(second=False, extra=f"MAILRU_USER_2={SECOND}\n")
        with mock.patch.object(mailer, "run") as fake_run:
            code, text = self.main("--send")
        self.assertEqual(code, 1)
        self.assertIn("MAILRU_APP_PASSWORD_2", text)
        fake_run.assert_not_called()
        self.assertEqual(self.main()[0], 0)

    def test_the_name_option_applies_to_every_selected_mailbox(self):
        self.write_env()
        kwargs = self.run_kwargs("--send", "--from-name", "Анна")
        self.assertEqual({b.from_name for b in kwargs["mailboxes"]}, {"Анна"})

    def test_status_shows_every_mailbox_with_its_warmup_and_no_secrets(self):
        self.write_env()
        code, text = self.main("--status")
        self.assertEqual(code, 0)
        self.assertIn("Ящик 1 (f***@mail.ru): день прогрева 1", text)
        self.assertIn("Ящик 2 (s***@mail.ru): день прогрева 1", text)
        self.assertEqual(text.count("жёсткий предел недели 15"), 2)
        for secret in (FIRST, SECOND, PW1, PW2):
            self.assertNotIn(secret, text)

    def test_status_of_a_data_folder_needs_no_table(self):
        self.write_env()
        folder = os.path.dirname(self.table.path)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mailer.main([folder, "--status", "--env", self.env, "--settings", self.settings])
        self.assertEqual(code, 0)
        self.assertIn("Ящик 2", out.getvalue())
        self.assertNotIn("Писем в таблице", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mailer.main([folder, "--env", self.env]), 1)       # без --status папку не принимаем

    def test_status_of_a_data_folder_shows_the_week_cap_of_each_mailbox(self):
        self.write_env()
        today = boxes.today_msk()
        cfg.save_settings({"mailboxes": {"1": {"first_message": (today - timedelta(days=10)).isoformat()},       # день 11
                                         "2": {"first_message": (today - timedelta(days=20)).isoformat()}}},     # день 21
                          self.settings)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mailer.main([os.path.dirname(self.table.path), "--status", "--env", self.env, "--settings", self.settings])
        lines = out.getvalue().splitlines()
        self.assertEqual(code, 0)
        self.assertIn("день прогрева 11 (неделя 2, жёсткий предел недели 30)", lines[0])
        self.assertIn("на сегодня можно 30", lines[0])
        self.assertIn("день прогрева 21 (неделя 3 и дальше, жёсткий предел недели 40)", lines[1])
        self.assertIn("на сегодня можно 40", lines[1])
        for secret in (FIRST, SECOND, PW1, PW2):
            self.assertNotIn(secret, out.getvalue())

    def test_background_start_passes_the_choice_on_to_the_sender(self):
        self.write_env()
        with mock.patch.object(mailer.subprocess, "Popen") as popen:
            popen.return_value.pid = 77
            code, text = self.main("--send", "--background", "--mailbox", "2", "--limit", "5")
        self.assertEqual(code, 0)
        command = popen.call_args.args[0]
        self.assertIn("--mailbox", command)
        self.assertIn("2", command)
        self.assertIn("30-75 с", text)
        self.assertNotIn(PW2, text)

    def test_real_send_goes_through_both_mailboxes(self):
        self.write_env()
        servers = {FIRST: FakeServer(), SECOND: FakeServer()}

        def sender(user, password, port=465, **kw):
            return MailruSender(user, password, connect=lambda *a: servers[user])

        with mock.patch.object(mailer, "MailruSender", sender):
            code, text = self.main("--send", "--limit", "0", "--pause", "0", "--mask-addresses")
        self.assertEqual(code, 0, text)
        self.assertEqual((len(servers[FIRST].sent), len(servers[SECOND].sent)), (2, 1))
        self.assertNotIn(PW1, text)
        self.assertNotIn(PW2, text)


if __name__ == "__main__":
    unittest.main()
