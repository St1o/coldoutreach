import csv
import json
import os
import smtplib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import send_mail
from send_mail import InputError, Settings, load_settings, read_outbox

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
PASSWORD = "app-password-123"
SETTINGS = Settings("smtp.example.com", 587, "me@example.com", PASSWORD, "", "Иван Петров")


class FakeSmtp:
    """Подставной SMTP-сервер: запоминает письма, по команде отвечает ошибкой."""

    def __init__(self, errors=None, noop_ok=True):
        self.messages = []
        self.errors = errors or {}           # адрес получателя -> исключение
        self.noop_ok = noop_ok
        self.closed = False

    def noop(self):
        if not self.noop_ok:
            raise smtplib.SMTPServerDisconnected("closed")
        return 250, b"ok"

    def send_message(self, msg):
        error = self.errors.get(msg["To"])
        if error:
            raise error
        self.messages.append(msg)

    def quit(self):
        self.closed = True


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.ledger = os.path.join(self.dir, "sent.json")
        self.suppress = os.path.join(self.dir, "suppress.txt")
        self.logs, self.sleeps, self.connections = [], [], []
        self.smtp = FakeSmtp()

    def outbox(self, rows, name="outbox.csv", header=("Почта", "Тема", "Текст"), delimiter=";"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=delimiter)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    def connect(self, settings):
        self.connections.append(settings)
        return self.smtp

    def run_mail(self, path, **kwargs):
        kwargs.setdefault("send", True)
        kwargs.setdefault("settings", SETTINGS)
        return send_mail.run(path, self.ledger, suppress_path=self.suppress, connect=self.connect,
                             sleep=self.sleeps.append, now=lambda: NOW, log=self.logs.append, **kwargs)

    def ledger_data(self):
        with open(self.ledger, encoding="utf-8") as fh:
            return json.load(fh)

    def log_text(self):
        return "\n".join(self.logs)


def row(email, subject="Предложение", body="Здравствуйте!\nПишу по поводу..."):
    return [email, subject, body]


class SendTest(Case):
    def test_sends_each_letter_with_right_headers_and_utf8_body(self):
        path = self.outbox([row("A@Example.RU", "Тема письма", "Привет, мир!\nВторая строка")])
        self.assertEqual(self.run_mail(path), 0)
        msg = self.smtp.messages[0]
        self.assertEqual((msg["To"], msg["Subject"]), ("a@example.ru", "Тема письма"))
        self.assertIn("me@example.com", msg["From"])
        self.assertIn("Date", msg)
        self.assertIn("example.com>", msg["Message-ID"])
        self.assertEqual(msg.get_content().strip(), "Привет, мир!\nВторая строка")
        self.assertEqual(msg.get_content_charset(), "utf-8")
        self.assertEqual(self.ledger_data()["a@example.ru"]["status"], send_mail.SENT)

    def test_pause_only_between_letters(self):
        self.run_mail(self.outbox([row("a@x.ru"), row("b@x.ru"), row("c@x.ru")]), pause=30)
        self.assertEqual(self.sleeps, [30, 30])
        self.assertEqual(len(self.connections), 1)          # одно соединение на весь запуск
        self.assertTrue(self.smtp.closed)

    def test_dry_run_sends_nothing_writes_nothing_and_needs_no_password(self):
        path = self.outbox([row("a@x.ru", "Тема А", "Текст А")])
        self.assertEqual(self.run_mail(path, send=False, settings=None), 0)
        self.assertEqual((self.smtp.messages, self.connections), ([], []))
        self.assertFalse(os.path.exists(self.ledger))
        self.assertIn("Текст А", self.log_text())
        self.assertIn("--send", self.log_text())

    def test_header_injection_in_subject_is_flattened(self):
        self.run_mail(self.outbox([row("a@x.ru", "Тема\r\nBcc: evil@x.ru")]))
        msg = self.smtp.messages[0]
        self.assertIsNone(msg["Bcc"])
        self.assertEqual(msg["Subject"], "Тема Bcc: evil@x.ru")

    def test_comma_delimited_english_header_and_multiline_body(self):
        path = self.outbox([["a@x.ru", "S", "line1\nline2, with comma"]], header=("email", "subject", "body"), delimiter=",")
        self.run_mail(path)
        self.assertEqual(self.smtp.messages[0].get_content().strip(), "line1\nline2, with comma")

    def test_extra_columns_are_ignored(self):
        path = self.outbox([["Фирма", "a@x.ru", "62.01", "S", "B"]], header=("Название", "Почта", "ОКВЭД", "Тема", "Текст"))
        self.run_mail(path)
        self.assertEqual(self.smtp.messages[0]["To"], "a@x.ru")

    def test_missing_column_is_explained(self):
        path = self.outbox([["a@x.ru", "S"]], header=("Почта", "Тема"))
        self.assertEqual(self.run_mail(path), send_mail.INPUT_ERROR)
        self.assertIn("Текст", self.log_text())


class SkipTest(Case):
    def test_bad_rows_are_skipped_with_reasons(self):
        path = self.outbox([row("не-почта"), row("a@x.ru", subject=""), row("b@x.ru", body="  "),
                            row("c@x.ru"), row("C@X.RU"), row("d@x.ru, e@x.ru")])
        self.run_mail(path)
        self.assertEqual([m["To"] for m in self.smtp.messages], ["c@x.ru"])
        text = self.log_text()
        for reason in ("(некорректная почта): 2", "(пустая тема или текст): 2", "(повтор адреса в файле): 1"):
            self.assertIn(reason, text)

    def test_suppression_list_by_address_and_domain(self):
        with open(self.suppress, "w", encoding="utf-8") as fh:
            fh.write("# отказались\nno@x.ru\n@blocked.ru\n")
        self.run_mail(self.outbox([row("no@x.ru"), row("any@blocked.ru"), row("ok@x.ru")]))
        self.assertEqual([m["To"] for m in self.smtp.messages], ["ok@x.ru"])
        self.assertIn("(в списке отказов): 2", self.log_text())

    def test_nobody_gets_two_letters_even_from_another_file(self):
        self.run_mail(self.outbox([row("a@x.ru"), row("b@x.ru")], name="one.csv"))
        self.smtp.messages.clear()
        self.run_mail(self.outbox([row("a@x.ru"), row("c@x.ru")], name="two.csv"))
        self.assertEqual([m["To"] for m in self.smtp.messages], ["c@x.ru"])
        self.assertIn("(уже отправлено раньше): 1", self.log_text())


class LimitTest(Case):
    def many(self, n):
        return self.outbox([row(f"u{i}@x.ru") for i in range(n)])

    def test_daily_limit_defers_the_rest(self):
        self.run_mail(self.many(5), daily_limit=3)
        self.assertEqual(len(self.smtp.messages), 3)
        self.assertIn("отложено", self.log_text())
        self.smtp.messages.clear()
        self.run_mail(self.many(5), daily_limit=3)          # лимит уже выбран в эти сутки
        self.assertEqual(self.smtp.messages, [])
        self.assertIn("Дневной лимит 3", self.log_text())

    def test_old_letters_do_not_count_against_the_daily_limit(self):
        old = (NOW - timedelta(hours=25)).isoformat()
        send_mail.save_ledger(self.ledger, {f"old{i}@x.ru": {"status": send_mail.SENT, "at": old} for i in range(3)})
        self.run_mail(self.many(5), daily_limit=3)
        self.assertEqual(len(self.smtp.messages), 3)

    def test_default_daily_limit_is_a_cautious_50(self):
        self.run_mail(self.many(60), pause=0)
        self.assertEqual(len(self.smtp.messages), 50)

    def test_per_run_limit(self):
        self.run_mail(self.many(5), limit=2, daily_limit=200)
        self.assertEqual(len(self.smtp.messages), 2)


class FailureTest(Case):
    def test_refused_address_is_remembered_and_run_continues(self):
        self.smtp.errors["bad@x.ru"] = smtplib.SMTPRecipientsRefused({"bad@x.ru": (550, b"no such user")})
        self.assertEqual(self.run_mail(self.outbox([row("bad@x.ru"), row("ok@x.ru")])), 0)
        self.assertEqual([m["To"] for m in self.smtp.messages], ["ok@x.ru"])
        entry = self.ledger_data()["bad@x.ru"]
        self.assertEqual(entry["status"], send_mail.REJECTED)
        self.assertIn("550", entry["error"])
        self.smtp.messages.clear()
        self.run_mail(self.outbox([row("bad@x.ru")], name="again.csv"))
        self.assertEqual(self.smtp.messages, [])            # отвергнутому адресу не пишем второй раз

    def test_many_refused_in_a_row_stop_the_run(self):
        for i in range(5):
            self.smtp.errors[f"b{i}@x.ru"] = smtplib.SMTPRecipientsRefused({f"b{i}@x.ru": (550, b"x")})
        path = self.outbox([row(f"b{i}@x.ru") for i in range(5)] + [row("ok@x.ru")])
        self.assertEqual(self.run_mail(path), send_mail.TOO_MANY_REJECTS)
        self.assertEqual(self.smtp.messages, [])

    def test_wrong_password_stops_and_does_not_leak_it(self):
        def refuse(settings):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        code = send_mail.run(self.outbox([row("a@x.ru")]), self.ledger, settings=SETTINGS, send=True,
                             suppress_path=self.suppress, connect=refuse, sleep=self.sleeps.append,
                             now=lambda: NOW, log=self.logs.append)
        self.assertEqual(code, send_mail.AUTH_ERROR)
        self.assertIn("пароль приложения", self.log_text())
        self.assertNotIn(PASSWORD, self.log_text())
        self.assertFalse(os.path.exists(self.ledger))

    def test_server_refusal_stops_and_letter_stays_unsent(self):
        self.smtp.errors["a@x.ru"] = smtplib.SMTPDataError(550, b"5.4.5 Daily user sending quota exceeded")
        path = self.outbox([row("a@x.ru"), row("b@x.ru")])
        self.assertEqual(self.run_mail(path), send_mail.SERVER_STOP)
        self.assertIn("quota", self.log_text())
        self.assertEqual(self.smtp.messages, [])
        self.assertNotIn("a@x.ru", self.ledger_data())      # не отправлено - значит, можно повторить
        self.smtp.errors.clear()
        self.run_mail(path)
        self.assertEqual([m["To"] for m in self.smtp.messages], ["a@x.ru", "b@x.ru"])

    def test_connection_lost_mid_send_is_never_retried_automatically(self):
        self.smtp.errors["a@x.ru"] = smtplib.SMTPServerDisconnected("reset")
        path = self.outbox([row("a@x.ru"), row("b@x.ru")])
        self.assertEqual(self.run_mail(path), send_mail.SERVER_STOP)
        self.assertEqual(self.ledger_data()["a@x.ru"]["status"], send_mail.SENDING)
        self.smtp.errors.clear()
        self.run_mail(path)
        self.assertEqual([m["To"] for m in self.smtp.messages], ["b@x.ru"])         # a - ждёт вашей проверки
        self.assertIn("исход прошлой отправки неизвестен", self.log_text())
        self.run_mail(path, retry_unknown=True)
        self.assertEqual([m["To"] for m in self.smtp.messages], ["b@x.ru", "a@x.ru"])

    def test_dead_connection_is_reopened_before_sending(self):
        self.smtp.noop_ok = False
        self.run_mail(self.outbox([row("a@x.ru"), row("b@x.ru")]))
        self.assertEqual(len(self.connections), 2)
        self.assertEqual(len(self.smtp.messages), 2)

    def test_ctrl_c_keeps_the_ledger(self):
        def interrupt(_seconds):
            raise KeyboardInterrupt
        code = send_mail.run(self.outbox([row("a@x.ru"), row("b@x.ru")]), self.ledger, settings=SETTINGS, send=True,
                             suppress_path=self.suppress, connect=self.connect, sleep=interrupt,
                             now=lambda: NOW, log=self.logs.append)
        self.assertEqual(code, send_mail.INTERRUPTED)
        self.assertEqual(self.ledger_data()["a@x.ru"]["status"], send_mail.SENT)

    def test_second_simultaneous_run_is_refused(self):
        open(self.ledger + ".lock", "w").close()
        self.assertEqual(self.run_mail(self.outbox([row("a@x.ru")])), send_mail.INPUT_ERROR)
        self.assertEqual(self.smtp.messages, [])
        self.assertIn("другая отправка", self.log_text())

    def test_lock_is_released_after_a_run(self):
        self.run_mail(self.outbox([row("a@x.ru")]))
        self.assertFalse(os.path.exists(self.ledger + ".lock"))

    def test_damaged_ledger_is_not_silently_replaced(self):
        with open(self.ledger, "w", encoding="utf-8") as fh:
            fh.write("{не json")
        self.assertEqual(self.run_mail(self.outbox([row("a@x.ru")])), send_mail.INPUT_ERROR)
        self.assertEqual(self.smtp.messages, [])


class SettingsTest(unittest.TestCase):
    def test_missing_variables_are_named_without_values(self):
        with self.assertRaises(InputError) as caught:
            load_settings({"SMTP_HOST": "smtp.example.com"})
        self.assertIn("SMTP_USER", str(caught.exception))
        self.assertIn("SMTP_PASSWORD", str(caught.exception))
        self.assertNotIn("smtp.example.com", str(caught.exception))

    def test_defaults_and_overrides(self):
        env = {"SMTP_HOST": "h", "SMTP_USER": "u@x.ru", "SMTP_PASSWORD": "p"}
        settings = load_settings(env)
        self.assertEqual((settings.port, settings.from_address), (587, "u@x.ru"))
        env.update(SMTP_PORT="465", MAIL_FROM="sales@x.ru", MAIL_FROM_NAME="Имя")
        settings = load_settings(env)
        self.assertEqual((settings.port, settings.from_address, settings.name), (465, "sales@x.ru", "Имя"))

    def test_bad_port(self):
        with self.assertRaises(InputError):
            load_settings({"SMTP_HOST": "h", "SMTP_USER": "u", "SMTP_PASSWORD": "p", "SMTP_PORT": "abc"})

    def test_missing_outbox_file(self):
        with self.assertRaises(InputError):
            read_outbox("/nonexistent/outbox.csv")


if __name__ == "__main__":
    unittest.main()
