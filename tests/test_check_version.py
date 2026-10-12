import contextlib
import io
import os
import unittest
from unittest import mock

import check_version
from mailing import config


class FeaturesTest(unittest.TestCase):
    def test_this_version_has_every_feature_the_conductor_relies_on(self):
        missing = [name for name, ok, _ in check_version.features() if not ok]
        self.assertEqual(missing, [])

    def test_the_copy_to_the_sent_folder_is_one_of_the_checked_features(self):
        found = [(name, ok) for name, ok, _ in check_version.features() if "Отправленные" in name]
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0][1])

    def test_the_numbers_of_the_warmup_are_printed(self):
        details = " ".join(detail for _, _, detail in check_version.features())
        self.assertIn("первый день 7", details)
        self.assertIn("шаг +3", details)
        self.assertIn("((7, 15), (14, 30))", details)


class OutputTest(unittest.TestCase):
    def run_main(self, env):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(config, "ENV_FILE", os.devnull), contextlib.redirect_stdout(out):
            code = check_version.main()
        return code, out.getvalue()

    def test_no_addresses_and_no_passwords_are_printed(self):
        env = {"MAILRU_USER": "Ivan.Petrov@mail.ru", "MAILRU_APP_PASSWORD": "secret-pw-111",
               "MAILRU_USER_2": "second@mail.ru", "MAILRU_APP_PASSWORD_2": "secret-pw-222"}
        code, text = self.run_main(env)
        self.assertEqual(code, 0, text)
        for secret in ("Ivan.Petrov", "second@", "secret-pw-111", "secret-pw-222", "@mail.ru"):
            self.assertNotIn(secret, text)
        self.assertIn("подключено ящиков: 2 (номера: 1, 2)", text)
        self.assertIn("ВЕРСИЯ ОК", text)

    def test_a_half_filled_mailbox_is_named_by_number_only(self):
        _, text = self.run_main({"MAILRU_USER": "a@mail.ru", "MAILRU_APP_PASSWORD": "x1x1x1", "MAILRU_USER_2": "b@mail.ru"})
        self.assertIn("заполнены не полностью: 2", text)
        self.assertNotIn("b@mail.ru", text)


class GitInfoTest(unittest.TestCase):
    def test_without_git_everything_is_unknown_but_nothing_breaks(self):
        with mock.patch.object(check_version.subprocess, "run", side_effect=FileNotFoundError):
            info = check_version.git_info()
        self.assertEqual(info, {"branch": None, "commit": None, "changed": None, "ahead": None, "behind": None})

    def test_changed_files_are_listed_by_name_only(self):
        answers = {("status", "--short"): " M mailer.py\n?? tests/new.py", ("rev-list", "--left-right", "--count", "HEAD...@{u}"): "1\t2",
                   ("branch", "--show-current"): "main", ("log", "-1", "--format=%h %s"): "abc1234 Subject"}
        with mock.patch.object(check_version, "git", side_effect=lambda *args: answers.get(args)):
            info = check_version.git_info()
        self.assertEqual((info["changed"], info["ahead"], info["behind"]), (["mailer.py", "tests/new.py"], 1, 2))


if __name__ == "__main__":
    unittest.main()
