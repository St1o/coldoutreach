"""Проверка MX почтовых доменов (mailing/mx.py): клиент DNS по проводу против поддельного сервера на 127.0.0.1, кеш на сутки,
сбои DNS. В настоящий DNS тесты не ходят."""
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mailing import config, mx


def encode_name(name):
    return b"".join(bytes([len(label)]) + label.encode() for label in name.split(".")) + b"\x00"


def question_end(query):
    pos = 12
    while query[pos]:
        pos += query[pos] + 1
    return pos + 5                                       # нулевая метка и 4 байта типа и класса


def answer(query, hosts, rcode=0, truncated=False, ident=None):
    """Ответ DNS на query: hosts - [(предпочтение, имя)]; имя вида xxx.<спрошенный домен> пишется с указателем сжатия."""
    suffix = "." + domain_text(query[12:])
    flags = 0x8180 | rcode | (0x0200 if truncated else 0)
    body = b""
    for preference, host in hosts:
        if host.endswith(suffix):
            rdata = struct.pack("!H", preference) + encode_name(host[:-len(suffix)])[:-1] + b"\xc0\x0c"
        else:
            rdata = struct.pack("!H", preference) + encode_name(host)
        body += b"\xc0\x0c" + struct.pack("!HHIH", 15, 1, 300, len(rdata)) + rdata
    ident = struct.unpack("!H", query[:2])[0] if ident is None else ident
    return struct.pack("!6H", ident, flags, 1, len(hosts), 0, 0) + query[12:question_end(query)] + body


def domain_text(question_name):
    labels, pos = [], 0
    while question_name[pos]:
        labels.append(question_name[pos + 1:pos + 1 + question_name[pos]].decode())
        pos += question_name[pos] + 1
    return ".".join(labels)


class UdpDns:
    """Поддельный сервер DNS: handler(пакет) -> список пакетов-ответов (пусто - молчит)."""

    def __init__(self, handler, tcp_handler=None):
        self.handler, self.queries, self.tcp = handler, [], None
        for _ in range(20):                                  # TCP на том же номере порта, что и UDP: если занят, берём другой
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("127.0.0.1", 0))
            self.port = self.sock.getsockname()[1]
            if not tcp_handler:
                break
            self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                self.tcp.bind(("127.0.0.1", self.port))
            except OSError:
                self.sock.close()
                self.tcp.close()
                continue
            self.tcp.listen(2)
            self.tcp.settimeout(0.05)
            break
        else:
            raise OSError("не нашлось порта для поддельного DNS")
        self.sock.settimeout(0.05)
        self.address = ("127.0.0.1", self.port)
        self.stopping = threading.Event()
        self.threads = [threading.Thread(target=self._udp, daemon=True)]
        if self.tcp:
            self.tcp_handler = tcp_handler
            self.threads.append(threading.Thread(target=self._tcp, daemon=True))
        for thread in self.threads:
            thread.start()

    def _udp(self):
        while not self.stopping.is_set():
            try:
                data, peer = self.sock.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                return
            self.queries.append(data)
            for packet in self.handler(data) or []:
                self.sock.sendto(packet, peer)

    def _tcp(self):
        while not self.stopping.is_set():
            try:
                conn, _ = self.tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(1)
                size = struct.unpack("!H", conn.recv(2))[0]
                query = conn.recv(size)
                reply = self.tcp_handler(query)
                conn.sendall(struct.pack("!H", len(reply)) + reply)

    def close(self):
        self.stopping.set()
        for thread in self.threads:
            thread.join()
        self.sock.close()
        if self.tcp:
            self.tcp.close()


class WireTest(unittest.TestCase):
    def serve(self, handler, tcp_handler=None):
        server = UdpDns(handler, tcp_handler)
        self.addCleanup(server.close)
        return server

    def test_query_has_one_mx_question_with_recursion_desired(self):
        query = mx.build_query("Firm.Example.ru.", 0x1234)
        ident, flags, questions, *rest = struct.unpack("!6H", query[:12])
        self.assertEqual((ident, flags, questions, rest), (0x1234, 0x0100, 1, [0, 0, 0]))
        self.assertEqual(query[12:], encode_name("firm.example.ru") + struct.pack("!HH", 15, 1))

    def test_bad_domain_names_are_refused(self):
        for bad in ["", "a..ru", "x" * 64 + ".ru"]:
            with self.assertRaises(mx.MxError, msg=bad):
                mx.build_query(bad, 1)

    def test_idn_domain_is_sent_in_punycode(self):
        query = mx.build_query("компания.рф", 1)
        self.assertIn(b"xn--", query)

    def test_hosts_come_back_sorted_by_preference_and_compressed_names_are_read(self):
        server = self.serve(lambda q: [answer(q, [(20, "mx2.firm.ru"), (10, "MX.Yandex.NET")])])
        self.assertEqual(mx.lookup_mx("firm.ru", [server.address], timeout=1), ["mx.yandex.net", "mx2.firm.ru"])

    def test_no_mx_records_and_unknown_domain_are_an_empty_list(self):
        server = self.serve(lambda q: [answer(q, [])])
        self.assertEqual(mx.lookup_mx("firm.ru", [server.address], timeout=1), [])
        server = self.serve(lambda q: [answer(q, [], rcode=3)])
        self.assertEqual(mx.lookup_mx("nope.ru", [server.address], timeout=1), [])

    def test_a_failing_server_is_skipped_for_the_next_one(self):
        broken = self.serve(lambda q: [answer(q, [], rcode=2)])                      # SERVFAIL
        silent = self.serve(lambda q: [])
        good = self.serve(lambda q: [answer(q, [(10, "mx.yandex.ru")])])
        found = mx.lookup_mx("firm.ru", [broken.address, silent.address, good.address], timeout=0.2)
        self.assertEqual(found, ["mx.yandex.ru"])
        self.assertEqual((len(broken.queries), len(silent.queries), len(good.queries)), (1, 1, 1))

    def test_every_server_failing_raises_mx_error(self):
        silent = self.serve(lambda q: [])
        with self.assertRaises(mx.MxError) as ctx:
            mx.lookup_mx("firm.ru", [silent.address], timeout=0.2)
        self.assertIn("DNS не ответил", str(ctx.exception))
        with self.assertRaises(mx.MxError):
            mx.lookup_mx("firm.ru", [], timeout=0.2)

    def test_a_stray_packet_with_another_id_is_ignored(self):
        server = self.serve(lambda q: [answer(q, [(10, "evil.example.com")], ident=0xFFFF ^ struct.unpack("!H", q[:2])[0]),
                                       answer(q, [(10, "mx.yandex.ru")])])
        self.assertEqual(mx.lookup_mx("firm.ru", [server.address], timeout=1), ["mx.yandex.ru"])

    def test_garbage_and_name_loops_are_errors_not_crashes(self):
        for bad in (b"\x00\x01", b"x" * 40):
            server = self.serve(lambda q, bad=bad: [bad])
            with self.assertRaises(mx.MxError):
                mx.lookup_mx("firm.ru", [server.address], timeout=0.2)
        loop = struct.pack("!6H", 1, 0x8180, 1, 1, 0, 0) + encode_name("a.ru") + struct.pack("!HH", 15, 1)
        loop += b"\xc0\x0c" + struct.pack("!HHIH", 15, 1, 1, 4) + struct.pack("!H", 10) + b"\xc0" + bytes([len(loop) + 12 + 2 + 8])
        with self.assertRaises(mx.MxError):
            mx.parse_response(loop, 1)

    def test_a_truncated_udp_answer_is_asked_again_over_tcp(self):
        server = self.serve(lambda q: [answer(q, [], truncated=True)], tcp_handler=lambda q: answer(q, [(10, "mx.yandex.ru")]))
        self.assertEqual(mx.lookup_mx("firm.ru", [server.address], timeout=1), ["mx.yandex.ru"])


class YandexHostTest(unittest.TestCase):
    def test_hosts(self):
        for host in ["mx.yandex.ru", "mx.yandex.net", "MX.YANDEX.RU.", "yandex.ru", "mx.yandex.com", "mx.yandex.by",
                     "mx.yandex.kz", "mx.yandex.com.tr", "mxs.yandex.ru"]:
            self.assertTrue(mx.is_yandex_host(host), host)
        for host in ["mxs.mail.ru", "notyandex.ru", "yandex-mx.example.com", "mx.yandex.example.com", "yandex", "",
                     "mx.google.com", "aspmx.l.google.com"]:
            self.assertFalse(mx.is_yandex_host(host), host)


class ServersTest(unittest.TestCase):
    def test_own_servers_replace_everything(self):
        self.assertEqual(mx.dns_servers({"MX_DNS_SERVERS": "9.9.9.9, 8.8.4.4 ;1.0.0.1"}), ["9.9.9.9", "8.8.4.4", "1.0.0.1"])

    def test_system_servers_come_first_then_the_public_ones_without_repeats(self):
        with mock.patch.object(mx, "_resolv_conf_servers", return_value=["192.168.1.1", "8.8.8.8"]), \
                mock.patch.object(mx, "_windows_servers", return_value=["192.168.1.1", "8.8.8.8"]):
            self.assertEqual(mx.dns_servers({}), ["192.168.1.1", "8.8.8.8", "1.1.1.1"])

    def test_nothing_found_means_the_public_servers(self):
        with mock.patch.object(mx, "_resolv_conf_servers", return_value=[]), mock.patch.object(mx, "_windows_servers", return_value=[]):
            self.assertEqual(mx.dns_servers({}), list(mx.FALLBACK_SERVERS))

    def test_resolv_conf(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "resolv.conf")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# comment\nsearch lan\nnameserver 10.0.0.1 # router\nnameserver fe80::1%eth0\nnameserver 2001:4860:4860::8888\n")
            self.assertEqual(mx._resolv_conf_servers(path), ["10.0.0.1", "2001:4860:4860::8888"])
            self.assertEqual(mx._resolv_conf_servers(os.path.join(folder, "нет")), [])

    def test_check_switch(self):
        self.assertTrue(config.resolve_check_mx({}))
        self.assertTrue(config.resolve_check_mx({"CHECK_MX": "1"}))
        for off in ("0", "false", "No", "off", "нет"):
            self.assertFalse(config.resolve_check_mx({"CHECK_MX": off}), off)


class CheckerTest(unittest.TestCase):
    NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = [self.NOW]
        self.calls = []
        self.answers = {"yfirm.ru": ["mx.yandex.net"], "plain.ru": ["mx.plain.ru"], "none.ru": []}

    def lookup(self, domain):
        self.calls.append(domain)
        answer_ = self.answers[domain]
        if isinstance(answer_, Exception):
            raise answer_
        return answer_

    def checker(self, **kw):
        return mx.MxChecker(self.dir.name, self.lookup, now=lambda: self.clock[0], **kw)

    def cache_file(self):
        with open(os.path.join(self.dir.name, mx.CACHE_NAME), encoding="utf-8") as fh:
            return json.load(fh)

    def test_verdicts_and_counts(self):
        checker = self.checker()
        self.assertEqual([checker.status(d) for d in ("yfirm.ru", "plain.ru", "none.ru", "YFIRM.ru.")],
                         [mx.YANDEX, mx.CLEAN, mx.CLEAN, mx.YANDEX])
        self.assertEqual((checker.checked, checker.count(mx.YANDEX), checker.count(mx.UNCHECKED)), (3, 1, 0))
        self.assertEqual(self.calls, ["yfirm.ru", "plain.ru", "none.ru"])                  # каждый домен спрашивается один раз

    def test_answers_are_cached_for_a_day_in_the_data_folder(self):
        first = self.checker()
        first.status("yfirm.ru")
        first.status("plain.ru")
        first.save()
        data = self.cache_file()
        self.assertEqual(sorted(data["domains"]), ["plain.ru", "yfirm.ru"])
        self.assertEqual(data["domains"]["yfirm.ru"]["mx"], ["mx.yandex.net"])
        self.assertNotIn("@", json.dumps(data))
        self.calls.clear()
        self.clock[0] = self.NOW + timedelta(hours=23, minutes=59)
        second = self.checker()
        self.assertEqual((second.status("yfirm.ru"), second.status("plain.ru")), (mx.YANDEX, mx.CLEAN))
        self.assertEqual(self.calls, [])
        self.assertEqual(second.checked, 2)
        self.clock[0] = self.NOW + timedelta(hours=24, minutes=1)                         # сутки вышли: спрашиваем заново
        third = self.checker()
        self.assertEqual(third.status("yfirm.ru"), mx.YANDEX)
        self.assertEqual(self.calls, ["yfirm.ru"])
        third.save()
        self.assertEqual(sorted(self.cache_file()["domains"]), ["yfirm.ru"])               # устаревшее из файла ушло

    def test_a_dns_failure_is_unchecked_and_is_not_cached(self):
        self.answers["plain.ru"] = mx.MxError("DNS не ответил (таймаут)")
        checker = self.checker()
        self.assertEqual(checker.status("plain.ru"), mx.UNCHECKED)
        self.assertEqual(checker.status("yfirm.ru"), mx.YANDEX)                            # сбой одного домена другим не мешает
        self.assertEqual((checker.checked, checker.count(mx.UNCHECKED), checker.cause), (1, 1, "DNS не ответил (таймаут)"))
        checker.save()
        self.assertEqual(sorted(self.cache_file()["domains"]), ["yfirm.ru"])
        self.answers["plain.ru"] = ["mx.plain.ru"]
        again = self.checker()
        self.assertEqual(again.status("plain.ru"), mx.CLEAN)                                # в следующий раз проверка повторяется

    def test_any_error_in_the_lookup_is_a_failure_not_a_crash(self):
        self.answers["plain.ru"] = ValueError("странный ответ")
        checker = self.checker()
        self.assertEqual(checker.status("plain.ru"), mx.UNCHECKED)
        self.assertEqual(checker.cause, "странный ответ")

    def test_after_several_failures_in_a_row_dns_is_not_asked_any_more(self):
        down = mx.MxError("DNS не ответил (таймаут)")
        for number in range(6):
            self.answers[f"d{number}.ru"] = down
        self.answers["plain.ru"] = ["mx.plain.ru"]
        seed = self.checker()
        seed.status("yfirm.ru")
        seed.save()
        self.calls.clear()
        checker = self.checker()
        verdicts = [checker.status(f"d{number}.ru") for number in range(6)]
        self.assertEqual(verdicts, [mx.UNCHECKED] * 6)
        self.assertEqual(len(self.calls), 3)                                              # остальные три без запроса
        self.assertEqual(checker.status("yfirm.ru"), mx.YANDEX)                           # а кеш по-прежнему работает
        self.assertEqual(checker.status("plain.ru"), mx.UNCHECKED)

    def test_a_success_resets_the_failure_streak(self):
        down = mx.MxError("таймаут")
        for name in ("a.ru", "b.ru", "c.ru", "d.ru"):
            self.answers[name] = down
        checker = self.checker()
        for name in ("a.ru", "b.ru"):
            checker.status(name)
        self.assertEqual(checker.status("plain.ru"), mx.CLEAN)
        checker.status("c.ru")
        checker.status("d.ru")
        self.assertEqual(len(self.calls), 5)

    def test_broken_or_foreign_cache_files_are_ignored(self):
        path = os.path.join(self.dir.name, mx.CACHE_NAME)
        for text in ("{not json", "[]", json.dumps({"domains": {"plain.ru": "строка", "yfirm.ru": {"time": "вчера", "yandex": True}}})):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            checker = self.checker()
            self.assertEqual(checker.status("plain.ru"), mx.CLEAN)
            self.assertEqual(checker.status("yfirm.ru"), mx.YANDEX)

    def test_without_a_folder_nothing_is_written(self):
        checker = mx.MxChecker("", self.lookup, now=lambda: self.NOW)
        checker.status("plain.ru")
        checker.save()
        self.assertEqual(os.listdir(self.dir.name), [])


if __name__ == "__main__":
    unittest.main()
