"""Копия отправленного письма в «Отправленные» (IMAP). Везде поддельное IMAP-соединение: настоящий imap.mail.ru тесты не трогают."""
import contextlib
import imaplib
import io
import json
import os
import smtplib
import ssl
import tempfile
import unittest
from email import message_from_bytes, policy
from types import SimpleNamespace
from unittest import mock

import mailer
from mailing import config as cfg, journal, sentcopy
from mailing import smtp as smtpmod
from mailing.smtp import MailruSender, build_message
from mailing.table import Letter
from tests.test_mailer import FakeServer, TempTable, refused, table

USER = "sender@mail.ru"
SECRET = "APP-PASSWORD-123"
RUSSIAN = "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"                      # «Отправленные» в modified UTF-7
SENT_LINES = [b'(\\HasNoChildren) "/" "INBOX"',
              b'(\\HasNoChildren \\Sent) "/" "' + RUSSIAN.encode() + b'"',
              b'(\\HasNoChildren \\Drafts) "/" "Drafts"']


class OrderedServer(FakeServer):
    """Подставной SMTP-сервер, который помнит порядок событий вместе с подставным IMAP."""

    def __init__(self, events, **kw):
        super().__init__(**kw)
        self.events = events

    def send_message(self, msg):
        super().send_message(msg)
        self.events.append("smtp " + msg["To"])


class FakeIMAP:
    """Подставной imaplib.IMAP4_SSL: запоминает вызовы, умеет отказывать по сценарию."""

    def __init__(self, events=None, lines=None, login_error=None, append_errors=None, append_reply=("OK", [b"done"]),
                 alive=True):
        self.events = events if events is not None else []
        self.lines = SENT_LINES if lines is None else lines
        self.login_error, self.append_errors, self.append_reply, self.alive = login_error, append_errors, append_reply, alive
        self.logins, self.appended, self.created = [], [], []
        self.list_calls = self.noops = 0
        self.logged_out = self.shut_down = False

    def login(self, user, password):
        self.logins.append((user, password))
        if self.login_error:
            raise self.login_error
        return "OK", [b"Logged in"]

    def noop(self):
        self.noops += 1
        if not self.alive:
            raise imaplib.IMAP4.abort("socket error: EOF")
        return "OK", [b"Nothing Accomplished"]

    def list(self):
        self.list_calls += 1
        return "OK", list(self.lines)

    def create(self, name):
        self.created.append(name)
        return "OK", [b"created"]

    def append(self, mailbox, flags, date_time, message):
        error = self.append_errors.pop(0) if self.append_errors else None
        if error:
            raise error
        if self.append_reply[0] == "OK":
            self.appended.append((mailbox, flags, date_time, message))
            self.events.append("imap append")
        return self.append_reply

    def logout(self):
        self.logged_out = True
        return "BYE", [b"bye"]

    def shutdown(self):
        self.shut_down = True


class Connector:
    """Вместо sentcopy.default_connect: каждое подключение - новый FakeIMAP; помнит, куда и с чем подключались."""

    def __init__(self, error=None, **kw):
        self.error, self.kw, self.calls, self.connections = error, kw, [], []

    def __call__(self, host, port, timeout):
        self.calls.append((host, port, timeout))
        if self.error:
            raise self.error
        imap = FakeIMAP(**self.kw)
        self.connections.append(imap)
        return imap

    @property
    def appended(self):
        return [item for imap in self.connections for item in imap.appended]


def strip_times(state):
    return {address: {key: value for key, value in entry.items() if key != "time"} for address, entry in state.items()}


class BatchCase(unittest.TestCase):
    """Партия писем через mailer.run с настоящим MailruSender и подставными SMTP и IMAP."""

    def batch(self, count=3, copy=True, fail=None, connector=None, mask=False, saver=None, pause=7, name="letters.csv"):
        source = TempTable(table(*[f"u{i}@x.ru;Тема {i};Текст {i}" for i in range(count)]), name)
        self.addCleanup(source.close)
        events, logs, sleeps = [], [], []
        server = OrderedServer(events, fail=fail)
        connector = connector if connector is not None else Connector(events=events)
        copiers = {}
        if copy:
            copiers = {"1": saver or sentcopy.SentFolderSaver(USER, SECRET, connect=connector)}
        code = mailer.run(source.path, send=True, limit=0, pause=pause, log=logs.append, sleep=sleeps.append, mask=mask,
                          sender=MailruSender(USER, SECRET, connect=lambda *a: server), copiers=copiers)
        folder = os.path.dirname(source.path)
        return SimpleNamespace(code=code, server=server, logs=logs, sleeps=sleeps, events=events, connector=connector,
                               path=source.path, folder=folder, state=journal.load_state(journal.state_path(source.path)))


class CopyAfterSendTest(BatchCase):
    def test_copy_is_appended_after_each_accepted_letter_with_the_same_date_and_message_id_and_seen_flag(self):
        done = self.batch(3)
        self.assertEqual(done.code, 0)
        appended = done.connector.appended
        self.assertEqual(len(appended), 3)
        self.assertEqual(len({m["Message-ID"] for m in done.server.sent}), 3)
        for sent, (folder, flags, date_time, data) in zip(done.server.sent, appended):
            self.assertEqual(folder, RUSSIAN)                          # папка с атрибутом \Sent, имя как прислал сервер
            self.assertEqual(flags, "\\Seen")
            self.assertEqual(data, sent.as_bytes())                    # те же байты, ничего не пересобрано
            copy = message_from_bytes(data, policy=policy.default)
            self.assertEqual((copy["Date"], copy["Message-ID"], copy["To"], copy["Subject"]),
                             (sent["Date"], sent["Message-ID"], sent["To"], sent["Subject"]))
        # копия идёт сразу за SMTP-приёмом своего письма, до следующего
        self.assertEqual(done.events, ["smtp u0@x.ru", "imap append", "smtp u1@x.ru", "imap append",
                                       "smtp u2@x.ru", "imap append"])
        self.assertEqual([line for line in done.logs if "копия" in line.split("]")[-1] and "Копии" not in line],
                         [f"[{n}/3] копия в Отправленные: ок" for n in (1, 2, 3)])

    def test_no_extra_pauses_between_letters(self):
        self.assertEqual(self.batch(3, pause=7).sleeps, [7, 7])
        self.assertEqual(self.batch(3, pause=7, copy=False).sleeps, [7, 7])

    def test_a_refused_letter_gets_no_copy_and_a_stopped_run_keeps_its_code(self):
        done = self.batch(3, fail={"u1@x.ru": refused(550)})
        self.assertEqual(done.code, 0)
        self.assertEqual([message_from_bytes(item[3], policy=policy.default)["To"] for item in done.connector.appended], ["u0@x.ru", "u2@x.ru"])
        self.assertEqual(done.state["u1@x.ru"]["status"], journal.REJECTED)
        limited = self.batch(3, fail={"u1@x.ru": smtplib.SMTPDataError(451, b"Ratelimit exceeded")})
        self.assertEqual(limited.code, 3)                              # код выхода тот же, что и без копии
        self.assertEqual([message_from_bytes(item[3], policy=policy.default)["To"] for item in limited.connector.appended], ["u0@x.ru"])

    def test_the_connection_is_reused_between_letters(self):
        done = self.batch(3)
        self.assertEqual(len(done.connector.calls), 1)
        imap = done.connector.connections[0]
        self.assertEqual(imap.logins, [(USER, SECRET)])
        self.assertEqual(imap.list_calls, 1)                           # папку ищем один раз
        self.assertEqual(len(imap.appended), 3)
        self.assertEqual(imap.noops, 2)                                # перед вторым и третьим письмом проверка, что связь жива
        self.assertTrue(imap.logged_out)                               # в конце запуска закрыто
        self.assertEqual(done.connector.calls[0][:2], ("imap.mail.ru", 993))

    def test_a_dropped_connection_is_replaced_by_a_new_one(self):
        connector = Connector()
        saver = sentcopy.SentFolderSaver(USER, SECRET, connect=connector)
        msg = build_message(Letter("a@x.ru", "Т", "Х"), USER)
        self.assertEqual(saver.save(msg), (True, ""))
        connector.connections[0].alive = False                         # сервер закрыл соединение между письмами
        self.assertEqual(saver.save(msg), (True, ""))
        self.assertEqual(len(connector.calls), 2)
        self.assertTrue(connector.connections[0].shut_down)
        self.assertEqual(len(connector.connections[1].appended), 1)
        saver.close()


class ConnectionTest(unittest.TestCase):
    def test_the_connection_is_secure_and_uses_the_same_checked_context_as_smtp(self):
        context = object()
        with mock.patch.object(sentcopy, "make_ssl_context", return_value=context), \
                mock.patch.object(sentcopy.imaplib, "IMAP4_SSL") as ssl_class:
            sentcopy.default_connect("imap.mail.ru", 993, 20.0)
        self.assertEqual(ssl_class.call_args.args, ("imap.mail.ru", 993))
        self.assertIs(ssl_class.call_args.kwargs["ssl_context"], context)
        self.assertEqual(ssl_class.call_args.kwargs["timeout"], 20.0)
        self.assertIs(sentcopy.make_ssl_context, smtpmod.make_ssl_context)           # не свой контекст, а контекст SMTP
        checked = smtpmod.make_ssl_context()
        self.assertEqual((checked.verify_mode, checked.check_hostname), (ssl.CERT_REQUIRED, True))

    def test_a_refused_connection_is_an_ordinary_oserror_that_the_saver_reports(self):
        with self.assertRaises(OSError):
            sentcopy.default_connect("127.0.0.1", 1, 2.0)
        saver = sentcopy.SentFolderSaver(USER, SECRET, "127.0.0.1", 1, timeout=2.0)
        ok, reason = saver.save(build_message(Letter("a@x.ru", "Т", "Х"), USER))
        self.assertFalse(ok)
        self.assertIn("нет соединения с 127.0.0.1:1", reason)


class FolderTest(unittest.TestCase):
    def save(self, lines):
        connector = Connector(lines=lines)
        saver = sentcopy.SentFolderSaver(USER, SECRET, connect=connector)
        msg = build_message(Letter("a@x.ru", "Т", "Х"), USER)
        return saver.save(msg), connector

    def test_the_folder_is_found_by_the_sent_attribute_whatever_its_name(self):
        lines = [b'(\\HasNoChildren) "/" "' + RUSSIAN.encode() + b'"', b'(\\HasNoChildren \\Sent) "/" "Elsewhere"']
        (ok, reason), connector = self.save(lines)
        self.assertTrue(ok, reason)
        self.assertEqual(connector.appended[0][0], "Elsewhere")        # атрибут главнее имени

    def test_without_the_attribute_the_russian_name_then_sent_are_tried(self):
        (ok, _), connector = self.save([b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Sent"',
                                        b'(\\HasNoChildren) "/" "' + RUSSIAN.encode() + b'"'])
        self.assertTrue(ok)
        self.assertEqual(connector.appended[0][0], RUSSIAN)
        (ok, _), connector = self.save([b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Sent"'])
        self.assertTrue(ok)
        self.assertEqual(connector.appended[0][0], "Sent")

    def test_without_any_sent_folder_nothing_is_created_and_a_warning_is_returned(self):
        (ok, reason), connector = self.save([b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Drafts"'])
        self.assertFalse(ok)
        self.assertIn("нет папки «Отправленные»", reason)
        self.assertEqual(connector.appended, [])
        self.assertEqual([imap.created for imap in connector.connections], [[]])      # новую папку не создаём

    def test_utf7_names_and_list_answers(self):
        self.assertEqual(sentcopy.utf7_encode("Отправленные"), RUSSIAN)
        self.assertEqual(sentcopy.utf7_decode(RUSSIAN), "Отправленные")
        self.assertEqual(sentcopy.utf7_decode(sentcopy.utf7_encode("A&B Ы 😀")), "A&B Ы 😀")
        entries = sentcopy.parse_list(SENT_LINES + [(b'(\\HasNoChildren) "/" {4}', b'Sent'), b'', None])
        self.assertEqual(entries[1], ({"\\hasnochildren", "\\sent"}, RUSSIAN))
        self.assertEqual(entries[3], ({"\\hasnochildren"}, "Sent"))


class FailureTest(BatchCase):
    """Сбой IMAP: письмо уже ушло, поэтому отправка, журнал и код выхода те же, что и без копии, и ничего не повторяется."""

    @staticmethod
    def scenarios():
        """Свежие сценарии сбоя на каждый вызов: списки ошибок поддельного IMAP расходуются по мере работы."""
        return {
            "нет соединения": dict(error=OSError("Network is unreachable")),
            "сертификат": dict(error=ssl.SSLCertVerificationError(1, "certificate verify failed")),
            "обрыв шифрования": dict(error=ssl.SSLError("EOF occurred in violation of protocol")),
            "логин не принят": dict(login_error=imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")),
            "обрыв при сохранении": dict(append_errors=[imaplib.IMAP4.abort("socket error: EOF")] * 3),
            "сервер ответил NO": dict(append_reply=("NO", [b"[OVERQUOTA] Quota exceeded"])),
            "неожиданная ошибка": dict(append_errors=[RuntimeError("boom")] * 3),
            "нет папки": dict(lines=[b'(\\HasNoChildren) "/" "INBOX"']),
        }

    def test_an_imap_failure_does_not_change_the_send_the_journal_or_the_exit_code(self):
        control = self.batch(3, copy=False)
        self.assertEqual(control.code, 0)
        for title in self.scenarios():
            with self.subTest(title):
                done = self.batch(3, connector=Connector(**self.scenarios()[title]))
                self.assertEqual(done.code, control.code)
                self.assertEqual([m["To"] for m in done.server.sent], ["u0@x.ru", "u1@x.ru", "u2@x.ru"])   # каждое письмо ровно один раз
                self.assertEqual(strip_times(done.state), strip_times(control.state))
                self.assertEqual(sum("отправлено" in line for line in done.logs), 3)
                self.assertEqual(done.sleeps, control.sleeps)
                failed = [line for line in done.logs if "копия в Отправленные не сохранена (" in line]
                self.assertGreaterEqual(len(failed), 1, done.logs)
                self.assertEqual(done.connector.appended, [])
                self.assertEqual([imap.created for imap in done.connector.connections], [[]] * len(done.connector.connections))

    def test_a_copier_that_raises_cannot_stop_the_batch(self):
        class Exploding:
            def save(self, msg):
                raise RuntimeError("boom")

            def close(self):
                pass

        done = self.batch(2, saver=Exploding())
        self.assertEqual(done.code, 0)
        self.assertEqual(len(done.server.sent), 2)
        self.assertEqual(sum("копия в Отправленные не сохранена (RuntimeError)" in line for line in done.logs), 2)

    def test_after_a_refused_login_or_a_missing_folder_the_run_does_not_try_again(self):
        done = self.batch(5, connector=Connector(login_error=imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")))
        self.assertEqual(len(done.connector.calls), 1)                 # неверный пароль не пробуем вводить снова и снова
        self.assertEqual(len(done.server.sent), 5)
        self.assertEqual(sum("копия в Отправленные не сохранена" in line for line in done.logs), 5)
        failed = [line for line in done.logs if "не сохранена" in line]
        self.assertIn("доступ по IMAP", failed[0])
        self.assertIn("до конца запуска больше не пробуем", failed[0])

    def test_three_failures_in_a_row_switch_the_copies_off_until_the_end_of_the_run(self):
        done = self.batch(6, connector=Connector(error=OSError("timed out")))
        self.assertEqual(len(done.connector.calls), sentcopy.MAX_FAILURES_IN_A_ROW)
        self.assertEqual(len(done.server.sent), 6)
        self.assertEqual(done.code, 0)

    def test_a_success_resets_the_count_of_failures(self):
        done = self.batch(6, connector=Connector(append_errors=[RuntimeError("x"), RuntimeError("x"), None,
                                                                RuntimeError("x"), RuntimeError("x"), None]))
        self.assertEqual(len(done.connector.appended), 2)
        self.assertEqual(sum("копия в Отправленные: ок" in line for line in done.logs), 2)


class SecretsTest(BatchCase):
    TEXT = f"login {SECRET} failed for u0@x.ru and u1@x.ru, sender sender@mail.ru"
    RECIPIENTS = ("u0@x.ru", "u1@x.ru", "u2@x.ru")

    def scenarios(self):
        return [dict(login_error=imaplib.IMAP4.error(self.TEXT.encode())),
                dict(append_reply=("NO", [self.TEXT.encode()])),
                dict(append_errors=[OSError(self.TEXT)] * 3),
                dict(append_errors=[RuntimeError(self.TEXT)] * 3)]

    def read(self, folder, name):
        with open(os.path.join(folder, name), encoding="utf-8") as fh:
            return fh.read()

    def test_neither_the_password_nor_recipient_addresses_reach_the_log_or_the_files(self):
        for number in range(len(self.scenarios())):
            for mask in (False, True):
                with self.subTest(number=number, mask=mask):
                    done = self.batch(3, connector=Connector(**self.scenarios()[number]), mask=mask)
                    copy_text = "\n".join(line for line in done.logs if "копия" in line or "Копии" in line)
                    report = self.read(done.folder, sentcopy.REPORT_NAME)
                    self.assertIn("не сохранена (", copy_text)
                    self.assertIn("***", copy_text)                                  # адрес и пароль в причине закрыты
                    for text in (copy_text, report):
                        self.assertNotIn(SECRET, text)
                        for address in self.RECIPIENTS + (USER,):
                            self.assertNotIn(address, text)
                    everything = "\n".join(done.logs)
                    self.assertNotIn(SECRET, everything)
                    self.assertNotIn(SECRET, self.read(done.folder, "letters.sent.json"))      # журнал отправки
                    if mask:                                                          # фоновый запуск: адресов нет нигде
                        for address in self.RECIPIENTS + (USER,):
                            self.assertNotIn(address, everything)

    def test_the_status_shows_no_password_and_no_addresses_either(self):
        done = self.batch(3, connector=Connector(**self.scenarios()[0]))
        with mock.patch.dict(os.environ, {"SAVE_SENT_COPY": "1"}):
            text = "\n".join(mailer.status_lines(done.path, 40))
        self.assertIn("не удалось сохранить 3", text)
        self.assertNotIn(SECRET, text)
        for address in self.RECIPIENTS:
            self.assertNotIn(address, text)


class StatusTest(BatchCase):
    def test_status_counts_the_copies_that_could_not_be_saved_in_the_last_run(self):
        done = self.batch(3, connector=Connector(append_errors=[None, imaplib.IMAP4.abort("socket error: EOF"), None]))
        self.assertEqual(done.code, 0)
        self.assertEqual(len(done.connector.calls), 2)                             # после обрыва открыто новое соединение
        self.assertEqual(len(done.connector.appended), 2)
        self.assertIn("Копии в «Отправленные»: сохранено 2, не сохранено 1.", done.logs[-1])
        with open(os.path.join(done.folder, sentcopy.REPORT_NAME), encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertEqual((report["saved"], report["failed"]), (2, 1))
        with mock.patch.dict(os.environ, {"SAVE_SENT_COPY": "1"}):
            table_status = "\n".join(mailer.status_lines(done.path, 40))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(mailer.main([done.folder, "--status", "--env", os.devnull, "--settings", os.devnull]), 0)
        for text in (table_status, out.getvalue()):
            self.assertIn("сохранено 2, не удалось сохранить 1", text)
            self.assertIn("последняя причина: ", text)
        with mock.patch.dict(os.environ, {"SAVE_SENT_COPY": "0"}):
            self.assertIn("выключена", "\n".join(mailer.status_lines(done.path, 40)))

    def test_a_folder_without_any_run_says_so(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"SAVE_SENT_COPY": "1"}):
            self.assertIn("ещё не было", sentcopy.status_line(folder))


class SettingsTest(BatchCase):
    def test_defaults_and_the_off_switch(self):
        self.assertEqual(cfg.resolve_imap({}), ("imap.mail.ru", 993))
        self.assertEqual(cfg.resolve_imap({"MAILRU_IMAP_HOST": " imap.example.ru ", "MAILRU_IMAP_PORT": "143"}), ("imap.example.ru", 143))
        for bad in ("abc", "0", "70000", "-1"):
            with self.assertRaises(ValueError, msg=bad):
                cfg.resolve_imap({"MAILRU_IMAP_PORT": bad})
        with self.assertRaises(ValueError):
            cfg.resolve_imap({"MAILRU_IMAP_HOST": "imap.mail.ru/x"})
        self.assertTrue(cfg.resolve_sent_copy({}))
        self.assertTrue(cfg.resolve_sent_copy({"SAVE_SENT_COPY": "1"}))
        for off in ("0", " 0 ", "false", "OFF", "no"):
            self.assertFalse(cfg.resolve_sent_copy({"SAVE_SENT_COPY": off}), off)
        self.assertIsNone(sentcopy.make_saver(USER, SECRET, {"SAVE_SENT_COPY": "0"}))
        self.assertEqual(sentcopy.make_savers([SimpleNamespace(id="1", user=USER, password=SECRET)], {"SAVE_SENT_COPY": "0"}), {})

    def test_without_copiers_there_is_no_copy_no_line_and_no_report(self):
        done = self.batch(3, copy=False)
        self.assertEqual((done.connector.calls, len(done.server.sent)), ([], 3))
        self.assertFalse(any("копия" in line.lower() for line in done.logs))
        self.assertFalse(os.path.exists(os.path.join(done.folder, sentcopy.REPORT_NAME)))

    def test_a_wrong_imap_port_is_reported_but_does_not_stop_the_send(self):
        saver = sentcopy.make_saver(USER, SECRET, {"MAILRU_IMAP_PORT": "abc"}, connect=Connector())
        done = self.batch(2, saver=saver)
        self.assertEqual(done.code, 0)
        self.assertEqual(len(done.server.sent), 2)
        self.assertEqual(sum("MAILRU_IMAP_PORT" in line for line in done.logs), 2)

    def run_main(self, switch):
        """Сквозной прогон mailer.main: ящик из окружения, SMTP и IMAP подставные, адреса скрыты."""
        source = TempTable(table("a@x.ru;Тема;Текст", "b@x.ru;Тема;Текст"))
        self.addCleanup(source.close)
        env = {key: value for key, value in os.environ.items() if not key.startswith(("MAILRU_", "MAIL_"))}
        env.update(MAILRU_USER=USER, MAILRU_APP_PASSWORD=SECRET, SAVE_SENT_COPY=switch)
        server, connector = FakeServer(), Connector()
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(mailer, "MailruSender", lambda user, password, port=465, **kw: MailruSender(user, password, connect=lambda *a: server)), \
                mock.patch.object(sentcopy, "default_connect", connector), contextlib.redirect_stdout(out):
            code = mailer.main([source.path, "--send", "--limit", "0", "--pause", "0", "--env", os.devnull,
                                "--settings", os.devnull, "--mask-addresses"])
        return code, out.getvalue(), server, connector

    def test_main_copies_by_default_and_not_with_save_sent_copy_0(self):
        code, text, server, connector = self.run_main("1")
        self.assertEqual(code, 0, text)
        self.assertEqual(len(server.sent), 2)
        self.assertEqual(len(connector.appended), 2)
        self.assertEqual(text.count("копия в Отправленные: ок"), 2)
        self.assertNotIn(SECRET, text)
        code, text, server, connector = self.run_main("0")
        self.assertEqual(code, 0, text)
        self.assertEqual(len(server.sent), 2)
        self.assertEqual((connector.calls, connector.appended), ([], []))
        self.assertNotIn("Отправленные", text)


if __name__ == "__main__":
    unittest.main()
