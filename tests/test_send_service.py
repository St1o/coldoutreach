import json
import os
import tempfile
import unittest
from unittest import mock

import compose
import send_service
from mailing import config as cfg, journal
from mailing.table import read_letters
from send_service import SendError, SendService
from tests.test_compose import HEADER, LETTERS, LINK, SEGMENT, SIGNATURE, lead

DAY = "leads_2026-10-11.csv"


class FakeMailer:
    """Вместо mailer.main: запоминает ключи запуска и печатает то, что напечатал бы настоящий."""

    def __init__(self, code=0, text="Отправка запущена в фоне (процесс 4242)."):
        self.calls, self.code, self.text = [], code, text

    def __call__(self, argv):
        self.calls.append(list(argv))
        print(self.text)
        return self.code


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, "data")
        agents = os.path.join(self.tmp.name, "agents")
        os.makedirs(self.data)
        os.makedirs(agents)
        self.segment, self.letters = os.path.join(agents, "segment.md"), os.path.join(agents, "letters.md")
        for path, text in ((self.segment, SEGMENT), (self.letters, LETTERS)):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        self.empty_env = os.path.join(self.tmp.name, "empty.env")
        open(self.empty_env, "w").close()                         # настоящий local.env тесты не читают
        self.leads = os.path.join(self.data, DAY)
        self.write_leads([lead("a@x.ru", "47.11"), lead("b@x.ru", "41.20"), lead("c@x.ru", "01.11"),
                          lead("d@x.ru", "62.01"), lead("e@x.ru", "47.19", source="контакты Checko")])
        env = {"MAILRU_USER": "sender@mail.ru", "MAILRU_APP_PASSWORD": "pw", "SURVEY_LINK": LINK,
               "MAIL_SIGNATURE": SIGNATURE, "OUTREACH_LETTERS": self.letters, "OUTREACH_SEGMENT": self.segment}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mailer = FakeMailer()
        self.settings = os.path.join(self.tmp.name, "none.json")
        self.service = SendService(self.data, settings_path=self.settings,
                                   compose_main=self.compose, mailer_main=self.mailer)

    def compose(self, argv, log=print):
        return compose.main(list(argv) + ["--env", self.empty_env], log=log)

    def write_leads(self, rows):
        import csv
        with open(self.leads, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(HEADER)
            writer.writerows(rows)

    @property
    def letters_file(self):
        return os.path.join(self.data, "letters_1.csv")


class PrepareTest(ServiceCase):
    def test_tells_what_will_go_out_without_addresses_or_names(self):
        info = self.service.prepare(self.leads, 10)
        self.assertEqual(info["letters"], 3)                       # торговля, стройка и запасная группа
        text = json.dumps(info, ensure_ascii=False)
        self.assertIn("не собрано: для группы 11", text)
        self.assertIn("почта не из ЕГРЮЛ", text)
        self.assertEqual(sorted(example["group"] for example in info["examples"]), ["1", "12", "3"])
        for secret in ("@x.ru", "СЕКРЕТНАЯ", "Иванов Иван"):
            self.assertNotIn(secret, text)
        self.assertNotIn("letters_1.csv", text)                    # путей и подсказок командной строки нет
        self.assertNotIn("--include-unwritten", text)
        self.assertIn("поставьте галочку", text)
        self.assertTrue(os.path.exists(self.letters_file))

    def test_count_limits_the_batch_and_unwritten_groups_are_optional(self):
        self.assertEqual(self.service.prepare(self.leads, 2)["letters"], 2)
        self.assertEqual(self.service.prepare(self.leads, 10, include_unwritten=True)["letters"], 4)

    def test_fallback_letters_are_named_in_the_summary_only_when_they_are_included(self):
        plain = json.dumps(self.service.prepare(self.leads, 10), ensure_ascii=False)
        self.assertNotIn("запасной вариант текста", plain)
        with_fallback = self.service.prepare(self.leads, 10, include_unwritten=True)
        self.assertEqual(sum("запасной вариант текста: для группы 11" in line for line in with_fallback["summary"]), 1)
        self.assertIn("писем 1", " ".join(with_fallback["summary"]))

    def test_daily_room_counts_what_was_sent_in_the_last_day(self):
        journal.save_state(os.path.join(self.data, "letters_1.sent.json"),
                           {"old@x.ru": journal.make_entry(journal.SENT, "Тема", {})})
        cfg.save_settings({"mailboxes": {"1": {"first_message": "2026-01-01"}}}, self.settings)   # ящик давно прогрет
        info = self.service.prepare(self.leads, 10)
        self.assertEqual((info["daily_limit"], info["sent_24h"], info["room"]), (40, 1, 39))

    def test_a_new_mailbox_has_only_its_first_day_quota_today(self):
        journal.save_state(os.path.join(self.data, "letters_1.sent.json"),
                           {"old@x.ru": journal.make_entry(journal.SENT, "Тема", {}, mailbox="1")})
        info = self.service.prepare(self.leads, 10)                 # первый день прогрева: 7, одно письмо уже ушло
        self.assertEqual((info["sent_24h"], info["room"]), (1, 6))
        self.assertEqual(info["today"], 3)

    def test_count_must_be_a_sensible_number(self):
        for bad in (0, -1, 201, "много", None, 2.5 + 0j):
            with self.assertRaises(SendError) as caught:
                self.service.prepare(self.leads, bad)
            self.assertEqual(caught.exception.status, 400, bad)

    def test_refuses_without_the_mailbox_a_table_in_the_data_folder_or_while_sending(self):
        with mock.patch.dict(os.environ, {"MAILRU_APP_PASSWORD": ""}):
            with self.assertRaises(SendError) as caught:
                self.service.prepare(self.leads, 5)
            self.assertIn("MAILRU_APP_PASSWORD", caught.exception.message)
        with self.assertRaises(SendError) as caught:
            self.service.prepare(os.path.join(self.tmp.name, DAY), 5)
        self.assertEqual(caught.exception.status, 400)
        with mock.patch.object(send_service, "lock_holder", return_value=4321):
            with self.assertRaises(SendError) as caught:
                self.service.prepare(self.leads, 5)
            self.assertEqual(caught.exception.status, 409)

    def test_compose_errors_reach_the_user(self):
        with mock.patch.dict(os.environ, {"SURVEY_LINK": ""}):
            with self.assertRaises(SendError) as caught:
                self.service.prepare(self.leads, 5)
        self.assertIn("ссылк", caught.exception.message)


class StartTest(ServiceCase):
    def test_composes_again_and_starts_the_background_sender_for_exactly_that_batch(self):
        info = self.service.start(self.leads, 3, expected=3)
        self.assertTrue(info["started"])
        self.assertEqual(info["letters"], 3)
        self.assertEqual(self.mailer.calls, [[self.letters_file, "--send", "--background", "--limit", "3"]])
        self.assertEqual(len(read_letters(self.letters_file)[0]), 3)

    def test_nothing_is_sent_if_the_batch_changed_after_the_user_looked(self):
        with self.assertRaises(SendError) as caught:
            self.service.start(self.leads, 3, expected=5)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.mailer.calls, [])

    def test_no_letters_means_no_start(self):
        self.write_leads([lead("e@x.ru", "47.19", source="контакты Checko")])
        with self.assertRaises(SendError) as caught:
            self.service.start(self.leads, 5)
        self.assertIn("Писем для отправки нет", caught.exception.message)
        self.assertEqual(self.mailer.calls, [])

    def test_a_failed_start_is_reported_as_is(self):
        self.service._mailer = FakeMailer(code=1, text="Ошибка: в этой папке уже идёт отправка (процесс 77).")
        with self.assertRaises(SendError) as caught:
            self.service.start(self.leads, 3)
        self.assertIn("уже идёт отправка", caught.exception.message)

    def test_expected_is_optional(self):
        self.assertTrue(self.service.start(self.leads, 2)["started"])
        self.assertEqual(self.mailer.calls[0][-1], "2")


class StatusTest(ServiceCase):
    def test_status_before_and_after_the_letters_are_composed(self):
        state = self.service.status()
        self.assertEqual((state["running"], state["lines"], state["ready"]), (False, [], True))
        self.service.prepare(self.leads, 10)
        state = self.service.status()
        self.assertIn("Писем в таблице: 3", state["lines"][0])
        self.assertIn("отправка не идёт", " ".join(state["lines"]))

    def test_running_follows_the_folder_lock(self):
        with mock.patch.object(send_service, "lock_holder", return_value=99):
            self.assertTrue(self.service.status()["running"])


SECOND = {"MAILRU_USER_2": "second@mail.ru", "MAILRU_APP_PASSWORD_2": "second-secret-pw"}


class TwoMailboxesTest(ServiceCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, SECOND)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fill_today(self, mailbox, count):
        path = os.path.join(self.data, "letters_9.sent.json")
        entries = journal.load_state(path)
        entries.update({f"s{i}-{mailbox}@y.ru": journal.make_entry(journal.SENT, "Т", {}, mailbox=mailbox)
                        for i in range(count)})
        journal.save_state(path, entries)

    def test_both_mailboxes_are_offered_and_share_the_batch_in_turn(self):
        info = self.service.prepare(self.leads, 10)
        self.assertEqual([(box["id"], box["day"], box["quota"], box["planned"]) for box in info["mailboxes"]],
                         [("1", 1, 7, 2), ("2", 1, 7, 1)])
        self.assertEqual((info["letters"], info["today"], info["room"]), (3, 3, 14))

    def test_the_count_field_chooses_how_many_mailboxes_send(self):
        info = self.service.prepare(self.leads, 10, mailbox_count=1)
        self.assertEqual([box["id"] for box in info["mailboxes"]], ["1"])
        self.assertEqual(info["mailboxes"][0]["planned"], 3)
        self.assertEqual(len(self.service.prepare(self.leads, 10, mailbox_count=2)["mailboxes"]), 2)

    def test_one_particular_mailbox_can_be_chosen(self):
        info = self.service.prepare(self.leads, 10, only_mailbox="2")
        self.assertEqual([box["id"] for box in info["mailboxes"]], ["2"])
        self.assertEqual(info["mailboxes"][0]["planned"], 3)

    def test_a_wrong_choice_is_a_plain_400(self):
        for kwargs in ({"mailbox_count": 0}, {"only_mailbox": "5"}, {"mailbox_count": 1, "only_mailbox": "2"}):
            with self.assertRaises(SendError, msg=kwargs) as caught:
                self.service.prepare(self.leads, 10, **kwargs)
            self.assertEqual(caught.exception.status, 400)

    def test_only_what_the_quota_allows_goes_today_and_the_rest_is_said_so(self):
        self.fill_today("1", 6)
        self.fill_today("2", 5)                                    # осталось 1 и 2 письма
        info = self.service.prepare(self.leads, 10)
        self.assertEqual((info["letters"], info["today"], info["room"]), (3, 3, 3))
        self.write_leads([lead(f"p{i}@x.ru", "47.11") for i in range(5)])
        info = self.service.prepare(self.leads, 10)
        self.assertEqual((info["letters"], info["today"]), (5, 3))
        self.assertEqual(sorted(box["planned"] for box in info["mailboxes"]), [1, 2])
        started = self.service.start(self.leads, 10)
        self.assertIn("сегодня уйдёт 3 из 5", started["message"])
        self.assertIn("Остальные 2 уйдут в следующие дни", started["message"])

    def test_nothing_starts_when_every_selected_quota_is_used_up(self):
        self.fill_today("1", 7)
        self.fill_today("2", 7)
        with self.assertRaises(SendError) as caught:
            self.service.start(self.leads, 3)
        self.assertIn("квота всех выбранных ящиков", caught.exception.message)
        self.assertEqual(self.mailer.calls, [])

    def test_start_hands_the_choice_to_the_sender_only_when_it_was_made(self):
        self.service.start(self.leads, 3)
        self.assertEqual(self.mailer.calls[-1], [self.letters_file, "--send", "--background", "--limit", "3"])
        self.service.start(self.leads, 3, mailbox_count=2)
        self.assertEqual(self.mailer.calls[-1][-2:], ["--mailboxes", "2"])
        self.service.start(self.leads, 3, only_mailbox="2")
        self.assertEqual(self.mailer.calls[-1][-2:], ["--mailbox", "2"])

    def test_a_half_filled_mailbox_is_explained_and_nothing_starts(self):
        with mock.patch.dict(os.environ, {"MAILRU_APP_PASSWORD_2": ""}):
            with self.assertRaises(SendError) as caught:
                self.service.prepare(self.leads, 5)
        self.assertIn("MAILRU_APP_PASSWORD_2", caught.exception.message)

    def test_status_lists_the_mailboxes_and_never_a_password(self):
        state = self.service.status()
        self.assertEqual([(box["id"], box["address"], box["room"]) for box in state["mailboxes"]],
                         [("1", "sender@mail.ru", 7), ("2", "second@mail.ru", 7)])
        self.assertEqual([(box["week"], box["week_cap"]) for box in state["mailboxes"]], [(1, 15), (1, 15)])
        text = json.dumps(self.service.prepare(self.leads, 10), ensure_ascii=False) + json.dumps(state, ensure_ascii=False)
        for secret in ("second-secret-pw", "@x.ru", "СЕКРЕТНАЯ"):
            self.assertNotIn(secret, text)

    def test_the_date_of_the_first_letter_is_part_of_the_picture(self):
        cfg.save_settings({"mailboxes": {"2": {"first_message": "2026-01-01"}}}, self.settings)
        box = self.service.mailbox_infos()[1]
        self.assertEqual((box["first_message"], box["source"]), ("2026-01-01", "задана на странице /mail"))
        self.assertEqual(box["room"], 40)                          # давно прогрет: действует только лимит в сутки
        self.assertEqual((box["week"], box["week_cap"]), (3, 40))

    def test_a_date_written_once_in_local_env_covers_both_mailboxes_and_the_page_shows_where_it_came_from(self):
        env = {"MAIL_FIRST_MESSAGE": "2026-01-01", "MAIL_FIRST_MESSAGE_2": "2026-01-02"}
        with mock.patch.dict(os.environ, env):
            first, second = self.service.mailbox_infos()
        self.assertEqual([(box["first_message"], box["source"], box["week_cap"]) for box in (first, second)],
                         [("2026-01-01", "задана в local.env", 40), ("2026-01-02", "задана в local.env", 40)])

    def test_the_week_cap_limits_what_the_page_promises_for_today(self):
        cfg.save_settings({"mailboxes": {"1": {"first_message": "2026-01-01"}}}, self.settings)   # ящик 1 давно прогрет
        self.write_leads([lead(f"p{i}@x.ru", "47.11") for i in range(30)])
        info = self.service.prepare(self.leads, 30)
        self.assertEqual((info["letters"], info["today"], info["room"]), (30, 30, 47))           # 40 + 7
        info = self.service.prepare(self.leads, 30, only_mailbox="2")
        self.assertEqual((info["today"], info["room"]), (7, 7))                                  # новый ящик: 7, а не 30


if __name__ == "__main__":
    unittest.main()
