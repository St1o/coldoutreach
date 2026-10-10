import smtplib
import threading
import unittest

from mail_service import MailError, MailService

USER = "sender@internet.ru"
SECRET = "APP-PASSWORD-123"


class FakeSender:
    def __init__(self, error=None, gate=None):
        self.user, self.error, self.gate = USER, error, gate
        self.sent, self.closed = [], False

    def send(self, msg):
        if self.gate:
            self.gate.wait(5)
        if self.error:
            raise self.error
        self.sent.append(msg)

    def close(self):
        self.closed = True


class MailServiceTest(unittest.TestCase):
    def make(self, error=None, **kw):
        self.sender = FakeSender(error)
        kw.setdefault("clock", lambda: self.now)
        return MailService(USER, SECRET, sender_factory=lambda: self.sender, **kw)

    def setUp(self):
        self.now = 1000.0

    def fail(self, service, status, **args):
        with self.assertRaises(MailError) as ctx:
            service.send(**{"to": "a@b.ru", "subject": "Тема", "body": "Текст", **args})
        self.assertEqual(ctx.exception.status, status, ctx.exception.message)
        return ctx.exception.message

    def test_sends_and_reports(self):
        service = self.make(from_name="Пётр")
        result = service.send("a@b.ru", "Тема", "Текст письма")
        self.assertTrue(result["ok"])
        self.assertEqual((result["to"], result["from"]), ("a@b.ru", USER))
        self.assertEqual(len(self.sender.sent), 1)
        msg = self.sender.sent[0]
        self.assertEqual((msg["To"], msg["Subject"]), ("a@b.ru", "Тема"))
        self.assertIn(USER, msg["From"])
        self.assertEqual(msg["Message-ID"], result["message_id"])
        self.assertTrue(self.sender.closed)

    def test_not_configured(self):
        for user, password in (("", ""), (USER, ""), ("", SECRET), ("not-an-address", SECRET)):
            service = MailService(user, password)
            self.assertFalse(service.configured)
            self.assertEqual(service.defaults()["configured"], False)
            with self.assertRaises(MailError) as ctx:
                service.send("a@b.ru", "Тема", "Текст")
            self.assertEqual(ctx.exception.status, 503)
            self.assertIn("MAILRU_APP_PASSWORD", ctx.exception.message)

    def test_validation(self):
        service = self.make()
        self.fail(service, 400, to="не адрес")
        self.fail(service, 400, to="a@b.ru, c@d.ru")
        self.fail(service, 400, subject="")
        self.fail(service, 400, subject="две\nстроки")
        self.fail(service, 400, body="   ")
        self.fail(service, 400, body="x" * 20_001)
        self.assertEqual(self.sender.sent, [])

    def test_hourly_cap_and_recovery(self):
        service = self.make(max_per_hour=2)
        service.send("a@b.ru", "Т", "Х")
        self.now += 10
        service.send("c@d.ru", "Т", "Х")
        self.assertIn("2 писем в час", self.fail(service, 429))
        self.now += 3600 - 10                       # первое письмо вышло из окна часа
        service.send("e@f.ru", "Т", "Х")
        self.assertEqual(len(self.sender.sent), 3)

    def test_failures_do_not_use_up_the_cap(self):
        service = self.make(error=smtplib.SMTPServerDisconnected("closed"), max_per_hour=1)
        self.fail(service, 502)
        self.fail(service, 502)
        self.sender.error = None
        service.send("a@b.ru", "Т", "Х")

    def test_auth_error_explains_app_password_and_hides_secret(self):
        service = self.make(error=smtplib.SMTPAuthenticationError(535, b"Application password is REQUIRED"))
        message = self.fail(service, 502)
        self.assertIn("пароль для внешних приложений", message)
        self.assertIn("535", message)
        self.assertNotIn(SECRET, message)

    def test_recipient_refused(self):
        error = smtplib.SMTPRecipientsRefused({"a@b.ru": (550, b"No such user")})
        message = self.fail(self.make(error=error), 502)
        self.assertIn("550", message)
        self.assertIn("No such user", message)

    def test_rate_limit_from_server(self):
        error = smtplib.SMTPDataError(451, b"Ratelimit exceeded for mailbox")
        message = self.fail(self.make(error=error), 502)
        self.assertIn("451", message)
        self.assertIn("позже", message)

    def test_blocked_smtp_port_mentions_render(self):
        for error in (TimeoutError("timed out"), OSError(101, "Network is unreachable")):
            message = self.fail(self.make(error=error), 504)
            self.assertIn("smtp.mail.ru:465", message)
            self.assertIn("Render", message)
            self.assertTrue(self.sender.closed)

    def test_second_send_while_busy_is_refused(self):
        service = self.make()
        self.sender.gate = gate = threading.Event()
        first = threading.Thread(target=lambda: service.send("a@b.ru", "Т", "Х"))
        first.start()
        try:
            for _ in range(200):                    # ждём, пока первое письмо займёт отправку
                if not service._busy.acquire(blocking=False):
                    break
                service._busy.release()
                threading.Event().wait(0.01)
            self.fail(service, 409)
        finally:
            gate.set()
            first.join(5)
        service.send("c@d.ru", "Т", "Х")            # после окончания снова можно

    def test_defaults_are_a_ready_example_addressed_to_the_sender(self):
        data = self.make().defaults()
        self.assertTrue(data["configured"])
        self.assertEqual(data["to"], USER)          # пробное письмо уходит владельцу ящика
        self.assertEqual(data["subject"], "Вопрос для ООО «Ромашка»")
        self.assertTrue(data["body"].startswith("Добрый день, Иван Иванович!"))
        self.assertNotIn(SECRET, str(data))

    def test_example_can_be_overridden_by_environment(self):
        data = self.make(example={"to": "x@y.ru", "director": "петров сергей николаевич"}).defaults()
        self.assertEqual(data["to"], "x@y.ru")
        self.assertTrue(data["body"].startswith("Добрый день, Сергей Николаевич!"))


if __name__ == "__main__":
    unittest.main()
