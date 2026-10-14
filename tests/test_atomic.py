import os
import tempfile
import unittest
from unittest import mock

from mailing import atomic, journal


class ReplaceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "state.json")
        self.tmp = self.path + ".part"
        with open(self.tmp, "w", encoding="utf-8") as fh:
            fh.write("new")

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def test_plain_replace(self):
        atomic.replace(self.tmp, self.path)
        self.assertEqual(self.read(), "new")
        self.assertFalse(os.path.exists(self.tmp))

    def test_busy_file_is_retried_until_it_is_free(self):
        real, pauses = os.replace, []
        busy = [PermissionError(5, "Отказано в доступе")] * 3      # файл прочитан другой программой в этот миг

        def flaky(src, dst):
            if busy:
                raise busy.pop()
            real(src, dst)

        with mock.patch("os.replace", flaky):
            atomic.replace(self.tmp, self.path, sleep=pauses.append)
        self.assertEqual(self.read(), "new")
        self.assertEqual(len(pauses), 3)

    def test_a_file_that_stays_busy_raises_after_the_last_attempt(self):
        pauses = []
        with mock.patch("os.replace", side_effect=PermissionError(5, "Отказано в доступе")) as replace:
            with self.assertRaises(PermissionError):
                atomic.replace(self.tmp, self.path, attempts=4, sleep=pauses.append)
        self.assertEqual((replace.call_count, len(pauses)), (4, 3))

    def test_other_errors_are_not_retried(self):
        pauses = []
        with mock.patch("os.replace", side_effect=FileNotFoundError()) as replace:
            with self.assertRaises(FileNotFoundError):
                atomic.replace(self.tmp, self.path, sleep=pauses.append)
        self.assertEqual((replace.call_count, pauses), (1, []))

    def test_the_sent_journal_survives_a_reader_holding_the_file(self):
        journal.save_state(self.path, {"a": 1})
        real, busy = os.replace, [PermissionError(5, "Отказано в доступе")]

        def flaky(src, dst):
            if busy:
                raise busy.pop()
            real(src, dst)

        with mock.patch("os.replace", flaky):
            journal.save_state(self.path, {"a": 2})
        self.assertEqual(journal.load_state(self.path), {"a": 2})


if __name__ == "__main__":
    unittest.main()
