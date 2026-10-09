import csv
import json
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

import leads
from checko_api import CheckoApiError, CheckoApiNotFound, CheckoApiQuotaExceeded
from checko_updates import CheckoBlocked, CheckoError, NewCompany
from egrul_inn_search import EgrulCaptchaRequired, EgrulError, EgrulRecord
from egrul_pdf import ExtractError

DAY = date(2026, 10, 9)
A, B, C = "7707083004", "7707083011", "7707083029"      # с почтой / без почты / ещё нет в ЕГРЮЛ

WITH_EMAIL = ("Адрес электронной почты\n10 E-mail SALES@EXAMPLE.RU\n11 ГРН\n"
              "Сведения о лице, имеющем право без доверенности действовать от имени юридического лица\n"
              "26 Фамилия\nИмя\nОтчество\nИВАНОВ\nИВАН\nИВАНОВИЧ\n27 ИНН 1\n")
NO_EMAIL = WITH_EMAIL.split("11 ГРН\n")[1]
NO_DIRECTOR = "Адрес электронной почты\n10 E-mail SALES@EXAMPLE.RU\n11 ГРН\n"
PDFS = {b"with-email": WITH_EMAIL, b"no-email": NO_EMAIL, b"no-director": NO_DIRECTOR}


class FakeChecko:
    def __init__(self, companies=None, error=None):
        self.companies, self.error = companies or [], error

    def new_companies(self, target, max_pages=25):
        if self.error:
            raise self.error
        return self.companies


class FakeEgrul:
    """Подставной клиент ЕГРЮЛ: по ИНН отдаёт запись и «PDF» (маркер из PDFS)."""

    def __init__(self, table, errors=None):
        self.table = table                  # ИНН -> (маркер PDF, director из поиска) либо None (нет в ЕГРЮЛ)
        self.errors = errors or {}
        self.searched = []

    def search_by_inn(self, inn):
        self.searched.append(inn)
        if inn in self.errors:
            raise self.errors[inn]
        if self.table.get(inn) is None:
            return []
        marker, director = self.table[inn]
        return [EgrulRecord(inn=inn, name=f"ООО ФИРМА {inn}", short_name=f"ФИРМА {inn}",
                            director=director, token=marker.decode())]

    def download_extract(self, record):
        return record.token.encode()


class FlakyEgrul(FakeEgrul):
    """Первые `captcha_times` обращений отвечает капчей, дальше работает как обычно."""

    def __init__(self, table, captcha_times):
        super().__init__(table)
        self.captcha_left = captcha_times

    def search_by_inn(self, inn):
        if self.captcha_left > 0:
            self.captcha_left -= 1
            raise EgrulCaptchaRequired("капча")
        return super().search_by_inn(inn)


def companies(*inns):
    return [NewCompany(inn=i, ogrn="1" * 13, reg_date=DAY, name=f"checko {i}") for i in inns]


class LeadsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "out", "leads.csv")
        self.logs = []
        patcher = mock.patch("leads.pdf_to_text", side_effect=lambda pdf: PDFS[pdf])
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_leads(self, egrul, checko=None, **kwargs):
        checko = checko or FakeChecko(companies(A, B, C))
        return leads.run(DAY, self.out, pause=0, checko=checko, egrul=egrul, log=self.logs.append, **kwargs)

    def read_csv(self):
        with open(self.out, encoding="utf-8-sig", newline="") as fh:
            return list(csv.reader(fh, delimiter=";"))

    def state(self):
        with open(leads.state_path(self.out), encoding="utf-8") as fh:
            return json.load(fh)

    def standard_egrul(self):
        return FakeEgrul({A: (b"with-email", ""), B: (b"no-email", ""), C: None})



class LeadsTest(LeadsCase):
    # ---- результат ----

    def test_only_companies_with_email_are_in_the_table(self):
        self.assertEqual(self.run_leads(self.standard_egrul()), 0)
        self.assertEqual(self.read_csv(), [
            ["Название", "ФИО директора", "Почта"],
            [f"ФИРМА {A}", "Иванов Иван Иванович", "sales@example.ru"],
        ])
        statuses = {inn: e["status"] for inn, e in self.state().items()}
        self.assertEqual(statuses, {A: leads.OK, B: leads.NO_EMAIL, C: leads.RETRY})

    def test_csv_is_excel_friendly(self):
        self.run_leads(self.standard_egrul())
        with open(self.out, "rb") as fh:
            raw = fh.read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))      # BOM
        self.assertIn("Название;ФИО директора;Почта".encode(), raw)

    def test_director_falls_back_to_search_result(self):
        egrul = FakeEgrul({A: (b"no-director", "Генеральный директор: Петров Пётр Петрович")})
        self.run_leads(egrul, FakeChecko(companies(A)))
        self.assertEqual(self.read_csv()[1][1], "Петров Пётр Петрович")

    def test_company_with_email_is_kept_even_without_director(self):
        self.run_leads(FakeEgrul({A: (b"no-director", "")}), FakeChecko(companies(A)))
        self.assertEqual(self.read_csv()[1], [f"ФИРМА {A}", "", "sales@example.ru"])

    def test_log_has_no_personal_data(self):
        self.run_leads(self.standard_egrul())
        text = "\n".join(self.logs)
        for secret in ("Иванов", "sales@example.ru", "ФИРМА"):
            self.assertNotIn(secret, text)
        self.assertIn(A, text)

    # ---- повторный запуск ----

    def test_rerun_checks_only_what_needs_it(self):
        self.run_leads(self.standard_egrul())
        second = self.standard_egrul()
        self.run_leads(second)
        self.assertEqual(second.searched, [C])            # A и B повторно не проверяются

    def test_retry_company_appears_when_registry_catches_up(self):
        self.run_leads(self.standard_egrul())
        later = FakeEgrul({A: (b"with-email", ""), B: (b"no-email", ""), C: (b"with-email", "")})
        self.run_leads(later)
        self.assertEqual([row[0] for row in self.read_csv()[1:]], [f"ФИРМА {A}", f"ФИРМА {C}"])

    def test_error_is_retried_next_time(self):
        egrul = FakeEgrul({A: (b"with-email", "")}, errors={A: EgrulError("таймаут")})
        self.run_leads(egrul, FakeChecko(companies(A)))
        self.assertEqual(self.state()[A]["status"], leads.RETRY)
        self.assertIn("таймаут", self.state()[A]["note"])
        self.assertEqual(self.read_csv(), [["Название", "ФИО директора", "Почта"]])
        fixed = FakeEgrul({A: (b"with-email", "")})
        self.run_leads(fixed, FakeChecko(companies(A)))
        self.assertEqual(len(self.read_csv()), 2)

    def test_unreadable_pdf_is_retry_not_no_email(self):
        with mock.patch("leads.pdf_to_text", side_effect=ExtractError("битый")):
            self.run_leads(FakeEgrul({A: (b"with-email", "")}), FakeChecko(companies(A)))
        self.assertEqual(self.state()[A]["status"], leads.RETRY)

    # ---- остановки ----

    def test_captcha_stops_and_keeps_what_is_done(self):
        egrul = FakeEgrul({A: (b"with-email", ""), B: (b"with-email", ""), C: (b"with-email", "")},
                          errors={B: EgrulCaptchaRequired("капча")})
        self.assertEqual(self.run_leads(egrul), 2)
        self.assertEqual(egrul.searched, [A, B])           # на C уже не пошли
        self.assertEqual(len(self.read_csv()), 2)           # A сохранён
        self.assertIn(A, self.state())
        self.assertNotIn(B, self.state())

    # ---- ожидание при капче и остановка ----

    def test_waits_after_captcha_and_continues(self):
        egrul = FlakyEgrul({A: (b"with-email", "")}, captcha_times=2)
        sleeps = []
        code = self.run_leads(egrul, FakeChecko(companies(A)), captcha_wait=600, sleep=sleeps.append)
        self.assertEqual(code, 0)
        self.assertEqual(sum(sleeps), 1200)                       # дважды по 10 минут
        self.assertEqual(self.state()[A]["status"], leads.OK)
        text = "\n".join(self.logs)
        self.assertIn("Ждём 10 мин", text)
        self.assertIn("(2/6)", text)

    def test_gives_up_after_too_many_captchas(self):
        egrul = FlakyEgrul({A: (b"with-email", "")}, captcha_times=99)
        code = self.run_leads(egrul, FakeChecko(companies(A)), captcha_wait=60, captcha_retries=2,
                              sleep=lambda s: None)
        self.assertEqual(code, 2)
        self.assertNotIn(A, self.state())
        self.assertIn("(2/2)", "\n".join(self.logs))

    def test_captcha_without_wait_stops_at_once(self):
        egrul = FlakyEgrul({A: (b"with-email", "")}, captcha_times=1)
        self.assertEqual(self.run_leads(egrul, FakeChecko(companies(A))), 2)

    def test_stop_during_captcha_wait(self):
        egrul = FlakyEgrul({A: (b"with-email", "")}, captcha_times=99)
        sleeps = []
        code = self.run_leads(egrul, FakeChecko(companies(A)), captcha_wait=600, sleep=sleeps.append,
                              should_stop=lambda: len(sleeps) >= 3)
        self.assertEqual(code, 4)
        self.assertEqual(len(sleeps), 3)                          # не досидели 10 минут
        self.assertIn("Остановлено по запросу", "\n".join(self.logs))

    def test_stop_between_companies_keeps_finished_ones(self):
        egrul = FakeEgrul({A: (b"with-email", ""), B: (b"with-email", ""), C: (b"with-email", "")})
        code = self.run_leads(egrul, should_stop=lambda: len(egrul.searched) >= 1)
        self.assertEqual(code, 4)
        self.assertEqual(egrul.searched, [A])
        self.assertEqual(len(self.read_csv()), 2)                 # заголовок и A

    def test_stops_after_a_series_of_errors(self):
        table = {A: (b"with-email", ""), B: (b"with-email", ""), C: (b"with-email", "")}
        errors = {i: EgrulError("400 Client Error") for i in table}
        egrul = FakeEgrul(table, errors=errors)
        code = self.run_leads(egrul, FakeChecko(companies(A, B, C)), max_errors_in_a_row=2)
        self.assertEqual(code, 5)
        self.assertEqual(egrul.searched, [A, B])                  # на третью уже не пошли
        self.assertIn("2 ошибок подряд", "\n".join(self.logs))
        self.assertEqual(self.state()[A]["status"], leads.RETRY)  # ошибки проверятся при повторе

    def test_one_error_between_good_ones_does_not_stop(self):
        egrul = FakeEgrul({A: (b"with-email", ""), B: (b"with-email", ""), C: (b"with-email", "")},
                          errors={B: EgrulError("таймаут")})
        self.assertEqual(self.run_leads(egrul, max_errors_in_a_row=2), 0)
        self.assertEqual(egrul.searched, [A, B, C])

    def test_limit(self):
        egrul = self.standard_egrul()
        self.run_leads(egrul, limit=2)
        self.assertEqual(egrul.searched, [A, B])

    def test_checko_blocked_and_error(self):
        for error in (CheckoBlocked("403"), CheckoError("вёрстка")):
            self.assertEqual(self.run_leads(self.standard_egrul(), FakeChecko(error=error)), 3)

    def test_nothing_found_still_writes_an_empty_table(self):
        self.assertEqual(self.run_leads(self.standard_egrul(), FakeChecko([])), 0)
        self.assertEqual(self.read_csv(), [["Название", "ФИО директора", "Почта"]])


class FakeApi:
    """Подставной API Checko: по ИНН отдаёт готовый ответ или бросает ошибку."""

    def __init__(self, table):
        self.table = table
        self.asked = []

    def company(self, inn):
        self.asked.append(inn)
        result = self.table[inn]
        if isinstance(result, Exception):
            raise result
        return result


OKVED = {"Код": "62.01", "Наим": "Разработка компьютерного программного обеспечения", "Версия": "2014"}


def leads_company(inn, name):
    return NewCompany(inn=inn, ogrn="1" * 13, reg_date=DAY, name=name)


def api_payload(email="", register=True, fio="Иванов Иван Иванович", okved=OKVED):
    body = {"data": {"НаимСокр": "ООО ФИРМА", "Руковод": [{"ФИО": fio}]}, "meta": {"status": "ok"}}
    if okved:
        body["data"]["ОКВЭД"] = okved
    if email and register:
        body["source_data"] = {"СвАдрЭлПочты": {"E-mail": email}}
    elif email:
        body["data"]["Контакты"] = {"Емэйл": [email]}
    return body


class ApiSourceTest(LeadsCase):
    """Тот же конвейер, но данные идут из API Checko, а не из выписок ФНС."""

    def run_api(self, api, checko=None, **kwargs):
        return self.run_leads(FakeEgrul({}), checko, process=lambda c: leads.process_company_api(api, c), **kwargs)

    def test_statuses_and_table(self):
        api = FakeApi({A: api_payload("sales@example.ru"), B: api_payload(""), C: CheckoApiNotFound("нет")})
        self.assertEqual(self.run_api(api), 0)
        self.assertEqual(self.read_csv(), [["Название", "ФИО директора", "Почта", "Основной ОКВЭД"],
                                           [f"checko {A}", "Иванов Иван Иванович", "sales@example.ru",
                                            "Разработка компьютерного программного обеспечения"]])
        statuses = {inn: e["status"] for inn, e in self.state().items()}
        self.assertEqual(statuses, {A: leads.OK, B: leads.NO_EMAIL, C: leads.RETRY})
        self.assertIn("нет в Checko", self.state()[C]["note"])

    def test_name_comes_from_the_checko_list_and_the_api_name_is_only_a_fallback(self):
        listed = [leads_company(A, "ООО ИЗ СПИСКА"), leads_company(B, "")]
        self.run_api(FakeApi({A: api_payload("a@example.ru"), B: api_payload("b@example.ru")}), FakeChecko(listed))
        self.assertEqual([row[0] for row in self.read_csv()[1:]], ["ООО ИЗ СПИСКА", "ООО ФИРМА"])

    def test_contact_email_is_marked(self):
        self.run_api(FakeApi({A: api_payload("c@example.ru", register=False)}), FakeChecko(companies(A)))
        self.assertEqual(self.state()[A]["status"], leads.OK)
        self.assertIn("не из ЕГРЮЛ", self.state()[A]["note"])

    def test_missing_okved_is_noted_and_left_empty(self):
        self.run_api(FakeApi({A: api_payload("a@example.ru", okved=None)}), FakeChecko(companies(A)))
        self.assertEqual(self.read_csv()[1][3], "")
        self.assertIn("ОКВЭД не найден", self.state()[A]["note"])

    def test_rerun_asks_only_for_what_is_pending(self):
        first = FakeApi({A: api_payload("a@example.ru"), B: api_payload(""), C: CheckoApiNotFound("нет")})
        self.run_api(first)
        second = FakeApi({C: api_payload("c@example.ru")})
        self.run_api(second)
        self.assertEqual(second.asked, [C])
        self.assertEqual(len(self.read_csv()), 3)

    def test_exhausted_quota_stops_the_run_and_keeps_results(self):
        api = FakeApi({A: api_payload("a@example.ru"), B: CheckoApiQuotaExceeded("лимит"), C: api_payload("c@example.ru")})
        self.assertEqual(self.run_api(api), 6)
        self.assertEqual(api.asked, [A, B])                      # на третью уже не пошли
        self.assertEqual(len(self.read_csv()), 2)
        self.assertIn("Лимит API Checko исчерпан", "\n".join(self.logs))

    def test_errors_in_a_row_stop_the_run(self):
        api = FakeApi({i: CheckoApiError("HTTP 500") for i in (A, B, C)})
        self.assertEqual(self.run_api(api, max_errors_in_a_row=2), 5)
        self.assertEqual(api.asked, [A, B])

    def test_log_has_no_personal_data(self):
        self.run_api(FakeApi({A: api_payload("secret@example.ru", fio="Тайный Человек"), B: api_payload(""), C: api_payload("")}))
        text = "\n".join(self.logs)
        for secret in ("secret@example.ru", "Тайный", "ФИРМА"):
            self.assertNotIn(secret, text)



class HelpersTest(unittest.TestCase):
    def test_director_from_search(self):
        self.assertEqual(leads.director_from_search("Директор: Иванов Иван"), "Иванов Иван")
        self.assertEqual(leads.director_from_search("Иванов Иван"), "Иванов Иван")
        self.assertEqual(leads.director_from_search(""), "")

    def test_state_path(self):
        self.assertEqual(leads.state_path("a/leads_2026-10-09.csv"), "a/leads_2026-10-09.state.json")

    def test_today_moscow_is_a_date(self):
        self.assertIsInstance(leads.today_moscow(), date)


if __name__ == "__main__":
    unittest.main()
