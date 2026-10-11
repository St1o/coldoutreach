import json
import os
import smtplib
import tempfile
import time
import unittest
from datetime import datetime, timezone

import mailer
from mailer import run
from mailing import config as cfg, journal
from mailing import lock as sendlock
from mailing import smtp as smtpmod
from mailing import table as lettertable
from mailing.smtp import MailruSender, build_message
from mailing.table import Letter, read_letters

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
        with self.assertRaises(lettertable.LettersError) as ctx:
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
        with self.assertRaises(lettertable.LettersError):
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
        self.assertFalse(os.path.exists(journal.state_path(self.path)))

    def test_dry_run_flags_bad_rows_with_exit_code_1(self):
        self.assertEqual(self.go(table("плохо;Тема;Текст", "a@b.ru;Тема;Текст")), 1)

    def test_sends_with_pause_between_letters_and_logs_them(self):
        code = self.go(table("a@b.ru;Т1;Х", "c@d.ru;Т2;Х", "e@f.ru;Т3;Х"), send=True, pause=7)
        self.assertEqual(code, 0)
        self.assertEqual([m["To"] for m in self.server.sent], ["a@b.ru", "c@d.ru", "e@f.ru"])
        self.assertEqual(self.sleeps, [7, 7])                  # между письмами, не перед первым и не после последнего
        self.assertEqual(self.server.logins, [(USER, SECRET)])
        self.assertTrue(self.server.quit_called)
        state = journal.load_state(journal.state_path(self.path))
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
        state = journal.load_state(journal.state_path(self.path))
        self.assertEqual(state["bad@b.ru"]["status"], "rejected")

    def test_temporary_recipient_refusals_are_retried_next_time_then_stop(self):
        server = FakeServer(fail={f"u{i}@b.ru": refused(450, b"try later") for i in range(3)})
        rows = [f"u{i}@b.ru;Т;Х" for i in range(3)] + ["z@b.ru;Т;Х"]
        code = self.go(table(*rows), server=server, send=True, pause=0)
        self.assertEqual(code, 5)
        self.assertEqual(server.sent, [])
        self.assertEqual(journal.load_state(journal.state_path(self.path)), {})

    def test_rate_limit_stops_at_once_and_keeps_letter_for_next_run(self):
        server = FakeServer(fail={"c@d.ru": smtplib.SMTPDataError(451, b"Ratelimit exceeded for mailbox. Try again later.")})
        code = self.go(table("a@b.ru;Т;Х", "c@d.ru;Т;Х", "e@f.ru;Т;Х"), server=server, send=True, pause=0)
        self.assertEqual(code, 3)
        self.assertEqual([m["To"] for m in server.sent], ["a@b.ru"])
        self.assertEqual(list(journal.load_state(journal.state_path(self.path))), ["a@b.ru"])
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
        self.assertEqual(cfg.read_env_file(self.env),
                         {"MAILRU_USER": "a@b.ru", "MAILRU_APP_PASSWORD": "abc def", "PORT": "8001"})

    def test_file_fills_missing_variables(self):
        self.write(f"MAILRU_USER={USER}\nMAILRU_APP_PASSWORD={SECRET}\n")
        self.assertTrue(cfg.load_env_file(self.env))
        self.assertEqual((os.environ["MAILRU_USER"], os.environ["MAILRU_APP_PASSWORD"]), (USER, SECRET))

    def test_real_environment_wins_over_file_but_empty_one_does_not(self):
        self.write(f"MAILRU_USER={USER}\nMAILRU_APP_PASSWORD={SECRET}\n")
        os.environ["MAILRU_USER"] = "other@mail.ru"
        os.environ["MAILRU_APP_PASSWORD"] = ""
        cfg.load_env_file(self.env)
        self.assertEqual(os.environ["MAILRU_USER"], "other@mail.ru")
        self.assertEqual(os.environ["MAILRU_APP_PASSWORD"], SECRET)

    def test_empty_values_in_file_do_not_create_variables(self):
        self.write("MAILRU_USER=\nMAILRU_APP_PASSWORD=\n")
        cfg.load_env_file(self.env)
        self.assertNotIn("MAILRU_USER", os.environ)

    def test_missing_file_is_not_an_error(self):
        self.assertFalse(cfg.load_env_file(self.env))

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

    def test_several_entries_in_one_line_split_by_comma_semicolon_or_space(self):
        with open(self.path("stoplist.txt"), "w", encoding="utf-8-sig") as fh:
            fh.write("One@Yandex.ru,fort4@x.ru, fort5@x.ru ; @reg.ru   # три адреса и домен\ntwo@x.ru\n")
        stop = cfg.read_stoplist(self.path("stoplist.txt"))
        self.assertEqual(stop, {"one@yandex.ru", "fort4@x.ru", "fort5@x.ru", "@reg.ru", "two@x.ru"})
        self.assertTrue(cfg.is_stopped("FORT5@x.ru", stop))

    def test_reads_addresses_domains_comments_and_case(self):
        with open(self.path("stoplist.txt"), "w", encoding="utf-8-sig") as fh:
            fh.write("# отписались\nBad@Mail.ru\n\n@registrar.ru  # сервис регистрации\n")
        stop = cfg.read_stoplist(self.path("stoplist.txt"))
        self.assertEqual(stop, {"bad@mail.ru", "@registrar.ru"})
        self.assertTrue(cfg.is_stopped("BAD@mail.ru", stop))
        self.assertTrue(cfg.is_stopped("anyone@Registrar.ru", stop))
        self.assertFalse(cfg.is_stopped("good@mail.ru", stop))

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
        self.assertEqual(set(journal.load_state(journal.state_path(t.path))), {"ok@b.ru"})

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
    NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.logs, self.server = [], FakeServer()

    def journal(self, name, entries):
        with open(os.path.join(self.dir.name, name), "w", encoding="utf-8") as fh:
            json.dump(entries, fh)

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
        self.assertEqual(journal.sent_last_24h(self.dir.name, self.NOW), 3)

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
        entry = journal.load_state(journal.state_path(t.path))["a@b.ru"]
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


class MaskingTest(unittest.TestCase):
    def test_mask_and_redact(self):
        self.assertEqual(cfg.mask_email("Ivan.Petrov@Mail.ru"), "I***@Mail.ru")
        self.assertEqual(cfg.mask_email("мусор"), "***")
        self.assertEqual(cfg.redact("550 <a.b@x.ru>: no such user, cc c@y.com"),
                         "550 <a***@x.ru>: no such user, cc c***@y.com")

    def test_masked_run_prints_no_addresses(self):
        logs = []
        server = FakeServer(fail={"bad@b.ru": refused(550, b"<bad@b.ru>: no such user")})
        t = TempTable(table("ok@b.ru;Т;Х", "bad@b.ru;Т;Х"))
        self.addCleanup(t.close)
        run(t.path, send=True, mask=True, pause=0, log=logs.append, sleep=lambda s: None,
            sender=MailruSender(USER, SECRET, connect=lambda *a: server))
        text = "\n".join(logs)
        self.assertNotIn("@b.ru", text.replace("***@b.ru", ""))
        self.assertIn("o***@b.ru: отправлено", text)
        self.assertIn("b***@b.ru: адрес не принят", text)

    def test_masked_check_run(self):
        logs = []
        t = TempTable(table("ok@b.ru;Т;Х"))
        self.addCleanup(t.close)
        run(t.path, mask=True, log=logs.append)
        self.assertNotIn("ok@b.ru", "\n".join(logs))


class SendLockTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, sendlock.LOCK_NAME)

    def test_lock_is_taken_and_released(self):
        with sendlock.SendLock(self.dir.name):
            self.assertTrue(os.path.exists(self.path))
            self.assertEqual(sendlock.lock_holder(self.dir.name), os.getpid())
        self.assertFalse(os.path.exists(self.path))

    def test_second_sender_is_refused_while_the_first_is_alive(self):
        with open(self.path, "w") as fh:
            fh.write(str(os.getppid()))                      # родительский процесс жив
        with self.assertRaises(sendlock.Locked) as ctx:
            sendlock.SendLock(self.dir.name).__enter__()
        self.assertEqual(ctx.exception.pid, os.getppid())
        self.assertTrue(os.path.exists(self.path))           # чужой замок не трогаем

    def test_lock_of_a_dead_process_is_taken_over(self):
        import subprocess
        import sys
        done = subprocess.Popen([sys.executable, "-c", "pass"])
        done.wait()
        with open(self.path, "w") as fh:
            fh.write(str(done.pid))
        with sendlock.SendLock(self.dir.name):
            self.assertEqual(sendlock.lock_holder(self.dir.name), os.getpid())

    def test_forgotten_old_lock_is_ignored(self):
        with open(self.path, "w") as fh:
            fh.write(str(os.getppid()))
        old = time.time() - sendlock.LOCK_MAX_AGE - 10
        os.utime(self.path, (old, old))
        self.assertEqual(sendlock.lock_holder(self.dir.name), 0)
        self.assertFalse(os.path.exists(self.path))

    def test_main_holds_the_lock_while_sending_and_refuses_a_second_run(self):
        from unittest import mock
        t = TempTable(table("a@b.ru;Т;Х"))
        self.addCleanup(t.close)
        lock = os.path.join(os.path.dirname(t.path), sendlock.LOCK_NAME)
        seen = []

        def fake_run(*a, **k):
            seen.append(os.path.exists(lock))
            with open(lock, "w") as fh:                       # как если бы другой процесс взял замок
                fh.write(str(os.getppid()))
            self.assertEqual(mailer.main([t.path, "--send", "--env", self.path + ".нет"]), 1)
            return 0

        with mock.patch.object(mailer, "run", side_effect=fake_run):
            mailer.main([t.path, "--send", "--env", self.path + ".нет"])
        self.assertEqual(seen, [True])


class ProjectFilesLocationTest(unittest.TestCase):
    def test_local_env_and_settings_live_in_the_project_root_next_to_mailer_py(self):
        root = os.path.dirname(os.path.abspath(mailer.__file__))
        self.assertEqual(os.path.dirname(cfg.ENV_FILE), root)
        self.assertEqual(os.path.dirname(cfg.SETTINGS_FILE), root)


class DailyLimitSettingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "settings.json")

    def test_parse_accepts_whole_numbers_in_range_only(self):
        self.assertEqual(cfg.parse_daily_limit(" 12 "), 12)
        self.assertEqual(cfg.parse_daily_limit(50), 50)                       # выше 50 с /mail не ставится
        for bad in ("0", "51", "201", "-1", "abc", "", None, "1.5"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                cfg.parse_daily_limit(bad)

    def test_save_and_load_round_trip_and_broken_file_is_empty(self):
        self.assertEqual(cfg.load_settings(self.path), {})
        cfg.save_settings({cfg.DAILY_LIMIT_KEY: 9}, self.path)
        self.assertEqual(cfg.load_settings(self.path), {cfg.DAILY_LIMIT_KEY: 9})
        with open(self.path, "w") as fh:
            fh.write("{не json")
        self.assertEqual(cfg.load_settings(self.path), {})

    def test_order_of_sources(self):
        env = {"MAIL_DAILY_LIMIT": "15"}
        self.assertEqual(cfg.resolve_daily_limit(None, self.path, {}), (40, "по умолчанию"))
        self.assertEqual(cfg.resolve_daily_limit(None, self.path, env), (15, "local.env"))
        cfg.save_settings({cfg.DAILY_LIMIT_KEY: 9}, self.path)
        self.assertEqual(cfg.resolve_daily_limit(None, self.path, env), (9, "сайт"))
        self.assertEqual(cfg.resolve_daily_limit(3, self.path, env), (3, "ключ --daily-limit"))
        self.assertEqual(cfg.resolve_daily_limit(0, self.path, env), (0, "ключ --daily-limit"))

    def test_bad_site_value_is_skipped_but_bad_env_value_is_an_error(self):
        cfg.save_settings({cfg.DAILY_LIMIT_KEY: 999}, self.path)
        self.assertEqual(cfg.resolve_daily_limit(None, self.path, {"MAIL_DAILY_LIMIT": "15"}), (15, "local.env"))
        with self.assertRaises(ValueError):
            cfg.resolve_daily_limit(None, self.path, {"MAIL_DAILY_LIMIT": "много"})

    def test_main_takes_the_limit_saved_on_the_site(self):
        from unittest import mock
        cfg.save_settings({cfg.DAILY_LIMIT_KEY: 9}, self.path)
        t = TempTable(table("a@b.ru;Т;Х"))
        self.addCleanup(t.close)
        with mock.patch.dict(os.environ):
            os.environ["MAIL_DAILY_LIMIT"] = "15"
            with mock.patch.object(mailer, "run", return_value=0) as fake_run:
                mailer.main([t.path, "--env", os.path.join(self.dir.name, "нет.env"), "--settings", self.path])
        self.assertEqual(fake_run.call_args.kwargs["daily_limit"], 9)


class WindowsPidTest(unittest.TestCase):
    """Логика проверки процесса на Windows на подставном kernel32 (на других системах настоящий недоступен)."""

    class Kernel32:
        def __init__(self, opens=True, exit_code=259, query_ok=True):
            self.opens, self.exit_code, self.query_ok, self.closed = opens, exit_code, query_ok, []

        def OpenProcess(self, access, inherit, pid):
            return 77 if self.opens else 0

        def GetExitCodeProcess(self, handle, ref):
            if self.query_ok:
                ref._obj.value = self.exit_code
            return 1 if self.query_ok else 0

        def CloseHandle(self, handle):
            self.closed.append(handle)

    def alive(self, **kw):
        fake = self.Kernel32(**kw)
        return sendlock._windows_pid_alive(123, fake), fake

    def test_running_process_is_alive_and_handle_is_closed(self):
        result, fake = self.alive()
        self.assertTrue(result)
        self.assertEqual(fake.closed, [77])

    def test_finished_process_whose_handle_is_still_open_is_not_alive(self):
        result, fake = self.alive(exit_code=0)             # так было у тестов: Popen держит дескриптор
        self.assertFalse(result)
        self.assertEqual(fake.closed, [77])

    def test_missing_process_is_not_alive(self):
        self.assertFalse(self.alive(opens=False)[0])

    def test_unknown_state_counts_as_alive(self):
        self.assertTrue(self.alive(query_ok=False)[0])


class StatusAndBackgroundTest(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("MAILRU_USER", "MAILRU_APP_PASSWORD", "MAIL_DAILY_LIMIT"):
            os.environ.pop(key, None)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.table = TempTable(table("a@b.ru;Т;Х", "c@b.ru;Т;Х", "d@b.ru;Т;Х"))
        self.addCleanup(self.table.close)
        self.folder = os.path.dirname(self.table.path)
        self.env = os.path.join(self.dir.name, "local.env")
        self.nowhere_json = os.path.join(self.dir.name, "нет.json")          # настоящий settings.json не читаем

    def write_env(self, text=f"MAILRU_USER={USER}\nMAILRU_APP_PASSWORD={SECRET}\n"):
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_status_counts_without_addresses(self):
        now = journal.now_iso()
        journal.save_state(journal.state_path(self.table.path), {
            "a@b.ru": {"status": "sent", "time": now}, "c@b.ru": {"status": "rejected", "time": now}})
        with open(os.path.join(self.folder, mailer.LOG_NAME), "w", encoding="utf-8") as fh:
            fh.write("--- запуск ---\n[1/3] a***@b.ru: отправлено\n")
        lines = mailer.status_lines(self.table.path, 40)
        text = "\n".join(lines)
        self.assertIn("Писем в таблице: 3; отправлено: 1; адрес не принят: 1; ещё не отправлялось: 1.", text)
        self.assertIn("За последние 24 часа по папке отправлено 1 из 40.", text)
        self.assertIn("Сейчас отправка не идёт.", text)
        self.assertIn("a***@b.ru: отправлено", text)
        self.assertNotIn("a@b.ru", text)

    def test_status_command_sends_nothing(self):
        self.write_env()
        self.assertEqual(mailer.main([self.table.path, "--status", "--env", self.env]), 0)

    def test_background_start_returns_at_once_and_hides_addresses(self):
        from unittest import mock
        self.write_env()
        with mock.patch.object(mailer.subprocess, "Popen") as popen:
            popen.return_value.pid = 4242
            code = mailer.main([self.table.path, "--send", "--limit", "5", "--background", "--env", self.env])
        self.assertEqual(code, 0)
        command = popen.call_args.args[0]
        self.assertNotIn("--background", command)
        self.assertIn("--mask-addresses", command)
        self.assertEqual(command[2], self.table.path)
        self.assertIn("--send", command)
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONUNBUFFERED"], "1")
        self.assertTrue(os.path.exists(os.path.join(self.folder, mailer.LOG_NAME)))

    def test_background_refuses_without_access_without_send_or_while_busy(self):
        from unittest import mock
        with mock.patch.object(mailer.subprocess, "Popen") as popen:
            self.write_env("")
            self.assertEqual(mailer.main([self.table.path, "--send", "--background", "--env", self.env]), 1)
            self.write_env()
            self.assertEqual(mailer.main([self.table.path, "--background", "--env", self.env]), 1)
            with open(os.path.join(self.folder, sendlock.LOCK_NAME), "w") as fh:
                fh.write(str(os.getppid()))
            self.assertEqual(mailer.main([self.table.path, "--send", "--background", "--env", self.env]), 1)
            popen.assert_not_called()

    def test_daily_limit_comes_from_local_env_unless_given(self):
        from unittest import mock
        self.write_env(f"MAILRU_USER={USER}\nMAILRU_APP_PASSWORD={SECRET}\nMAIL_DAILY_LIMIT=12\n")
        with mock.patch.object(mailer, "run", return_value=0) as fake_run:
            mailer.main([self.table.path, "--env", self.env, "--settings", self.nowhere_json])
            self.assertEqual(fake_run.call_args.kwargs["daily_limit"], 12)
            mailer.main([self.table.path, "--env", self.env, "--settings", self.nowhere_json, "--daily-limit", "7"])
            self.assertEqual(fake_run.call_args.kwargs["daily_limit"], 7)
        self.write_env("MAIL_DAILY_LIMIT=много\n")
        os.environ.pop("MAIL_DAILY_LIMIT", None)
        self.assertEqual(mailer.main([self.table.path, "--env", self.env, "--settings", self.nowhere_json]), 1)


class SslContextTest(unittest.TestCase):
    def test_verification_stays_on_and_certifi_roots_are_added(self):
        import ssl
        context = smtpmod.make_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreater(len(context.get_ca_certs()), len(ssl.create_default_context().get_ca_certs()))

    def test_extra_ca_file_from_environment_must_exist(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"MAILRU_CA_FILE": "/нет/такого/файла.pem"}):
            with self.assertRaises(OSError):
                smtpmod.make_ssl_context()


if __name__ == "__main__":
    unittest.main()
