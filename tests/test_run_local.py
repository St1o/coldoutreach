import contextlib
import io
import os
import tempfile
import unittest
import unittest.mock

import run_local

EXAMPLE = os.path.join(os.path.dirname(os.path.abspath(run_local.__file__)), "local.env.example")


class ReadEnvFileTest(unittest.TestCase):
    def read(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "local.env")
            with open(path, "w", encoding="utf-8-sig") as fh:
                fh.write(text)
            return run_local.read_env_file(path)

    def test_parses_values_comments_and_quotes(self):
        values = self.read("# комментарий\n\nMAILRU_USER = a@b.ru \nMAILRU_APP_PASSWORD=\"abc def\"\n"
                           "PORT='8001'\n  # MAIL_EXAMPLE_TO=x@y.ru\nbroken line\n")
        self.assertEqual(values, {"MAILRU_USER": "a@b.ru", "MAILRU_APP_PASSWORD": "abc def", "PORT": "8001"})

    def test_example_file_has_no_secrets_filled_in(self):
        values = run_local.read_env_file(EXAMPLE)
        self.assertEqual(values, {"MAILRU_USER": "", "MAILRU_APP_PASSWORD": ""})


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = os.path.join(self.tmp.name, "local.env")
        self.calls = []

    def go(self, argv=(), run=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_local.main(list(argv), self.env, EXAMPLE, run or (lambda *a: self.calls.append(a)))
        return code, out.getvalue()

    def write(self, text):
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_first_run_creates_the_file_and_asks_to_fill_it(self):
        code, out = self.go()
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(self.env))
        self.assertIn("MAILRU_APP_PASSWORD", out)
        self.assertEqual(self.calls, [])

    def test_unfilled_file_names_what_is_missing(self):
        self.write("MAILRU_USER=a@b.ru\nMAILRU_APP_PASSWORD=\n")
        code, out = self.go()
        self.assertEqual(code, 1)
        self.assertIn("MAILRU_APP_PASSWORD", out)
        self.assertNotIn("MAILRU_USER,", out)
        self.assertEqual(self.calls, [])

    def test_creates_the_page_password_once_and_keeps_it_in_the_file(self):
        self.write("MAILRU_USER=a@b.ru\nMAILRU_APP_PASSWORD=very-secret-pw\n")
        code, out = self.go()
        self.assertEqual(code, 0)
        port, token, values, browser = self.calls[0]
        self.assertEqual((port, browser), (8000, True))
        self.assertGreaterEqual(len(token), 12)
        self.assertEqual(run_local.read_env_file(self.env)["LEADS_TOKEN"], token)      # лежит в файле: вводить не нужно
        self.assertNotIn("very-secret-pw", out)
        self.assertNotIn(token, out)                                # в консоль пароль страницы не печатается
        self.go()
        self.assertEqual(token, self.calls[1][1])                   # от запуска к запуску тот же
        with open(self.env, encoding="utf-8") as fh:
            self.assertEqual(fh.read().count("LEADS_TOKEN="), 1)    # строка дописана один раз

    def test_a_too_short_token_is_replaced_for_this_run_only(self):
        self.write("MAILRU_USER=a@b.ru\nMAILRU_APP_PASSWORD=x\nLEADS_TOKEN=short\n")
        code, out = self.go()
        self.assertEqual(code, 0)
        self.assertGreaterEqual(len(self.calls[0][1]), 12)
        self.assertIn("короче 12", out)
        self.assertEqual(run_local.read_env_file(self.env)["LEADS_TOKEN"], "short")     # чужую строку не трогаем

    def test_a_token_with_russian_letters_or_spaces_is_not_used(self):
        for bad in ("придумайте_пароль_16_знаков_латиница_и_цифры", "has a space in the middle ok", "x" * 5 + "ё" * 10):
            self.write(f"MAILRU_USER=a@b.ru\nMAILRU_APP_PASSWORD=x\nLEADS_TOKEN={bad}\n")
            self.calls.clear()
            code, out = self.go()
            self.assertEqual(code, 0, bad)
            token = self.calls[0][1]
            self.assertNotEqual(token, bad)
            self.assertTrue(token.isascii() and token.isprintable() and " " not in token, bad)
            self.assertIn("LEADS_TOKEN в local.env", out)
            self.assertNotIn(bad, out)

    def test_leads_password_is_the_page_password_and_wins_over_leads_token(self):
        self.write("MAILRU_USER=a@b.ru\nMAILRU_APP_PASSWORD=x\nLEADS_TOKEN=aAnInDLx1x_0jkc1P8veJw\n"
                   "LEADS_PASSWORD=123456789123456\nCHECKO_API_KEY=DruuXMbu3ViT3kwM\n")
        code, out = self.go()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[0][1], "123456789123456")
        self.assertNotIn("123456789123456", out)
        with open(self.env, encoding="utf-8") as fh:
            self.assertEqual(fh.read().count("LEADS_TOKEN="), 1)          # ничего не дописано: подходящий пароль уже есть
        self.assertEqual(self.calls[0][2]["CHECKO_API_KEY"], "DruuXMbu3ViT3kwM")   # ключ Checko - отдельная переменная

    def test_a_bad_leads_password_falls_back_to_a_good_token_or_a_fresh_one(self):
        self.write("MAILRU_USER=a@b.ru\nMAILRU_APP_PASSWORD=x\nLEADS_PASSWORD=short\nLEADS_TOKEN=aAnInDLx1x_0jkc1P8veJw\n")
        self.go()
        self.assertEqual(self.calls[0][1], "aAnInDLx1x_0jkc1P8veJw")
        self.write("MAILRU_USER=a@b.ru\nMAILRU_APP_PASSWORD=x\nLEADS_PASSWORD=short\n")
        code, out = self.go()
        self.assertGreaterEqual(len(self.calls[1][1]), 12)
        self.assertIn("LEADS_PASSWORD в local.env короче", out)
        self.assertEqual(run_local.read_env_file(self.env)["LEADS_PASSWORD"], "short")      # чужую строку не трогаем
        self.assertNotIn("LEADS_TOKEN", run_local.read_env_file(self.env))

    def test_token_problem_names_the_reason(self):
        self.assertEqual(run_local.token_problem("abcdefghijkl"), "")
        self.assertIn("короче", run_local.token_problem("short"))
        self.assertIn("русскими", run_local.token_problem("пароль-пароль-пароль"))

    def test_data_folder_defaults_next_to_the_project_and_can_be_set(self):
        self.write("MAILRU_USER=a@b.ru\nMAILRU_APP_PASSWORD=x\n")
        self.go()
        self.assertEqual(self.calls[0][2]["OUTREACH_DATA"], run_local.DEFAULT_DATA_DIR)
        self.assertEqual(os.path.basename(run_local.DEFAULT_DATA_DIR), "outreach-data")
        self.write("MAILRU_USER=a@b.ru\nMAILRU_APP_PASSWORD=x\nOUTREACH_DATA=/somewhere/data\n")
        self.go()
        self.assertEqual(self.calls[1][2]["OUTREACH_DATA"], "/somewhere/data")

    def test_browser_opens_the_leads_page_with_the_password_in_the_link(self):
        opened = []
        values = {"OUTREACH_DATA": "/tmp/x"}
        with unittest.mock.patch("threading.Timer") as timer, unittest.mock.patch.dict(os.environ, {}, clear=False):
            timer.side_effect = lambda delay, func, args=(): opened.append(args[0]) or unittest.mock.Mock()
            import app
            with unittest.mock.patch.object(app, "main"):
                run_local.serve(8123, "token-for-the-link", values, True)
        self.assertEqual(opened, ["http://127.0.0.1:8123/leads#token-for-the-link"])

    def test_the_site_is_told_to_hand_the_password_to_its_own_pages(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            import app
            with unittest.mock.patch.object(app, "main"):
                run_local.serve(8123, "token-for-the-link", {}, False)
            self.assertEqual(os.environ["LEADS_AUTO_LOGIN"], "1")
            self.assertEqual(os.environ["LEADS_TOKEN"], "token-for-the-link")

    def test_options_and_own_token(self):
        self.write("MAILRU_USER=a@b.ru\nMAILRU_APP_PASSWORD=x\nPORT=8123\nLEADS_TOKEN=my-own-long-password\n")
        self.go(["--no-browser"])
        port, token, _, browser = self.calls[0]
        self.assertEqual((port, token, browser), (8123, "my-own-long-password", False))
        self.go(["--port", "9001"])
        self.assertEqual(self.calls[1][0], 9001)

    def test_busy_port_is_explained(self):
        self.write("MAILRU_USER=a@b.ru\nMAILRU_APP_PASSWORD=x\n")

        def busy(*args):
            raise OSError(98, "Address already in use")

        code, out = self.go(run=busy)
        self.assertEqual(code, 1)
        self.assertIn("--port 8001", out)


if __name__ == "__main__":
    unittest.main()
