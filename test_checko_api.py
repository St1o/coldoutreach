import unittest

import requests

from checko_api import (
    CheckoApi, CheckoApiError, CheckoApiNotFound, CheckoApiQuotaExceeded, parse_company,
)

KEY = "SECRET-KEY-123"


def payload(**data):
    base = {"НаимСокр": "ООО ФИРМА", "НаимПолн": "ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ ФИРМА",
            "Руковод": [{"ФИО": "Иванов Иван Иванович", "НаимДолжн": "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР"}]}
    base.update(data)
    return {"data": base, "meta": {"status": "ok"}}


class FakeResponse:
    def __init__(self, body=None, status=200):
        self._body, self.status_code = body, status

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if self.error:
            raise self.error
        return self.response


class ParseCompanyTest(unittest.TestCase):
    def test_name_director_and_email_from_the_register(self):
        body = payload(Контакты={"Емэйл": ["other@site.ru"]})
        body["source_data"] = {"СвЮЛ": {"СвАдрЭлПочты": {"@attributes": {"E-mail": "REG@Example.RU"}}}}
        info = parse_company(body)
        self.assertEqual((info.name, info.director), ("ООО ФИРМА", "Иванов Иван Иванович"))
        self.assertEqual((info.email, info.email_from_register), ("reg@example.ru", True))   # ЕГРЮЛ важнее контактов

    def test_register_email_as_plain_attribute(self):
        body = payload()
        body["source_data"] = {"СвАдрЭлПочты": {"E-mail": "a@b.ru", "ГРНДата": {"ГРН": "123"}}}
        self.assertEqual(parse_company(body).email, "a@b.ru")

    def test_falls_back_to_contacts_and_says_so(self):
        info = parse_company(payload(Контакты={"Тел": ["+7 1"], "Емэйл": ["Sales@Example.ru", "x@y.ru"]}))
        self.assertEqual((info.email, info.email_from_register), ("sales@example.ru", False))

    def test_no_email_anywhere(self):
        body = payload(Контакты={"Тел": ["+7 1"]})
        body["source_data"] = {"СвЮЛ": {"Наим": "ФИРМА"}}
        info = parse_company(body)
        self.assertEqual((info.email, info.email_from_register), ("", False))

    def test_tax_office_address_is_not_a_company_email(self):
        body = payload()
        body["source_data"] = {"СвАдрЭлПочты": {"E-mail": "inspector@nalog.ru"}}
        self.assertEqual(parse_company(body).email, "")

    def test_uppercase_director_is_normalised_and_first_with_name_wins(self):
        body = payload(Руковод=[{"ФИО": ""}, {"ФИО": "ПЕТРОВ ПЁТР ПЕТРОВИЧ"}, {"ФИО": "Сидоров Сидор"}])
        self.assertEqual(parse_company(body).director, "Петров Пётр Петрович")

    def test_management_company_has_no_director_name(self):
        info = parse_company(payload(Руковод=[], УпрОрг={"НаимПолн": "УК ООО"}))
        self.assertEqual(info.director, "")

    def test_main_okved_from_checko_field(self):
        info = parse_company(payload(ОКВЭД={"Код": "62.01", "Наим": "Разработка ПО", "Версия": "2014"}))
        self.assertEqual((info.okved, info.okved_name), ("62.01", "Разработка ПО"))

    def test_main_okved_from_register_data_when_checko_field_is_empty(self):
        body = payload(ОКВЭД={})
        body["source_data"] = {"СвЮЛ": {"СвОКВЭД": {"СвОКВЭДОсн": {"@attributes": {
            "КодОКВЭД": "47.19", "НаимОКВЭД": "Торговля розничная"}}}}}
        info = parse_company(body)
        self.assertEqual((info.okved, info.okved_name), ("47.19", "Торговля розничная"))

    def test_main_okved_as_one_string(self):
        info = parse_company(payload(ОКВЭД="62.01 Разработка ПО"))
        self.assertEqual((info.okved, info.okved_name), ("62.01", "Разработка ПО"))

    def test_no_okved_is_empty_not_an_error(self):
        info = parse_company(payload())
        self.assertEqual((info.okved, info.okved_name), ("", ""))

    def test_falls_back_to_full_name(self):
        self.assertTrue(parse_company(payload(НаимСокр="")).name.startswith("ОБЩЕСТВО"))


class CheckoApiTest(unittest.TestCase):
    def api(self, response=None, error=None):
        session = FakeSession(response, error)
        return CheckoApi(KEY, session), session

    def test_request_asks_for_the_register_source(self):
        api, session = self.api(FakeResponse(payload()))
        api.company("7707083893")
        url, params = session.calls[0]
        self.assertEqual(url, "https://api.checko.ru/v2/company")
        self.assertEqual(params, {"key": KEY, "inn": "7707083893", "source": "true"})

    def test_ok(self):
        api, _ = self.api(FakeResponse(payload()))
        self.assertEqual(api.company("1")["data"]["НаимСокр"], "ООО ФИРМА")

    def test_not_found_variants(self):
        for response in (FakeResponse({"meta": {"status": "error", "message": "Организация не найдена"}}),
                         FakeResponse({"meta": {"status": "error", "message": "x"}}, 404),
                         FakeResponse({"data": {}, "meta": {"status": "ok"}})):
            api, _ = self.api(response)
            with self.assertRaises(CheckoApiNotFound):
                api.company("1")

    def test_quota_variants_stop_the_run(self):
        for response in (FakeResponse({"meta": {"status": "error", "message": "Превышен лимит запросов"}}),
                         FakeResponse({"meta": {"status": "error", "message": "Недостаточно средств на балансе"}}),
                         FakeResponse({}, 402), FakeResponse({}, 429)):
            api, _ = self.api(response)
            with self.assertRaises(CheckoApiQuotaExceeded):
                api.company("1")

    def test_other_errors(self):
        for response in (FakeResponse({"meta": {"status": "error", "message": "Неверный ключ"}}),
                         FakeResponse({"meta": {"message": "сбой"}}, 500), FakeResponse(None, 200)):
            api, _ = self.api(response)
            with self.assertRaises(CheckoApiError):
                api.company("1")

    def test_the_key_never_appears_in_error_messages(self):
        error = requests.ConnectionError(f"Max retries for url: /v2/company?key={KEY}&inn=1")
        api, _ = self.api(error=error)
        with self.assertRaises(CheckoApiError) as ctx:
            api.company("1")
        self.assertNotIn(KEY, str(ctx.exception))
        self.assertIn("***", str(ctx.exception))
        api, _ = self.api(FakeResponse({"meta": {"status": "error", "message": f"ключ {KEY} недействителен"}}))
        with self.assertRaises(CheckoApiError) as ctx:
            api.company("1")
        self.assertNotIn(KEY, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
