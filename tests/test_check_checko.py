import errno
import os
import ssl
import sys
import tempfile
import unittest
from unittest import mock

import check_checko as cc

OK, FAIL, NO_WAY = (True, "ответ получен (HTTP 403)"), (False, "SSLError: обрыв"), (None, "способа нет")
SITE_OK, SITE_FAIL = (True, "открылся (HTTP 200)"), (False, "SSLError")
KEY = "SECRET-CHECKO-KEY-1"


NO_ADAPTERS = {"candidates": [], "error": "", "chosen": None}


def probes(default=FAIL, no_proxy=FAIL, bypass=FAIL, bypass_os=FAIL, tls12=FAIL, curl=FAIL, adapters=NO_ADAPTERS, cert=None, dns=(True, "1.2.3.4"), tcp=(True, "порт 443 открыт"),
           proxies=None, key=(True, "ключ принят"), public_dns=(True, "1.2.3.4"), sites=SITE_OK):
    modes = {"default": default, "no-proxy": no_proxy, "bypass": bypass, "bypass-os": bypass_os, "tls12": tls12, "curl": curl}
    return {"proxies": lambda: proxies or {}, "dns": lambda: dns, "tcp": lambda: tcp, "public_dns": lambda: public_dns,
            "tls": lambda tls12_only: (True, "шифрование установлено (TLSv1.3)"),
            "control": lambda url: sites, "api": lambda mode: modes[mode], "key": lambda k: key,
            "adapter": lambda: adapters, "cert": lambda card: cert}


def text_of(**kw):
    key = kw.pop("key_value", "")
    return " ".join(cc.verdict(cc.collect(key, probes(**kw))))


class VerdictTest(unittest.TestCase):
    def test_all_good(self):
        self.assertIn("работает так же, как в программе", text_of(default=OK, no_proxy=OK, bypass=OK, bypass_os=OK, tls12=OK, curl=OK))

    def test_only_without_proxy_works(self):
        text = text_of(no_proxy=OK, proxies={"https": "10.0.0.1:3128"})
        self.assertIn("БЕЗ прокси", text)
        self.assertIn("настроен прокси", text)

    def test_only_the_direct_way_around_the_vpn_works(self):
        text = text_of(bypass=OK)
        self.assertIn("Checko закрыт для адреса вашего VPN", text)
        self.assertIn("выключать VPN не нужно", text)

    def test_windows_accepts_the_certificate_but_python_does_not(self):
        text = text_of(bypass_os=OK)
        self.assertIn("Windows принимает его сертификат", text)
        self.assertIn("выключать VPN не нужно", text)
        self.assertIn("не отключается", text)

    def test_the_direct_way_wins_over_the_windows_check_when_both_work(self):
        self.assertNotIn("Windows принимает", text_of(bypass=OK, bypass_os=OK))

    def test_only_tls12_works(self):
        self.assertIn("TLS 1.2", text_of(tls12=OK))

    def test_only_curl_works_and_the_program_switches_to_it_by_itself(self):
        text = text_of(curl=OK)
        self.assertIn("curl", text)
        self.assertIn("делать ничего не нужно", text)
        self.assertIn("zapret", text)

    def test_nothing_works_but_other_sites_open_means_checko_closed_for_this_network(self):
        text = text_of(curl=NO_WAY)
        self.assertIn("api.checko.ru нет", text)
        self.assertIn("kill switch", text)
        self.assertIn("раздача с телефона", text)
        self.assertIn("поддержку Checko", text)

    def test_nothing_works_and_no_site_opens_means_pythons_https_is_broken(self):
        text = text_of(sites=SITE_FAIL)
        self.assertIn("ни с какими сайтами", text)
        self.assertIn("антивирус", text)

    def test_the_earlier_stages_are_named(self):
        self.assertIn("не находится", text_of(dns=(False, "нет")))
        self.assertIn("Порт 443", text_of(tcp=(False, "закрыт")))

    def test_a_proxy_makes_a_failed_direct_lookup_not_conclusive(self):
        text = text_of(dns=(False, "нет"), proxies={"https": "127.0.0.1:8080"})
        self.assertIn("настроен прокси", text)
        self.assertNotIn("нет интернета", text)

    def test_a_substituted_address_is_reported(self):
        text = text_of(public_dns=(True, "5.6.7.8"))
        self.assertIn("подменён", text)
        self.assertIn("1.1.1.1", text)
        self.assertNotIn("подменён", text_of(public_dns=(True, "5.6.7.8, 1.2.3.4")))        # хотя бы один адрес общий
        self.assertNotIn("подменён", text_of(public_dns=(None, "спросить не удалось")))      # не знаем - не обвиняем

    def test_the_key_is_checked_only_when_there_is_a_connection_and_a_key(self):
        self.assertIn("ключ принят", text_of(default=OK, key_value=KEY))
        self.assertIn("не принят", text_of(default=OK, key_value=KEY, key=(False, "HTTP 403")))
        self.assertNotIn("Ключ CHECKO_API_KEY", text_of(default=OK))                      # ключа нет - не проверяем
        self.assertNotIn("Ключ CHECKO_API_KEY", text_of(key_value=KEY))                   # связи нет - нечем проверять
        self.assertIn("ключ принят", text_of(curl=OK, key_value=KEY))                     # хватает и одного рабочего пути


def cert(issuer="commonName=Some CA", subject="commonName=api.checko.ru", verified=False, error="", verify_error="что-то"):
    return {"verified": verified, "verify_error": verify_error, "error": error, "subject": subject, "issuer": issuer,
            "valid": "Jan  1 00:00:00 2026 GMT - Jan  1 00:00:00 2027 GMT"}


class CertificateTest(unittest.TestCase):
    def advice(self, **kw):
        return " ".join(cc.cert_advice(cert(**kw)))

    def test_a_russian_ca_gets_the_instruction_with_the_official_link_and_the_setting_name(self):
        text = self.advice(issuer="countryName=RU, commonName=Russian Trusted Sub CA")
        self.assertIn("Минцифры", text)
        self.assertIn("CHECKO_CA_FILE", text)
        self.assertIn(cc.RUSSIAN_ROOT_URL, text)

    def test_an_antivirus_certificate_is_named(self):
        text = self.advice(issuer="commonName=Kaspersky Anti-Virus Personal Root Certificate")
        self.assertIn("антивирус", text)
        self.assertIn("api.checko.ru", text)

    def test_a_self_signed_certificate_means_the_connection_is_substituted(self):
        self.assertIn("самоподписанный", self.advice(issuer="commonName=router", subject="commonName=router"))

    def test_an_incomplete_chain_points_to_truststore(self):
        text = self.advice(verify_error="сервер прислал неполную цепочку сертификатов [unable to get local issuer certificate]")
        self.assertIn("truststore", text)

    def test_no_advice_when_the_certificate_is_fine_or_unknown(self):
        self.assertEqual(cc.cert_advice(None), [])
        self.assertEqual(cc.cert_advice(cert(verified=True)), [])
        self.assertEqual(self.advice(), "")

    def test_the_lines_show_who_issued_what_and_why_python_refused(self):
        lines = "\n".join(cc.cert_lines(cert(issuer="commonName=Some CA", verify_error="причина отказа")))
        for part in ("7. Сертификат", "кому выдан : commonName=api.checko.ru", "кем выдан  : commonName=Some CA", "действует",
                     "ОТКАЗ - причина отказа"):
            self.assertIn(part, lines)
        self.assertIn("принят", "\n".join(cc.cert_lines(cert(verified=True))))
        self.assertIn("не получен: сеть упала", "\n".join(cc.cert_lines(cert(issuer="", error="сеть упала"))))
        self.assertEqual(cc.cert_lines(None), [])

    def test_the_certificate_is_read_only_when_the_direct_way_failed(self):
        called = []
        probes_ = probes(adapters={"candidates": [OutputTest.CARD], "error": "", "chosen": OutputTest.CARD})
        probes_["cert"] = lambda card: called.append(card) or cert()
        report = cc.collect("", probes_)
        self.assertEqual(called, [OutputTest.CARD])
        self.assertIsNotNone(report.cert)
        called.clear()
        probes_ = probes(bypass=OK, adapters={"candidates": [OutputTest.CARD], "error": "", "chosen": OutputTest.CARD})
        probes_["cert"] = lambda card: called.append(card) or cert()
        self.assertIsNone(cc.collect("", probes_).cert)
        self.assertEqual(called, [])

    @unittest.skipUnless(__import__("shutil").which("openssl"), "openssl на этом компьютере нет")
    def test_reading_a_real_certificate_through_the_bound_socket(self):
        import http.server
        import socket as socketmod
        import threading
        import checko_api
        with tempfile.TemporaryDirectory() as tmp:
            crt, key = os.path.join(tmp, "c.pem"), os.path.join(tmp, "k.pem")
            made = __import__("subprocess").run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                                                 "-out", crt, "-days", "1", "-subj", "/CN=localhost",
                                                 "-addext", "subjectAltName=DNS:localhost"], capture_output=True)
            if made.returncode != 0:
                self.skipTest("openssl не создал сертификат")
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(crt, key)
            server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
            server.socket = server_context.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            original = checko_api.open_bound_socket
            unbound = lambda host, port, index, timeout, bind=True: original(host, port, index, timeout, bind=False)
            with mock.patch.object(cc, "HOST", "localhost"), mock.patch.object(cc, "PORT", server.server_port), \
                    mock.patch("checko_api.open_bound_socket", unbound):
                result = cc.probe_certificate({"ifIndex": 1})
        self.assertFalse(result["verified"])                          # самоподписанный: Python не доверяет
        self.assertIn("самоподписанный", result["verify_error"])
        self.assertIn("commonName=localhost", result["issuer"])
        self.assertIn("commonName=localhost", result["subject"])
        self.assertIn("GMT", result["valid"])

    def test_without_an_adapter_there_is_nothing_to_read(self):
        self.assertEqual(cc.probe_certificate(None)["error"], "нет подходящей сетевой карты")


class OutputTest(unittest.TestCase):
    CARD = {"ifIndex": 12, "NextHop": "192.168.1.1", "Metric": 25, "Name": "Ethernet",
            "Description": "Realtek PCIe GbE Family Controller", "Hardware": True}
    TUNNEL = {"ifIndex": 20, "NextHop": "10.8.0.1", "Metric": 1, "Name": "VPN", "Description": "Wintun Userspace Tunnel",
              "Hardware": False}

    def run_main(self, env_text, adapters=None, kw_cert=None, **kw):
        adapters = adapters or {"candidates": [self.TUNNEL, self.CARD], "error": "", "chosen": self.CARD}
        kw_cert = kw_cert or cert(issuer="commonName=Russian Trusted Sub CA")
        with tempfile.TemporaryDirectory() as tmp:
            env = os.path.join(tmp, "local.env")
            with open(env, "w", encoding="utf-8") as fh:
                fh.write(env_text)
            lines = []
            p = probes(**kw)
            with mock.patch.object(cc, "probe_proxies", p["proxies"]), mock.patch.object(cc, "probe_dns", p["dns"]), \
                    mock.patch.object(cc, "probe_tcp", p["tcp"]), mock.patch.object(cc, "probe_tls", p["tls"]), \
                    mock.patch.object(cc, "probe_api", p["api"]), mock.patch.object(cc, "probe_key", p["key"]), \
                    mock.patch.object(cc, "probe_public_dns", p["public_dns"]), \
                    mock.patch.object(cc, "probe_control", p["control"]), \
                    mock.patch.object(cc, "probe_adapter", lambda: adapters), \
                    mock.patch.object(cc, "probe_certificate", lambda card: kw_cert):
                code = cc.main(["check_checko.py", env], out=lines.append)
        return code, "\n".join(lines)

    def test_the_key_is_never_printed_and_exit_code_follows_the_connection(self):
        code, out = self.run_main(f"CHECKO_API_KEY={KEY}\n", default=OK)
        self.assertEqual(code, 0)
        self.assertNotIn(KEY, out)
        self.assertIn(f"задан, {len(KEY)} знаков", out)
        code, out = self.run_main("")
        self.assertEqual(code, 1)
        self.assertIn("не задан в local.env", out)

    def test_every_step_is_listed(self):
        _, out = self.run_main("", default=OK, curl=NO_WAY)
        for part in ("1. Адрес", "Публичный DNS", "2. Соединение", "3. Обычное шифрование", "3. Шифрование TLS 1.2",
                     "4. Другой сайт из Python, checko.ru", "4. Другой сайт из Python, ya.ru",
                     "5. Запрос к API (как в программе)", "5. Запрос к API (без прокси)",
                     "5. Запрос к API (в обход VPN, через сетевую карту)",
                     "5. Запрос к API (в обход VPN, сертификат проверяет Windows)", "5. Запрос к API (с TLS 1.2)",
                     "6. Сетевые карты с выходом в интернет:",
                     "№12 Ethernet (Realtek PCIe GbE Family Controller): метрика 25, шлюз 192.168.1.1, настоящая  <- через неё идёт обход VPN",
                     "№20 VPN (Wintun Userspace Tunnel): метрика 1, шлюз 10.8.0.1, виртуальная",
                     "5. Запрос к API (через curl, шифрование Windows): нет такого способа"):
            self.assertIn(part, out)

    def test_the_certificate_block_and_its_advice_are_printed_when_the_direct_way_failed(self):
        _, out = self.run_main("")
        self.assertIn("7. Сертификат, который присылает Checko", out)
        self.assertIn("кем выдан  : commonName=Russian Trusted Sub CA", out)
        self.assertIn("CHECKO_CA_FILE", out)
        _, out = self.run_main("", default=OK, bypass=OK)
        self.assertNotIn("7. Сертификат", out)

    def test_the_exclusion_hint_names_both_pythons_only_when_the_connection_failed(self):
        _, out = self.run_main("")
        self.assertIn("добавьте туда", out)
        self.assertIn(sys.executable, out)
        _, out = self.run_main("", default=OK)
        self.assertNotIn("добавьте туда", out)

    def test_when_no_adapter_fits_or_the_list_failed_the_reason_is_printed(self):
        _, out = self.run_main("", adapters={"candidates": [self.TUNNEL], "error": "", "chosen": None})
        self.assertIn("№20 VPN", out)
        self.assertIn("Подходящей карты для обхода VPN нет", out)
        _, out = self.run_main("", adapters={"candidates": [], "error": "PowerShell не запустился (нет файла)", "chosen": None})
        self.assertIn("список получить не удалось: PowerShell не запустился", out)
        _, out = self.run_main("", adapters={"candidates": [], "error": "", "chosen": None})
        self.assertIn("нет ни одной подключённой карты", out)

    def test_a_computer_without_curl_is_not_a_failure_of_the_connection(self):
        code, _ = self.run_main("", curl=NO_WAY)
        self.assertEqual(code, 1)                                  # связи нет, но curl-то просто нет
        code, _ = self.run_main("", curl=OK)
        self.assertEqual(code, 0)


class HelpersTest(unittest.TestCase):
    def test_proxy_address_is_shown_without_login_and_password(self):
        self.assertEqual(cc.hide_login("http://user:pass@10.0.0.1:3128"), "10.0.0.1:3128")
        self.assertEqual(cc.hide_login("127.0.0.1:8080"), "127.0.0.1:8080")
        self.assertEqual(cc.hide_login("http://proxy.example"), "proxy.example")

    def test_probe_api_hides_the_key_in_errors(self):
        class Boom:
            def get(self, *args, **kwargs):
                raise OSError(f"failed for key={KEY}")

        with mock.patch("checko_api.new_session", return_value=Boom()):
            ok, text = cc.probe_api("default", KEY)
        self.assertFalse(ok)
        self.assertNotIn(KEY, text)

    def test_any_http_answer_counts_as_a_connection(self):
        class Resp:
            status_code = 403

        class Session:
            def get(self, *args, **kwargs):
                return Resp()

        with mock.patch("checko_api.new_session", return_value=Session()):
            self.assertEqual(cc.probe_api("default"), (True, "ответ получен (HTTP 403)"))

    def test_no_curl_is_reported_as_no_such_way(self):
        with mock.patch("checko_api.new_session", return_value=None):
            self.assertEqual(cc.probe_api("curl")[0], None)

    def test_a_handshake_drop_shown_as_no_such_file_is_explained(self):
        text = cc.describe_tls_error(FileNotFoundError(errno.ENOENT, "No such file or directory"))
        self.assertIn("соединение оборвано", text)
        self.assertIn("файл тут ни при чём", text)
        self.assertIn("SSLEOFError", cc.describe_tls_error(ssl.SSLEOFError(8, "EOF")))

    def test_public_dns_answer_is_read_and_failures_are_not_accusations(self):
        class Answer:
            def json(self):
                return {"Answer": [{"type": 5, "data": "alias."}, {"type": 1, "data": "9.9.9.9"}, {"type": 1, "data": "1.1.1.1"}]}

        with mock.patch("check_checko.requests.get", return_value=Answer()):
            self.assertEqual(cc.probe_public_dns(), (True, "1.1.1.1, 9.9.9.9"))
        with mock.patch("check_checko.requests.get", side_effect=OSError("нет")):
            self.assertEqual(cc.probe_public_dns()[0], None)


if __name__ == "__main__":
    unittest.main()
