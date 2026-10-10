import contextlib
import io
import os
import tempfile
import unittest

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

    def test_starts_with_a_fresh_password_and_never_prints_the_mail_password(self):
        self.write("MAILRU_USER=a@b.ru\nMAILRU_APP_PASSWORD=very-secret-pw\n")
        code, out = self.go()
        self.assertEqual(code, 0)
        port, token, values, browser = self.calls[0]
        self.assertEqual((port, browser), (8000, True))
        self.assertGreaterEqual(len(token), 12)
        self.assertIn(token, out)                                   # пароль страницы показан на случай, если браузер не открылся
        self.assertNotIn("very-secret-pw", out)
        self.go()
        self.assertNotEqual(token, self.calls[1][1])                          # на каждый запуск новый

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
