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


def probes(default=FAIL, no_proxy=FAIL, bypass=FAIL, tls12=FAIL, curl=FAIL, dns=(True, "1.2.3.4"), tcp=(True, "порт 443 открыт"),
           proxies=None, key=(True, "ключ принят"), public_dns=(True, "1.2.3.4"), sites=SITE_OK):
    modes = {"default": default, "no-proxy": no_proxy, "bypass": bypass, "tls12": tls12, "curl": curl}
    return {"proxies": lambda: proxies or {}, "dns": lambda: dns, "tcp": lambda: tcp, "public_dns": lambda: public_dns,
            "tls": lambda tls12_only: (True, "шифрование установлено (TLSv1.3)"),
            "control": lambda url: sites, "api": lambda mode: modes[mode], "key": lambda k: key}


def text_of(**kw):
    key = kw.pop("key_value", "")
    return " ".join(cc.verdict(cc.collect(key, probes(**kw))))


class VerdictTest(unittest.TestCase):
    def test_all_good(self):
        self.assertIn("работает так же, как в программе", text_of(default=OK, no_proxy=OK, bypass=OK, tls12=OK, curl=OK))

    def test_only_without_proxy_works(self):
        text = text_of(no_proxy=OK, proxies={"https": "10.0.0.1:3128"})
        self.assertIn("БЕЗ прокси", text)
        self.assertIn("настроен прокси", text)

    def test_only_the_direct_way_around_the_vpn_works(self):
        text = text_of(bypass=OK)
        self.assertIn("Checko закрыт для адреса вашего VPN", text)
        self.assertIn("выключать VPN не нужно", text)

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


class OutputTest(unittest.TestCase):
    CARD = {"ifIndex": 12, "NextHop": "192.168.1.1", "Metric": 25, "Name": "Ethernet",
            "Description": "Realtek PCIe GbE Family Controller", "Hardware": True}
    TUNNEL = {"ifIndex": 20, "NextHop": "10.8.0.1", "Metric": 1, "Name": "VPN", "Description": "Wintun Userspace Tunnel",
              "Hardware": False}

    def run_main(self, env_text, adapters=None, **kw):
        adapters = adapters or {"candidates": [self.TUNNEL, self.CARD], "error": "", "chosen": self.CARD}
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
                    mock.patch.object(cc, "probe_adapter", lambda: adapters):
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
                     "5. Запрос к API (в обход VPN, через сетевую карту)", "5. Запрос к API (с TLS 1.2)",
                     "6. Сетевые карты с выходом в интернет:",
                     "№12 Ethernet (Realtek PCIe GbE Family Controller): метрика 25, шлюз 192.168.1.1, настоящая  <- через неё идёт обход VPN",
                     "№20 VPN (Wintun Userspace Tunnel): метрика 1, шлюз 10.8.0.1, виртуальная",
                     "5. Запрос к API (через curl, шифрование Windows): нет такого способа"):
            self.assertIn(part, out)

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
