import contextlib
import io
import json
import os
import smtplib
import ssl
import tempfile
import unittest
from unittest import mock

import check_login
from mailing import config

USER1, USER2 = "chief.test@mail.ru", "second.test@mail.ru"
PASS1, PASS2 = "Zq9wXk3LmN7pRt2Y", "Ab4cDe5FgH6iJk7L"          # выдуманные
REJECT = b"5.7.0 NEOBHODIM parol prilozheniya https://help.mail.ru/mail/security/protection/external / Application password is REQUIRED"
GOOD = f"MAILRU_USER={USER1}\nMAILRU_APP_PASSWORD={PASS1}\nMAILRU_USER_2={USER2}\nMAILRU_APP_PASSWORD_2={PASS2}\n"


class FakeSMTP:
    """Подставной сервер: принимает только пары из accounts, каждую команду AUTH записывает в общий список calls."""

    def __init__(self, accounts, calls):
        self.accounts, self.calls = accounts, calls
        self.esmtp_features = {"auth": "PLAIN LOGIN"}

    def ehlo_or_helo_if_needed(self):
        pass

    def auth_plain(self, challenge=None):
        return ""

    def auth(self, mechanism, method):
        self.calls.append((mechanism, self.user, self.password))
        if self.accounts.get(self.user) == self.password:
            return 235, b"2.7.0 Authentication successful"
        raise smtplib.SMTPAuthenticationError(535, REJECT)

    def quit(self):
        pass


class LoginCheckTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in [k for k in os.environ if k.upper().startswith(("MAIL", "SMTP"))]:
            os.environ.pop(key)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = os.path.join(self.dir.name, "local.env")
        self.ledger = os.path.join(self.dir.name, "check_login.json")
        self.report = os.path.join(self.dir.name, "check_login.txt")
        self.calls, self.connects, self.accounts = [], [], {USER1: PASS1, USER2: PASS2}

    def write(self, text):
        with open(self.env, "w", encoding="utf-8") as fh:
            fh.write(text)

    def connect(self, host, port, timeout):
        self.connects.append((host, port))
        return FakeSMTP(self.accounts, self.calls)

    def run_check(self, **kwargs):
        options = dict(env_path=self.env, project=self.dir.name, ledger_path=self.ledger, report_path=self.report,
                       connect=self.connect, deep=False, quiet=True)
        options.update(kwargs)
        return check_login.run(**options)

    @staticmethod
    def read(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def text(self, lines):
        return "\n".join(lines)

    # ---- вход ----
    def test_both_accepted_control_mailbox_goes_first_one_auth_each_and_no_letters(self):
        self.write(GOOD)
        code, lines = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual([(m, u) for m, u, _ in self.calls], [("PLAIN", USER2), ("PLAIN", USER1)])
        self.assertEqual(self.connects, [("smtp.mail.ru", 465)] * 2)
        self.assertIn("Оба входа приняты", self.text(lines))

    def test_main_mailbox_rejected_while_control_is_accepted(self):
        self.write(GOOD)
        self.accounts[USER1] = "другой пароль"
        code, lines = self.run_check()
        self.assertEqual(code, 2)
        self.assertEqual(len(self.calls), 2)
        text = self.text(lines)
        self.assertIn("NEOBHODIM parol prilozheniya", text)
        self.assertIn("Ящик 2 принят, ящик 1 отклонён", text)

    def test_control_mailbox_rejected_means_mailbox_one_is_not_touched(self):
        self.write(GOOD)
        self.accounts[USER2] = "другой пароль"
        code, lines = self.run_check()
        self.assertEqual(code, 2)
        self.assertEqual([u for _, u, _ in self.calls], [USER2])              # ящик 1 не пробовали
        self.assertIn("Контрольный ящик 2 тоже отклонён", self.text(lines))

    def test_without_control_mailbox_only_mailbox_one_is_tried(self):
        self.write(f"MAILRU_USER={USER1}\nMAILRU_APP_PASSWORD={PASS1}\n")
        code, lines = self.run_check()
        self.assertEqual((code, [u for _, u, _ in self.calls]), (0, [USER1]))

    def test_no_login_never_connects(self):
        self.write(GOOD)
        code, lines = self.run_check(login=False)
        self.assertEqual((code, self.connects), (0, []))
        self.assertIn("Не выполнялся", self.text(lines))

    def test_repeat_within_an_hour_is_blocked_until_again(self):
        self.write(GOOD)
        self.run_check(clock=lambda: 1000.0)
        self.calls.clear()
        code, lines = self.run_check(clock=lambda: 1000.0 + 600)
        self.assertEqual(self.calls, [])
        self.assertIn("ПРОПУЩЕНО", self.text(lines))
        self.run_check(clock=lambda: 1000.0 + 600, again=True)
        self.assertEqual(len(self.calls), 2)
        self.calls.clear()
        self.run_check(clock=lambda: 1000.0 + 7200)                            # через два часа защита сама снимается
        self.assertEqual(len(self.calls), 2)

    def test_ledger_is_written_before_the_attempt_and_holds_no_secrets(self):
        self.write(GOOD)
        self.run_check()
        raw = self.read(self.ledger)
        self.assertEqual(sorted(json.loads(raw)), ["1", "2"])
        for secret in (PASS1, PASS2, USER1, USER2):
            self.assertNotIn(secret, raw)

    def test_connection_failure_is_not_reported_as_a_password_problem(self):
        self.write(GOOD)

        def broken(host, port, timeout):
            self.connects.append(port)
            raise ssl.SSLError("EOF occurred in violation of protocol")

        code, lines = self.run_check(connect=broken)
        text = self.text(lines)
        self.assertEqual((code, self.connects), (3, [465]))                   # ящик 1 после сбоя связи не пробуется
        self.assertIn("пароль не проверялся", text)
        self.assertIn("check_cert.py", text)

    def test_port_587_uses_starttls_mode(self):
        self.write(GOOD + "MAILRU_PORT=587\n")
        code, lines = self.run_check()
        self.assertEqual(self.connects, [("smtp.mail.ru", 587)] * 2)
        self.assertIn("STARTTLS, порт 587", self.text(lines))

    # ---- секреты ----
    def test_report_and_output_never_contain_passwords_or_full_addresses(self):
        self.write(GOOD)
        self.accounts[USER1] = "x"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _, lines = self.run_check(quiet=False)
        saved = self.read(self.report)
        for blob in (out.getvalue(), saved, self.text(lines)):
            for secret in (PASS1, PASS2, USER1, USER2, "chief.test", "second.test"):
                self.assertNotIn(secret, blob)
        self.assertIn("c***@mail.ru", saved)

    def test_server_text_with_the_password_in_it_is_scrubbed(self):
        self.write(GOOD)

        class Echo(FakeSMTP):
            def auth(self, mechanism, method):
                raise smtplib.SMTPAuthenticationError(535, f"bad {self.password} for {self.user}".encode())

        code, lines = self.run_check(connect=lambda h, p, t: Echo({}, self.calls))
        self.assertNotIn(PASS2, self.text(lines))
        self.assertNotIn(USER2, self.text(lines))

    # ---- формат local.env ----
    def problems_for(self, text):
        self.write(text)
        info = check_login.scan_env(self.env, check_login.watched_keys())
        found = []
        for number in (1, 2):
            found += check_login.env_findings(info, number, lambda line: None)
        return found

    def test_duplicate_key_commented_key_export_prefix_and_lowercase_are_found(self):
        found = "\n".join(self.problems_for(
            f"MAILRU_USER={USER1}\nMAILRU_APP_PASSWORD=старый0000000000\nMAILRU_APP_PASSWORD={PASS1}\n"
            f"export MAILRU_USER_2={USER2}\nmailru_app_password_2={PASS2}\n# MAILRU_APP_PASSWORD_2={PASS2}\n"))
        self.assertIn("MAILRU_APP_PASSWORD указан 2 раза", found)
        self.assertIn("префикс export", found)
        self.assertIn("регистр букв", found)
        self.assertIn("MAILRU_APP_PASSWORD_2 есть только закомментированным", found)

    def test_password_defects_are_named_without_showing_the_value(self):
        for value, expected in ((f"{PASS1} ", None), (PASS1 + " # новый", "комментарий"), ("ab cd" + PASS1, "пробелы"),
                                ("'" + PASS1, "кавычки"), (PASS1[:-1] + "у", "не-ASCII"), ("", "пустой")):
            found = "\n".join(self.problems_for(f"MAILRU_USER={USER1}\nMAILRU_APP_PASSWORD={value}\n"
                                                f"MAILRU_USER_2={USER2}\nMAILRU_APP_PASSWORD_2={PASS2}\n"))
            if expected is None:
                self.assertEqual(found, "")                                    # пробел по краям программа обрежет
            else:
                self.assertIn(expected, found, value)
            self.assertNotIn(PASS1, found)

    def test_wrong_address_forms_are_found(self):
        found = "\n".join(self.problems_for(f"MAILRU_USER=chief.test@gmail.example\nMAILRU_APP_PASSWORD={PASS1}\n"))
        self.assertIn("не из семейства mail.ru", found)
        found = "\n".join(self.problems_for(f"MAILRU_USER=chief.test\nMAILRU_APP_PASSWORD={PASS1}\n"))
        self.assertIn("не похож на полный адрес", found)

    def test_paired_quotes_are_stripped_by_the_program_and_not_reported(self):
        found = self.problems_for(f"MAILRU_USER=\"{USER1}\"\nMAILRU_APP_PASSWORD=\"{PASS1}\"\n"
                                  f"MAILRU_USER_2={USER2}\nMAILRU_APP_PASSWORD_2={PASS2}\n")
        self.assertEqual(found, [])

    def test_clean_file_has_no_findings(self):
        self.assertEqual(self.problems_for(GOOD), [])

    # ---- перекрытия ----
    def test_environment_variable_that_differs_from_the_file_is_reported_by_name_only(self):
        self.write(GOOD)
        os.environ["MAILRU_APP_PASSWORD"] = "old-" + PASS1
        code, lines = self.run_check(login=False)
        text = self.text(lines)
        self.assertIn("MAILRU_APP_PASSWORD задана в окружении и ОТЛИЧАЕТСЯ от local.env", text)
        self.assertNotIn(PASS1, text)
        self.assertNotIn("old-", text)

    def test_same_value_in_environment_is_not_a_conflict(self):
        self.write(GOOD)
        os.environ["MAILRU_APP_PASSWORD"] = PASS1
        code, lines = self.run_check(login=False)
        self.assertNotIn("ОТЛИЧАЕТСЯ", self.text(lines))

    def test_empty_environment_variable_is_reported_because_the_site_would_not_take_the_file(self):
        found = check_login.override_findings({"MAILRU_APP_PASSWORD": PASS1}, self.dir.name, 0.0, lambda line: None,
                                              scan_files=False, processes=lambda: [])
        self.assertEqual(found, [])
        os.environ["MAILRU_APP_PASSWORD"] = ""
        found = check_login.override_findings({"MAILRU_APP_PASSWORD": PASS1}, self.dir.name, 0.0, lambda line: None,
                                              scan_files=False, processes=lambda: [])
        self.assertTrue(any("пуста в окружении" in item for item in found))

    def test_process_started_before_the_last_save_of_local_env_is_reported(self):
        rows = [(1111, 100.0, "python run_local.py"), (2222, 900.0, "python run_local.py"), (3333, 100.0, "python other.py"),
                (os.getpid(), 100.0, "python mailer.py")]
        found = check_login.override_findings({}, self.dir.name, 500.0, lambda line: None, scan_files=False,
                                              processes=lambda: rows)
        self.assertEqual(len(found), 1)
        self.assertIn("процесс 1111 (run_local)", found[0])

    def test_pycharm_run_configuration_with_a_different_value_is_reported(self):
        os.makedirs(os.path.join(self.dir.name, ".idea"))
        with open(os.path.join(self.dir.name, ".idea", "workspace.xml"), "w", encoding="utf-8") as fh:
            fh.write('<envs><env name="MAILRU_APP_PASSWORD" value="stale-value-0000" /></envs>')
        parent = os.path.join(self.dir.name, "p")
        project = os.path.join(parent, "proj")
        os.makedirs(os.path.join(project, ".idea"))
        os.replace(os.path.join(self.dir.name, ".idea", "workspace.xml"), os.path.join(project, ".idea", "workspace.xml"))
        found = check_login.override_findings({"MAILRU_APP_PASSWORD": PASS1}, project, 0.0, lambda line: None,
                                              processes=lambda: [])
        self.assertTrue(any("настройках запуска PyCharm" in item for item in found), found)
        self.assertFalse(any("stale-value" in item for item in found))

    def test_another_copy_of_the_program_next_door_is_reported(self):
        parent = os.path.join(self.dir.name, "codes")
        project, other = os.path.join(parent, "coldoutreach"), os.path.join(parent, "coldoutreach-old")
        os.makedirs(project)
        os.makedirs(other)
        with open(os.path.join(other, "mailer.py"), "w"):
            pass
        found = check_login.override_findings({}, project, 0.0, lambda line: None, processes=lambda: [])
        self.assertTrue(any("другие копии программы (coldoutreach-old)" in item for item in found), found)

    def test_missing_env_file_is_reported_not_a_crash(self):
        code, lines = self.run_check(login=False)
        self.assertEqual(code, 0)
        self.assertIn("ФАЙЛА НЕТ", self.text(lines))

    def test_utf16_file_is_reported(self):
        with open(self.env, "wb") as fh:
            fh.write(GOOD.encode("utf-16"))
        code, lines = self.run_check()
        self.assertIn("сохранён не в UTF-8", self.text(lines))
        self.assertEqual(self.connects, [])                                    # не читается - входов нет, как у mailer.py

    def test_findings_are_repeated_in_the_summary(self):
        self.write(GOOD + f"MAILRU_APP_PASSWORD={PASS1} # новый\n")
        code, lines = self.run_check(login=False)
        text = self.text(lines)
        self.assertGreater(text.index("  ! MAILRU_APP_PASSWORD указан 2 раза"), text.index("== ИТОГ =="))
        self.assertIn("  ! MAILRU_APP_PASSWORD указан 2 раза", text)
        self.assertIn("Найдено замечаний к настройке", text)


if __name__ == "__main__":
    unittest.main()
