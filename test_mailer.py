import os
import smtplib
import tempfile
import unittest

import mailer
from mailer import Letter, MailruSender, build_message, read_letters, run

USER = "sender@mail.ru"
SECRET = "APP-PASSWORD-123"


class FakeServer:
    """Подставной SMTP-сервер: запоминает письма, умеет отказывать по сценарию."""

    def __init__(self, fail=None, alive=True):
        self.sent, self.fail, self.logins, self.alive, self.quit_called = [], fail or {}, [], alive, False

    def login(self, user, password):
        self.logins.append((user, password))

    def noop(self):
        if not self.alive:
            raise smtplib.SMTPServerDisconnected("closed")
        return 250, b"ok"

    def send_message(self, msg):
        error = self.fail.get(msg["To"])
        if error:
            raise error
        self.sent.append(msg)

    def quit(self):
        self.quit_called = True

    def close(self):
        pass


def refused(code, text=b"no such user"):
    return smtplib.SMTPRecipientsRefused({"x@y.ru": (code, text)})


class TempTable:
    def __init__(self, content, name="letters.csv"):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, name)
        with open(self.path, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(content)

    def close(self):
        self.dir.cleanup()


def table(*rows, header="Почта;Тема;Текст"):
    return header + "\n" + "\n".join(rows) + "\n"


class ReadLettersTest(unittest.TestCase):
    def read(self, content):
        t = TempTable(content)
        self.addCleanup(t.close)
        return read_letters(t.path)

    def test_reads_semicolon_table_with_bom(self):
        letters, problems = self.read(table("a@b.ru;Привет;Здравствуйте, Иван"))
        self.assertEqual(letters, [Letter("a@b.ru", "Привет", "Здравствуйте, Иван")])
        self.assertEqual(problems, [])

    def test_comma_delimiter_and_english_headers(self):
        letters, _ = self.read("to,subject,body\na@b.ru,Hi,Hello\n")
        self.assertEqual(letters, [Letter("a@b.ru", "Hi", "Hello")])

    def test_multiline_body_in_quotes_is_kept(self):
        letters, problems = self.read('Почта;Тема;Текст\na@b.ru;Тема;"Строка 1\nСтрока 2\n\nПодпись"\n')
        self.assertEqual(letters[0].body, "Строка 1\nСтрока 2\n\nПодпись")
        self.assertEqual(problems, [])

    def test_missing_columns_are_named(self):
        with self.assertRaises(mailer.LettersError) as ctx:
            self.read("Почта;Тема\na@b.ru;Тема\n")
        self.assertIn("Текст", str(ctx.exception))

    def test_bad_rows_are_reported_without_address(self):
        letters, problems = self.read(table(
            "не-адрес;Тема;Текст",
            "ok@b.ru;;Текст",
            "dup@b.ru;Тема;Текст",
            "DUP@b.ru;Тема;Ещё",
            "two@b.ru, three@b.ru;Тема;Текст",
            "fine@b.ru;Тема;Текст"))
        self.assertEqual([l.email for l in letters], ["dup@b.ru", "fine@b.ru"])
        self.assertEqual([n for n, _ in problems], [2, 3, 5, 6])
        self.assertFalse(any("@" in reason for _, reason in problems))

    def test_missing_file(self):
        with self.assertRaises(mailer.LettersError):
            read_letters("/нет/такого/файла.csv")


class BuildMessageTest(unittest.TestCase):
    def test_headers_and_ascii_safe_cyrillic(self):
        msg = build_message(Letter("a@b.ru", "Предложение", "Добрый день!\nКак дела?"), USER, "Пётр")
        self.assertEqual(msg["To"], "a@b.ru")
        self.assertIn(USER, msg["From"])
        self.assertTrue(msg["Message-ID"].endswith("@mail.ru>"))
        raw = msg.as_bytes()
        raw.decode("ascii")                                    # на проводе только ASCII (base64 + RFC 2047)
        self.assertEqual(msg.get_content().strip(), "Добрый день!\nКак дела?")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.logs, self.sleeps = [], []

    def go(self, content, server=None, **kw):
        t = TempTable(content)
        self.addCleanup(t.close)
        self.server = server or FakeServer()
        sender = MailruSender(USER, SECRET, connect=lambda *a: self.server)
        code = run(t.path, sender=sender, log=self.logs.append, sleep=self.sleeps.append, **kw)
        self.path = t.path
        return code

    def output(self):
        return "\n".join(self.logs)

    def test_dry_run_sends_and_connects_to_nothing(self):
        code = self.go(table("a@b.ru;Тема;Текст"))
        self.assertEqual(code, 0)
        self.assertEqual(self.server.sent, [])
        self.assertEqual(self.server.logins, [])
        self.assertIn("--send", self.output())
        self.assertFalse(os.path.exists(mailer.state_path(self.path)))

    def test_dry_run_flags_bad_rows_with_exit_code_1(self):
        self.assertEqual(self.go(table("плохо;Тема;Текст", "a@b.ru;Тема;Текст")), 1)

    def test_sends_with_pause_between_letters_and_logs_them(self):
        code = self.go(table("a@b.ru;Т1;Х", "c@d.ru;Т2;Х", "e@f.ru;Т3;Х"), send=True, pause=7)
        self.assertEqual(code, 0)
        self.assertEqual([m["To"] for m in self.server.sent], ["a@b.ru", "c@d.ru", "e@f.ru"])
        self.assertEqual(self.sleeps, [7, 7])                  # между письмами, не перед первым и не после последнего
        self.assertEqual(self.server.logins, [(USER, SECRET)])
        self.assertTrue(self.server.quit_called)
        state = mailer.load_state(mailer.state_path(self.path))
        self.assertEqual({k: v["status"] for k, v in state.items()},
                         {"a@b.ru": "sent", "c@d.ru": "sent", "e@f.ru": "sent"})

    def test_limit_and_resume_without_duplicates(self):
        content = table("a@b.ru;Т;Х", "c@d.ru;Т;Х", "e@f.ru;Т;Х")
        self.go(content, send=True, limit=2, pause=0)
        first = [m["To"] for m in self.server.sent]
        self.assertEqual(first, ["a@b.ru", "c@d.ru"])
        # второй запуск с той же таблицей и тем же журналом
        server2 = FakeServer()
        run(self.path, send=True, limit=2, pause=0, log=self.logs.append, sleep=self.sleeps.append,
            sender=MailruSender(USER, SECRET, connect=lambda *a: server2))
        self.assertEqual([m["To"] for m in server2.sent], ["e@f.ru"])

    def test_permanent_recipient_refusal_is_remembered_and_run_continues(self):
        server = FakeServer(fail={"bad@b.ru": refused(550)})
        code = self.go(table("bad@b.ru;Т;Х", "ok@b.ru;Т;Х"), server=server, send=True, pause=0)
        self.assertEqual(code, 0)
        self.assertEqual([m["To"] for m in server.sent], ["ok@b.ru"])
        state = mailer.load_state(mailer.state_path(self.path))
        self.assertEqual(state["bad@b.ru"]["status"], "rejected")

    def test_temporary_recipient_refusals_are_retried_next_time_then_stop(self):
        server = FakeServer(fail={f"u{i}@b.ru": refused(450, b"try later") for i in range(3)})
        rows = [f"u{i}@b.ru;Т;Х" for i in range(3)] + ["z@b.ru;Т;Х"]
        code = self.go(table(*rows), server=server, send=True, pause=0)
        self.assertEqual(code, 5)
        self.assertEqual(server.sent, [])
        self.assertEqual(mailer.load_state(mailer.state_path(self.path)), {})

    def test_rate_limit_stops_at_once_and_keeps_letter_for_next_run(self):
        server = FakeServer(fail={"c@d.ru": smtplib.SMTPDataError(451, b"Ratelimit exceeded for mailbox. Try again later.")})
        code = self.go(table("a@b.ru;Т;Х", "c@d.ru;Т;Х", "e@f.ru;Т;Х"), server=server, send=True, pause=0)
        self.assertEqual(code, 3)
        self.assertEqual([m["To"] for m in server.sent], ["a@b.ru"])
        self.assertEqual(list(mailer.load_state(mailer.state_path(self.path))), ["a@b.ru"])
        self.assertIn("451", self.output())

    def test_auth_failure_stops_and_mentions_app_password(self):
        class BadLogin(FakeServer):
            def login(self, user, password):
                raise smtplib.SMTPAuthenticationError(535, b"Application password is REQUIRED")

        code = self.go(table("a@b.ru;Т;Х"), server=BadLogin(), send=True)
        self.assertEqual(code, 2)
        self.assertIn("пароль для внешних приложений", self.output())

    def test_password_never_appears_in_output(self):
        self.go(table("a@b.ru;Т;Х"), send=True, pause=0)
        self.assertNotIn(SECRET, self.output())

    def test_missing_credentials(self):
        t = TempTable(table("a@b.ru;Т;Х"))
        self.addCleanup(t.close)
        self.assertEqual(run(t.path, send=True, log=self.logs.append), 1)
        self.assertIn("MAILRU_APP_PASSWORD", self.output())

    def test_stale_connection_is_reopened_before_sending(self):
        connects = []
        servers = [FakeServer(alive=False), FakeServer()]

        def connect(*args):
            connects.append(args)
            return servers[len(connects) - 1]

        sender = MailruSender(USER, SECRET, connect=connect)
        sender.send(build_message(Letter("a@b.ru", "Т", "Х"), USER))
        servers[0].alive = False                                 # соединение «протухло» за время паузы
        sender.send(build_message(Letter("c@d.ru", "Т", "Х"), USER))
        self.assertEqual(len(connects), 2)
        self.assertEqual([m["To"] for m in servers[1].sent], ["c@d.ru"])


if __name__ == "__main__":
    unittest.main()
