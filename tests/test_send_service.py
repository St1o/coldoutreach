import json
import os
import tempfile
import unittest
from unittest import mock

import compose
import send_service
from mailing import journal
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
        self.service = SendService(self.data, settings_path=os.path.join(self.tmp.name, "none.json"),
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
        info = self.service.prepare(self.leads, 10)
        self.assertEqual((info["daily_limit"], info["sent_24h"], info["room"]), (40, 1, 39))

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


if __name__ == "__main__":
    unittest.main()
