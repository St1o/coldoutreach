import json
import os
import shutil
import socket
import ssl
import struct
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
        made = {mode: by_mode.get(mode, FakeSession(error=self.SSL)) for mode in ("no-proxy", "bypass", "tls12", "curl")}
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

    def test_bypassing_the_vpn_comes_before_tls12_and_curl(self):
        made = self.sessions(bypass=FakeSession(FakeResponse(payload())))
        api = CheckoApi(KEY)
        self.assertTrue(api.company("1"))
        self.assertEqual(api.mode, "bypass")
        self.assertEqual((len(made["no-proxy"].calls), made["tls12"].calls, made["curl"].calls), (1, [], []))

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


class BypassVpnTest(unittest.TestCase):
    """Запрос мимо VPN: поиск настоящей сетевой карты и соединение, привязанное к ней."""

    ETHERNET = {"ifIndex": 12, "NextHop": "192.168.1.1", "Metric": 25, "Name": "Ethernet",
                "Description": "Realtek PCIe GbE Family Controller", "Hardware": True}
    TUNNEL = {"ifIndex": 20, "NextHop": "10.8.0.1", "Metric": 1, "Name": "Подключение VPN",
              "Description": "Wintun Userspace Tunnel", "Hardware": False}

    def setUp(self):
        checko_api._adapters_cache.clear()
        self.addCleanup(checko_api._adapters_cache.clear)

    def run_powershell(self, stdout=None, code=0, error=None, stderr=b""):
        stdout = json.dumps([self.TUNNEL, self.ETHERNET]).encode() if stdout is None else stdout
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if error:
                raise error
            return subprocess.CompletedProcess(command, code, stdout, stderr)

        with mock.patch("checko_api._is_windows", return_value=True), mock.patch("checko_api.subprocess.run", fake_run):
            return checko_api.adapter_candidates(), calls

    def test_not_windows_means_no_such_way_and_no_command_is_run(self):
        with mock.patch("checko_api.subprocess.run") as run:
            self.assertEqual(checko_api.adapter_candidates(), ([], "это не Windows"))
            self.assertIsNone(checko_api.physical_interface_index())
        run.assert_not_called()

    def test_all_connected_adapters_with_a_route_to_the_internet_are_listed_without_admin_rights(self):
        (candidates, error), calls = self.run_powershell()
        self.assertEqual((error, [c["ifIndex"] for c in candidates]), ("", [20, 12]))
        self.assertEqual(candidates[1], self.ETHERNET)
        command = calls[0]
        self.assertEqual(command[0], "powershell")
        self.assertIn("-NoProfile", command)
        self.assertIn("Get-NetAdapter", command[-1])
        self.assertIn("0.0.0.0/0", command[-1])

    def test_one_adapter_arrives_as_an_object_not_a_list(self):
        (candidates, _), _ = self.run_powershell(stdout=json.dumps(self.ETHERNET).encode())
        self.assertEqual(candidates, [self.ETHERNET])

    def test_the_answer_is_remembered(self):
        with mock.patch("checko_api._is_windows", return_value=True), \
                mock.patch("checko_api.subprocess.run",
                           return_value=subprocess.CompletedProcess([], 0, json.dumps([self.ETHERNET]).encode(), b"")) as run:
            self.assertEqual(checko_api.physical_interface_index(), 12)
            self.assertEqual(checko_api.physical_interface_index(), 12)
        self.assertEqual(run.call_count, 1)

    def test_failures_are_explained(self):
        cases = ((dict(stdout=b"", code=1, stderr="Get-NetAdapter : нет доступа".encode("utf-8")), "PowerShell вернул ошибку: Get-NetAdapter"),
                 (dict(stdout=b"not json"), "не удалось разобрать"),
                 (dict(stdout=b'[{"NextHop":"1.1.1.1"}]'), "не удалось разобрать"),
                 (dict(error=OSError(2, "нет powershell")), "PowerShell не запустился"),
                 (dict(error=subprocess.TimeoutExpired("powershell", 40)), "не ответил за 40 секунд"))
        for kwargs, expected in cases:
            checko_api._adapters_cache.clear()
            (candidates, error), _ = self.run_powershell(**kwargs)
            self.assertEqual(candidates, [], kwargs)
            self.assertIn(expected, error)
        checko_api._adapters_cache.clear()
        (candidates, error), _ = self.run_powershell(stdout=b"")                  # нет подходящих карт - не ошибка
        self.assertEqual((candidates, error), ([], ""))

    def test_a_tunnel_is_never_picked_and_the_real_adapter_with_the_lowest_metric_wins(self):
        pick = checko_api.pick_adapter
        self.assertEqual(pick([self.TUNNEL, self.ETHERNET])["ifIndex"], 12)
        wifi = dict(self.ETHERNET, ifIndex=9, Name="Wi-Fi", Description="Intel(R) Wi-Fi 6 AX201", Metric=40)
        self.assertEqual(pick([self.TUNNEL, wifi, self.ETHERNET])["ifIndex"], 12)
        self.assertEqual(pick([wifi, dict(self.ETHERNET, Metric=50)])["ifIndex"], 9)
        self.assertIsNone(pick([self.TUNNEL]))
        self.assertIsNone(pick([]))

    def test_tunnels_and_virtual_machines_are_recognised_by_name_or_description(self):
        for description in ("WireGuard Tunnel", "TAP-Windows Adapter V9", "Wintun Userspace Tunnel", "OpenVPN Data Channel Offload",
                            "Hyper-V Virtual Ethernet Adapter", "VirtualBox Host-Only Ethernet Adapter", "Cloudflare WARP Interface",
                            "NordLynx Tunnel", "Amnezia Virtual Network", "sing-box Tunnel"):
            card = dict(self.ETHERNET, Description=description, Name="Ethernet 2")
            self.assertIsNone(checko_api.pick_adapter([card]), description)
        for description in ("Realtek PCIe GbE Family Controller", "Intel(R) Wi-Fi 6 AX201 160MHz", "Qualcomm Atheros AR9485"):
            card = dict(self.ETHERNET, Description=description)
            self.assertIsNotNone(checko_api.pick_adapter([card]), description)

    def test_a_real_looking_adapter_flagged_virtual_is_still_used_as_the_second_choice(self):
        modem = dict(self.ETHERNET, Name="Ethernet 3", Description="Remote NDIS based Internet Sharing Device", Hardware=False)
        self.assertEqual(checko_api.pick_adapter([self.TUNNEL, modem])["ifIndex"], 12)
        self.assertEqual(checko_api.pick_adapter([modem, self.ETHERNET])["ifIndex"], 12)      # настоящая карта всё же главнее

    def test_the_session_exists_only_when_there_is_an_adapter(self):
        with mock.patch("checko_api.physical_interface_index", return_value=None):
            self.assertIsNone(checko_api.new_session("bypass"))
        with mock.patch("checko_api.physical_interface_index", return_value=12):
            session = checko_api.new_session("bypass")
        self.assertIsInstance(session, checko_api._BoundSession)
        self.assertEqual(session.if_index, 12)
        self.assertTrue(session.context.check_hostname)                         # проверка сертификата не отключена

    def test_the_socket_is_pinned_to_the_adapter_before_connecting(self):
        recorded = []

        class FakeSocket:
            def __init__(self, *args):
                recorded.append(("created",))

            def setsockopt(self, *args):
                recorded.append(("setsockopt",) + args)

            def settimeout(self, value):
                recorded.append(("settimeout", value))

            def connect(self, address):
                recorded.append(("connect", address))
                raise OSError("нет пути")

            def close(self):
                recorded.append(("close",))

        conn = checko_api._BoundHTTPSConnection("localhost", 443, 12, 5, ssl.create_default_context())
        with mock.patch("checko_api.socket.socket", FakeSocket), self.assertRaises(OSError):
            conn.connect()
        option = [item for item in recorded if item[0] == "setsockopt"][0]
        self.assertEqual(option[1:], (socket.IPPROTO_IP, 31, struct.pack("!I", 12)))   # IP_UNICAST_IF, номер карты в сетевом порядке
        self.assertLess(recorded.index(option), recorded.index([i for i in recorded if i[0] == "connect"][0]))

    @unittest.skipUnless(shutil.which("openssl"), "openssl на этом компьютере нет")
    def test_a_request_through_the_bound_connection_with_a_real_tls_server(self):
        import http.server
        import tempfile
        import threading
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cert, key = os.path.join(tmp.name, "c.pem"), os.path.join(tmp.name, "k.pem")
        made = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                               "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                              capture_output=True)
        if made.returncode != 0:
            self.skipTest("openssl не создал сертификат")
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Host")))
                body = json.dumps(payload()).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"https://localhost:{server.server_port}/v2/company"

        trusting = ssl.create_default_context(cafile=cert)
        response = checko_api._BoundSession(1, trusting, bind=False).get(url, {"key": KEY, "inn": "7707083893"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["НаимСокр"], "ООО ФИРМА")
        self.assertIn("key=" + KEY, seen[0][0])
        with self.assertRaises(requests.exceptions.ConnectionError) as ctx:      # сертификат не доверенный: обрыв, не «успех»
            checko_api._BoundSession(1, ssl.create_default_context(), bind=False).get(url, {})
        self.assertIn("в обход VPN", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
