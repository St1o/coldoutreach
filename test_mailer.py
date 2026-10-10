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


class EnvFileTest(unittest.TestCase):
    KEYS = ("MAILRU_USER", "MAILRU_APP_PASSWORD", "MAIL_FROM_NAME")

    def setUp(self):
        from unittest import mock
        patcher = mock.patch.dict(os.environ)             # всё, что программа впишет в окружение, откатится
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in self.KEYS:
            os.environ.pop(key, None)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = os.path.join(self.dir.name, "local.env")

    def write(self, text):
        with open(self.env, "w", encoding="utf-8-sig") as fh:
            fh.write(text)

    def test_parses_values_comments_and_quotes(self):
        self.write("# комментарий\n\nMAILRU_USER = a@b.ru \nMAILRU_APP_PASSWORD=\"abc def\"\n"
                   "PORT='8001'\n  # MAIL_EXAMPLE_TO=x@y.ru\nbroken line\n")
        self.assertEqual(mailer.read_env_file(self.env),
                         {"MAILRU_USER": "a@b.ru", "MAILRU_APP_PASSWORD": "abc def", "PORT": "8001"})

    def test_file_fills_missing_variables(self):
        self.write(f"MAILRU_USER={USER}\nMAILRU_APP_PASSWORD={SECRET}\n")
        self.assertTrue(mailer.load_env_file(self.env))
        self.assertEqual((os.environ["MAILRU_USER"], os.environ["MAILRU_APP_PASSWORD"]), (USER, SECRET))

    def test_real_environment_wins_over_file_but_empty_one_does_not(self):
        self.write(f"MAILRU_USER={USER}\nMAILRU_APP_PASSWORD={SECRET}\n")
        os.environ["MAILRU_USER"] = "other@mail.ru"
        os.environ["MAILRU_APP_PASSWORD"] = ""
        mailer.load_env_file(self.env)
        self.assertEqual(os.environ["MAILRU_USER"], "other@mail.ru")
        self.assertEqual(os.environ["MAILRU_APP_PASSWORD"], SECRET)

    def test_empty_values_in_file_do_not_create_variables(self):
        self.write("MAILRU_USER=\nMAILRU_APP_PASSWORD=\n")
        mailer.load_env_file(self.env)
        self.assertNotIn("MAILRU_USER", os.environ)

    def test_missing_file_is_not_an_error(self):
        self.assertFalse(mailer.load_env_file(self.env))

    def test_main_takes_access_from_the_file(self):
        from unittest import mock
        self.write(f"MAILRU_USER={USER}\nMAILRU_APP_PASSWORD={SECRET}\nMAIL_FROM_NAME=Пётр\n")
        with mock.patch.object(mailer, "run", return_value=0) as fake_run:
            self.assertEqual(mailer.main(["letters.csv", "--send", "--env", self.env]), 0)
        args = fake_run.call_args.args
        self.assertEqual((args[4], args[5], args[6]), (USER, SECRET, "Пётр"))

    def test_from_name_option_beats_the_file(self):
        from unittest import mock
        self.write(f"MAILRU_USER={USER}\nMAILRU_APP_PASSWORD={SECRET}\nMAIL_FROM_NAME=Пётр\n")
        with mock.patch.object(mailer, "run", return_value=0) as fake_run:
            mailer.main(["letters.csv", "--from-name", "Анна", "--env", self.env])
        self.assertEqual(fake_run.call_args.args[6], "Анна")

    def test_check_run_works_without_any_access(self):
        table_file = TempTable(table("a@b.ru;Тема;Текст"))
        self.addCleanup(table_file.close)
        self.assertEqual(mailer.main([table_file.path, "--env", self.env]), 0)   # файла local.env нет

    def test_unreadable_file_is_reported(self):
        with open(self.env, "wb") as fh:
            fh.write("MAILRU_USER=ящик".encode("cp1251"))                         # не UTF-8
        self.assertEqual(mailer.main(["letters.csv", "--env", self.env]), 1)


class StoplistTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def test_reads_addresses_domains_comments_and_case(self):
        with open(self.path("stoplist.txt"), "w", encoding="utf-8-sig") as fh:
            fh.write("# отписались\nBad@Mail.ru\n\n@registrar.ru  # сервис регистрации\n")
        stop = mailer.read_stoplist(self.path("stoplist.txt"))
        self.assertEqual(stop, {"bad@mail.ru", "@registrar.ru"})
        self.assertTrue(mailer.is_stopped("BAD@mail.ru", stop))
        self.assertTrue(mailer.is_stopped("anyone@Registrar.ru", stop))
        self.assertFalse(mailer.is_stopped("good@mail.ru", stop))

    def test_stopped_addresses_are_skipped_but_not_marked_as_handled(self):
        logs, server = [], FakeServer()
        t = TempTable(table("bad@b.ru;Т;Х", "ok@b.ru;Т;Х", "x@reg.ru;Т;Х"))
        self.addCleanup(t.close)
        sender = MailruSender(USER, SECRET, connect=lambda *a: server)
        code = run(t.path, sender=sender, log=logs.append, sleep=lambda s: None, send=True,
                   stoplist=frozenset({"bad@b.ru", "@reg.ru"}))
        self.assertEqual(code, 0)
        self.assertEqual([m["To"] for m in server.sent], ["ok@b.ru"])
        self.assertIn("В стоп-листе: 2", "\n".join(logs))
        self.assertEqual(set(mailer.load_state(mailer.state_path(t.path))), {"ok@b.ru"})

    def test_main_uses_stoplist_next_to_the_table(self):
        from unittest import mock
        t = TempTable(table("a@b.ru;Т;Х"))
        self.addCleanup(t.close)
        with open(os.path.join(os.path.dirname(t.path), "stoplist.txt"), "w", encoding="utf-8") as fh:
            fh.write("a@b.ru\n")
        with mock.patch.object(mailer, "run", return_value=0) as fake_run:
            mailer.main([t.path, "--env", os.path.join(self.dir.name, "нет.env")])
        self.assertEqual(fake_run.call_args.kwargs["stoplist"], {"a@b.ru"})
        self.assertTrue(fake_run.call_args.kwargs["unsubscribe"])

    def test_main_explicit_missing_stoplist_is_an_error(self):
        t = TempTable(table("a@b.ru;Т;Х"))
        self.addCleanup(t.close)
        self.assertEqual(mailer.main([t.path, "--stoplist", os.path.join(self.dir.name, "нет.txt"),
                                      "--env", os.path.join(self.dir.name, "нет.env")]), 1)


class DailyLimitTest(unittest.TestCase):
    NOW = mailer.datetime(2026, 10, 12, 12, 0, tzinfo=mailer.timezone.utc)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.logs, self.server = [], FakeServer()

    def journal(self, name, entries):
        with open(os.path.join(self.dir.name, name), "w", encoding="utf-8") as fh:
            mailer.json.dump(entries, fh)

    def go(self, rows, **kw):
        path = os.path.join(self.dir.name, "letters_2.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(table(*rows))
        sender = MailruSender(USER, SECRET, connect=lambda *a: self.server)
        return run(path, sender=sender, log=self.logs.append, sleep=lambda s: None, send=True,
                   now=self.NOW, **kw)

    def test_counts_last_24_hours_across_all_journals_of_the_folder(self):
        recent, old = "2026-10-12T03:00:00+00:00", "2026-10-11T11:59:00+00:00"
        self.journal("letters_1.sent.json", {
            "a@x.ru": {"status": "sent", "time": recent}, "b@x.ru": {"status": "sent", "time": recent},
            "c@x.ru": {"status": "sent", "time": old},                 # больше 24 часов назад
            "d@x.ru": {"status": "rejected", "time": recent}})         # не отправлено
        self.journal("letters_test.sent.json", {"me@x.ru": {"status": "sent", "time": recent}})
        self.assertEqual(mailer.sent_last_24h(self.dir.name, self.NOW), 3)

    def test_only_the_remaining_room_is_sent(self):
        self.journal("letters_1.sent.json", {f"s{i}@x.ru": {"status": "sent", "time": "2026-10-12T09:00:00+00:00"}
                                             for i in range(3)})
        code = self.go(["a@b.ru;Т;Х", "c@b.ru;Т;Х", "d@b.ru;Т;Х"], daily_limit=4, pause=0)
        self.assertEqual(code, 0)
        self.assertEqual([m["To"] for m in self.server.sent], ["a@b.ru"])
        self.assertIn("уже отправлено 3, можно ещё 1", "\n".join(self.logs))

    def test_exhausted_limit_sends_nothing(self):
        self.journal("letters_1.sent.json", {f"s{i}@x.ru": {"status": "sent", "time": "2026-10-12T09:00:00+00:00"}
                                             for i in range(2)})
        self.assertEqual(self.go(["a@b.ru;Т;Х"], daily_limit=2, pause=0), 0)
        self.assertEqual(self.server.sent, [])
        self.assertIn("Отправлять нечего", "\n".join(self.logs))

    def test_zero_means_no_daily_limit(self):
        self.go(["a@b.ru;Т;Х", "c@b.ru;Т;Х"], daily_limit=0, pause=0)
        self.assertEqual(len(self.server.sent), 2)


class JournalMetaAndUnsubscribeTest(unittest.TestCase):
    def test_extra_columns_and_subject_go_to_the_journal(self):
        t = TempTable(table("a@b.ru;Тема;Текст;3;ЕГРЮЛ", header="Почта;Тема;Текст;Группа;Источник адреса"))
        self.addCleanup(t.close)
        letters, _ = read_letters(t.path)
        self.assertEqual(letters[0].meta, {"Группа": "3", "Источник адреса": "ЕГРЮЛ"})
        server = FakeServer()
        run(t.path, send=True, sender=MailruSender(USER, SECRET, connect=lambda *a: server), log=lambda m: None)
        entry = mailer.load_state(mailer.state_path(t.path))["a@b.ru"]
        self.assertEqual((entry["status"], entry["subject"]), ("sent", "Тема"))
        self.assertEqual(entry["meta"], {"Группа": "3", "Источник адреса": "ЕГРЮЛ"})

    def test_plain_table_has_no_meta(self):
        t = TempTable(table("a@b.ru;Тема;Текст"))
        self.addCleanup(t.close)
        self.assertEqual(read_letters(t.path)[0][0].meta, {})

    def test_list_unsubscribe_header_is_optional_and_points_to_the_sender(self):
        letter = Letter("a@b.ru", "Т", "Х")
        self.assertIsNone(build_message(letter, USER)["List-Unsubscribe"])
        msg = build_message(letter, USER, unsubscribe=True)
        self.assertEqual(msg["List-Unsubscribe"], f"<mailto:{USER}?subject=unsubscribe>")
        msg.as_bytes().decode("ascii")

    def test_run_adds_the_header_when_asked(self):
        server = FakeServer()
        t = TempTable(table("a@b.ru;Т;Х"))
        self.addCleanup(t.close)
        run(t.path, send=True, unsubscribe=True, log=lambda m: None,
            sender=MailruSender(USER, SECRET, connect=lambda *a: server))
        self.assertIn("List-Unsubscribe", server.sent[0])


class SslContextTest(unittest.TestCase):
    def test_verification_stays_on_and_certifi_roots_are_added(self):
        import ssl
        context = mailer.make_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreater(len(context.get_ca_certs()), len(ssl.create_default_context().get_ca_certs()))

    def test_extra_ca_file_from_environment_must_exist(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"MAILRU_CA_FILE": "/нет/такого/файла.pem"}):
            with self.assertRaises(OSError):
                mailer.make_ssl_context()


if __name__ == "__main__":
    unittest.main()
