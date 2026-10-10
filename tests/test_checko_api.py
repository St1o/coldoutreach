import json
import subprocess
import unittest
from unittest import mock

import requests

import checko_api

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


class ConnectionProblemsTest(unittest.TestCase):
    def explain(self, error):
        api = CheckoApi(KEY, FakeSession(error=error))
        with self.assertRaises(CheckoApiError) as ctx:
            api.company("1")
        return str(ctx.exception)

    def test_each_kind_of_failure_is_named_in_plain_words(self):
        ssl_error = requests.exceptions.SSLError(
            "HTTPSConnectionPool(host='api.checko.ru', port=443): Max retries exceeded with url: /v2/company?key="
            + KEY + " (Caused by SSLError(SSLEOFError(8, 'EOF occurred in violation of protocol')))")
        text = self.explain(ssl_error)
        self.assertIn("защищённое соединение", text)
        self.assertIn("SSLEOFError", text)                      # технический хвост остаётся
        self.assertNotIn(KEY, text)
        self.assertIn("прокси", self.explain(requests.exceptions.ProxyError("Tunnel connection failed: 403")))
        self.assertIn("не ответил", self.explain(requests.exceptions.ConnectTimeout("timed out")))
        self.assertIn("нет соединения", self.explain(requests.exceptions.ConnectionError("getaddrinfo failed")))
        self.assertIn("сбой запроса", self.explain(requests.exceptions.RequestException("что-то")))

    def test_messages_stay_short_enough_for_the_log(self):
        self.assertLessEqual(len(self.explain(requests.exceptions.SSLError("x" * 1000))), 200)


class TransportFallbackTest(unittest.TestCase):
    """Обрыв защищённого соединения: пробуем без прокси и с TLS 1.2, найденный путь запоминаем."""

    SSL = requests.exceptions.SSLError("Caused by SSLError(SSLEOFError(8, 'EOF occurred in violation of protocol'))")

    def sessions(self, **by_mode):
        made = {mode: by_mode.get(mode, FakeSession(error=self.SSL)) for mode in ("no-proxy", "tls12", "curl")}
        patcher = mock.patch("checko_api.new_session", side_effect=lambda mode="default": made[mode] if mode in made else FakeSession(error=self.SSL))
        patcher.start()
        self.addCleanup(patcher.stop)
        return made

    def test_a_working_fallback_is_used_and_remembered(self):
        made = self.sessions(tls12=FakeSession(FakeResponse(payload())))
        api = CheckoApi(KEY)                                    # свой сеанс: запасные пути разрешены
        self.assertEqual(api.company("1")["data"]["НаимСокр"], "ООО ФИРМА")
        self.assertEqual(api.mode, "tls12")
        self.assertEqual(len(made["no-proxy"].calls), 1)        # без прокси пробовали первым и не вышло
        api.company("2")
        self.assertEqual(len(made["tls12"].calls), 2)           # дальше идём найденным путём
        self.assertEqual(len(made["no-proxy"].calls), 1)

    def test_no_proxy_path_comes_first(self):
        made = self.sessions(**{"no-proxy": FakeSession(FakeResponse(payload()))})
        api = CheckoApi(KEY)
        api.company("1")
        self.assertEqual(api.mode, "no-proxy")
        self.assertEqual(made["tls12"].calls, [])

    def test_curl_is_the_last_resort_and_is_remembered(self):
        made = self.sessions(curl=FakeSession(FakeResponse(payload())))
        api = CheckoApi(KEY)
        self.assertEqual(api.company("1")["data"]["НаимСокр"], "ООО ФИРМА")
        self.assertEqual(api.mode, "curl")
        self.assertEqual((len(made["no-proxy"].calls), len(made["tls12"].calls)), (1, 1))     # сначала пробовали остальные
        api.company("2")
        self.assertEqual(len(made["curl"].calls), 2)

    def test_a_computer_without_curl_just_skips_that_way(self):
        made = self.sessions(tls12=FakeSession(FakeResponse(payload())))
        made["curl"] = None
        self.assertEqual(CheckoApi(KEY).company("1")["data"]["НаимСокр"], "ООО ФИРМА")

    def test_any_dropped_connection_triggers_the_fallbacks_but_a_timeout_does_not(self):
        made = self.sessions(tls12=FakeSession(FakeResponse(payload())))
        api = CheckoApi(KEY)
        api.session = FakeSession(error=requests.exceptions.ConnectionError("Connection aborted: ConnectionResetError"))
        self.assertTrue(api.company("1"))
        self.assertEqual(api.mode, "tls12")
        slow = CheckoApi(KEY)
        slow.session = FakeSession(error=requests.exceptions.ConnectTimeout("timed out"))
        calls_before = len(made["tls12"].calls)
        with self.assertRaises(CheckoApiError):
            slow.company("1")
        self.assertEqual(len(made["tls12"].calls), calls_before)

    def test_when_nothing_helps_the_first_error_is_reported(self):
        self.sessions()
        api = CheckoApi(KEY)
        api.session = FakeSession(error=self.SSL)
        with self.assertRaises(CheckoApiError) as ctx:
            api.company("1")
        self.assertIn("защищённое соединение", str(ctx.exception))
        self.assertEqual(api.mode, "default")

    def test_no_fallback_for_a_given_session_or_for_other_errors(self):
        made = self.sessions(tls12=FakeSession(FakeResponse(payload())))
        given = FakeSession(error=self.SSL)
        with self.assertRaises(CheckoApiError):
            CheckoApi(KEY, given).company("1")                  # сеанс передан снаружи (тесты, свои настройки): не трогаем
        self.assertEqual(made["tls12"].calls, [])
        api = CheckoApi(KEY)
        api.session = FakeSession(error=requests.exceptions.ConnectTimeout("timed out"))
        with self.assertRaises(CheckoApiError):
            api.company("1")                                    # таймаут - не повод менять способ соединения
        self.assertEqual((made["no-proxy"].calls, made["tls12"].calls, made["curl"].calls), ([], [], []))

    def test_real_sessions_are_built_for_each_mode(self):
        import checko_api
        self.assertFalse(checko_api.new_session("no-proxy").trust_env)
        adapter = checko_api.new_session("tls12").get_adapter("https://api.checko.ru/")
        self.assertIsInstance(adapter, checko_api._Tls12Adapter)
        self.assertTrue(checko_api.new_session().trust_env)


class CurlSessionTest(unittest.TestCase):
    """Запрос через curl: команда, ввод и разбор ответа (curl подменён); настоящий curl - в тесте ниже."""

    def run_with(self, stdout=b'{"a": 1}\n200', stderr=b"", code=0, error=None):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if error:
                raise error
            return subprocess.CompletedProcess(command, code, stdout, stderr)

        with mock.patch("checko_api.subprocess.run", fake_run):
            try:
                response = checko_api._CurlSession("/usr/bin/curl").get(
                    "https://api.checko.ru/v2/company", {"key": KEY, "inn": "1"}, timeout=20)
            except requests.exceptions.ConnectionError as exc:
                return calls, exc
        return calls, response

    def test_the_key_goes_through_stdin_not_the_command_line(self):
        calls, response = self.run_with()
        command, kwargs = calls[0]
        self.assertNotIn(KEY, " ".join(command))
        self.assertIn(KEY, kwargs["input"].decode())
        self.assertIn("--config", command)
        self.assertEqual((response.status_code, response.json()), (200, {"a": 1}))

    def test_http_errors_are_answers_not_failures(self):
        _, response = self.run_with(stdout=b'{"meta": {"message": "x"}}\n403')
        self.assertEqual(response.status_code, 403)

    def test_a_failed_curl_is_a_connection_error_with_its_reason(self):
        _, error = self.run_with(stdout=b"", stderr=b"curl: (35) schannel: failed to receive handshake", code=35)
        self.assertIsInstance(error, requests.exceptions.ConnectionError)
        self.assertIn("schannel", str(error))
        _, error = self.run_with(error=OSError("не найден"))
        self.assertIn("curl не запустился", str(error))
        _, error = self.run_with(stdout=b"garbage")
        self.assertIn("не в том виде", str(error))

    def test_a_missing_curl_means_no_such_way(self):
        with mock.patch("checko_api.find_curl", return_value=None):
            self.assertIsNone(checko_api.new_session("curl"))
        with mock.patch("checko_api.find_curl", return_value="/x/curl"):
            self.assertIsInstance(checko_api.new_session("curl"), checko_api._CurlSession)

    @unittest.skipUnless(checko_api.find_curl(), "curl на этом компьютере нет")
    def test_the_real_curl_against_a_local_server(self):
        import http.server
        import threading
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                body = json.dumps(payload()).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/v2/company"
        response = checko_api._CurlSession(checko_api.find_curl()).get(url, {"key": KEY, "inn": "7707083893", "source": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["НаимСокр"], "ООО ФИРМА")
        self.assertIn(f"key={KEY}", seen[0])
        self.assertIn("inn=7707083893", seen[0])


if __name__ == "__main__":
    unittest.main()
