import os
import tempfile
import unittest

from mailing import journal


class JournalCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def save(self, name, state):
        journal.save_state(os.path.join(self.tmp.name, name), state)

    @staticmethod
    def entry(status, when="2026-10-11T00:17:03+00:00"):
        return {"status": status, "time": when, "note": "", "subject": "Тема"}


class SentCountTest(JournalCase):
    def test_an_empty_folder_or_a_missing_journal_has_none(self):
        self.assertEqual(journal.sent_count(self.tmp.name, 1), 0)
        self.assertEqual(journal.sent_count(os.path.join(self.tmp.name, "нет такой папки"), 2), 0)

    def test_only_letters_accepted_by_the_server_are_counted_per_stage(self):
        self.save("letters_1.sent.json", {"a@x.ru": self.entry(journal.SENT), "b@x.ru": self.entry(journal.SENT),
                                          "c@x.ru": self.entry(journal.REJECTED)})
        self.save("letters_2.sent.json", {"a@x.ru": self.entry(journal.SENT)})
        self.save("letters_test.sent.json", {"me@x.ru": self.entry(journal.SENT)})
        self.assertEqual([journal.sent_count(self.tmp.name, stage) for stage in (1, 2, 3)], [2, 1, 0])


class SentOverviewTest(JournalCase):
    def test_empty_folder_has_nothing(self):
        self.assertEqual(journal.sent_overview(self.tmp.name), {})

    def test_the_latest_sent_stage_wins(self):
        self.save("letters_1.sent.json", {"a@x.ru": self.entry(journal.SENT), "b@x.ru": self.entry(journal.SENT)})
        self.save("letters_2.sent.json", {"a@x.ru": self.entry(journal.SENT, "2026-10-14T09:00:00+00:00")})
        overview = journal.sent_overview(self.tmp.name)
        self.assertEqual(overview["a@x.ru"], {"stage": 2, "status": journal.SENT, "time": "2026-10-14T09:00:00+00:00"})
        self.assertEqual(overview["b@x.ru"]["stage"], 1)

    def test_a_rejected_address_is_shown_but_never_hides_a_sent_letter(self):
        self.save("letters_1.sent.json", {"a@x.ru": self.entry(journal.REJECTED), "b@x.ru": self.entry(journal.SENT)})
        self.save("letters_2.sent.json", {"b@x.ru": self.entry(journal.REJECTED)})
        overview = journal.sent_overview(self.tmp.name)
        self.assertEqual(overview["a@x.ru"]["status"], journal.REJECTED)
        self.assertEqual(overview["b@x.ru"]["status"], journal.SENT)

    def test_test_letters_and_other_statuses_do_not_count(self):
        self.save("letters_test.sent.json", {"me@x.ru": self.entry(journal.SENT)})
        self.save("letters_1.sent.json", {"a@x.ru": self.entry("temp-error")})
        self.assertEqual(journal.sent_overview(self.tmp.name), {})


class SentStagesTest(JournalCase):
    def test_each_accepted_stage_is_listed_with_its_time(self):
        self.save("letters_1.sent.json", {"a@x.ru": self.entry(journal.SENT), "b@x.ru": self.entry(journal.SENT)})
        self.save("letters_2.sent.json", {"a@x.ru": self.entry(journal.SENT, "2026-10-14T09:00:00+00:00")})
        stages = journal.sent_stages(self.tmp.name)
        self.assertEqual(stages["a@x.ru"], {1: "2026-10-11T00:17:03+00:00", 2: "2026-10-14T09:00:00+00:00"})
        self.assertEqual(list(stages["b@x.ru"]), [1])

    def test_rejected_test_and_temporary_entries_do_not_count(self):
        self.save("letters_1.sent.json", {"a@x.ru": self.entry(journal.REJECTED), "b@x.ru": self.entry("temp-error")})
        self.save("letters_test.sent.json", {"me@x.ru": self.entry(journal.SENT)})
        self.assertEqual(journal.sent_stages(self.tmp.name), {})

    def test_empty_folder_has_no_stages(self):
        self.assertEqual(journal.sent_stages(self.tmp.name), {})


if __name__ == "__main__":
    unittest.main()
