import os
import tempfile
import unittest
from unittest import mock

import check_checko as cc

OK, FAIL = (True, "ответ получен (HTTP 403)"), (False, "SSLError: обрыв")
KEY = "SECRET-CHECKO-KEY-1"


def probes(default=FAIL, no_proxy=FAIL, tls12=FAIL, dns=(True, "1.2.3.4"), tcp=(True, "порт 443 открыт"),
           proxies=None, key=(True, "ключ принят")):
    modes = {"default": default, "no-proxy": no_proxy, "tls12": tls12}
    return {"proxies": lambda: proxies or {}, "dns": lambda: dns, "tcp": lambda: tcp,
            "tls": lambda tls12_only: (True, "шифрование установлено (TLSv1.3)"),
            "api": lambda mode: modes[mode], "key": lambda k: key}


def text_of(**kw):
    key = kw.pop("key_value", "")
    return " ".join(cc.verdict(cc.collect(key, probes(**kw))))


class VerdictTest(unittest.TestCase):
    def test_all_good(self):
        self.assertIn("работает так же, как в программе", text_of(default=OK, no_proxy=OK, tls12=OK))

    def test_only_without_proxy_works(self):
        text = text_of(no_proxy=OK, proxies={"https": "10.0.0.1:3128"})
        self.assertIn("БЕЗ прокси", text)
        self.assertIn("настроен прокси", text)

    def test_only_tls12_works(self):
        self.assertIn("TLS 1.2", text_of(tls12=OK))

    def test_nothing_works_names_the_stage(self):
        self.assertIn("не находится", text_of(dns=(False, "нет")))
        self.assertIn("Порт 443", text_of(tcp=(False, "закрыт")))
        text = text_of()
        self.assertIn("обрывается при согласовании шифрования", text)
        self.assertIn("VPN", text)
        self.assertIn("антивирус", text)

    def test_a_proxy_makes_a_failed_direct_lookup_not_conclusive(self):
        text = text_of(dns=(False, "нет"), proxies={"https": "127.0.0.1:8080"})
        self.assertIn("настроен прокси", text)
        self.assertNotIn("нет интернета", text)

    def test_the_key_is_checked_only_when_there_is_a_connection_and_a_key(self):
        self.assertIn("ключ принят", text_of(default=OK, key_value=KEY))
        self.assertIn("не принят", text_of(default=OK, key_value=KEY, key=(False, "HTTP 403")))
        self.assertNotIn("Ключ CHECKO_API_KEY", text_of(default=OK))                      # ключа нет - не проверяем
        self.assertNotIn("Ключ CHECKO_API_KEY", text_of(key_value=KEY))                   # связи нет - нечем проверять


class OutputTest(unittest.TestCase):
    def run_main(self, env_text, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.path.join(tmp, "local.env")
            with open(env, "w", encoding="utf-8") as fh:
                fh.write(env_text)
            lines = []
            p = probes(**kw)
            with mock.patch.object(cc, "probe_proxies", p["proxies"]), mock.patch.object(cc, "probe_dns", p["dns"]), \
                    mock.patch.object(cc, "probe_tcp", p["tcp"]), mock.patch.object(cc, "probe_tls", p["tls"]), \
                    mock.patch.object(cc, "probe_api", p["api"]), mock.patch.object(cc, "probe_key", p["key"]):
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
        _, out = self.run_main("", default=OK)
        for part in ("1. Адрес", "2. Соединение", "3. Обычное шифрование", "3. Шифрование TLS 1.2",
                     "4. Запрос к API (как в программе)", "4. Запрос к API (без прокси)", "4. Запрос к API (с TLS 1.2)"):
            self.assertIn(part, out)


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


if __name__ == "__main__":
    unittest.main()
