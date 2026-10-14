"""Сборка писем (compose.py) и адреса на серверах Яндекса: MX домена, кеш в папке данных, сбои DNS, строка отчёта."""
import csv
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import compose
from mailing import journal, mx
from mailing import table as lettertable
from tests.test_compose import HEADER, LETTERS, LINK, SEGMENT, SIGNATURE, lead, make_composer, sent

YANDEX_LINE = "почта на серверах Яндекса (вечный блок владельца 2026-10-14)"


class FakeDns:
    """Подставка вместо настоящего DNS: домен -> список MX или исключение; помнит, кого спрашивали."""

    def __init__(self, **answers):
        self.answers = {name.replace("_", "."): value for name, value in answers.items()}
        self.calls = []

    def __call__(self, domain):
        self.calls.append(domain)
        found = self.answers.get(domain, ["mx." + domain])
        if isinstance(found, Exception):
            raise found
        return found


class MxComposeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.work = os.path.join(self.dir.name, "data")
        self.project = os.path.join(self.dir.name, "agents")
        os.makedirs(self.work)
        os.makedirs(self.project)
        self.letters, self.segment = (os.path.join(self.project, name) for name in ("letters.md", "segment.md"))
        for path, text in [(self.letters, LETTERS), (self.segment, SEGMENT)]:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        self.leads = os.path.join(self.work, "leads.csv")
        self.write_leads([HEADER, lead("a@yfirm.ru", "47.91"), lead("b@plain.ru", "47.91"), lead("c@mail.ru", "47.91"),
                          lead("d@gmail.com", "47.91")])
        self.logs = []
        self.nowhere = os.path.join(self.dir.name, "нет.env")

    def write_leads(self, rows):
        with open(self.leads, "w", encoding="utf-8-sig", newline="") as fh:
            csv.writer(fh, delimiter=";").writerows(rows)

    def go(self, *extra, dns=None, **kw):
        argv = [self.leads, "--letters", self.letters, "--segment", self.segment, "--env", self.nowhere,
                "--link", LINK, "--signature", SIGNATURE, "--min-unique", "0", "--min-unique-subject", "0", *extra]
        if dns is not None:
            kw["mx_lookup"] = dns
        return compose.main(argv, log=self.logs.append, **kw)

    def output(self):
        return "\n".join(self.logs)

    def addresses(self):
        letters, _ = lettertable.read_letters(os.path.join(self.work, "letters_1.csv"))
        return sorted(letter.email for letter in letters)

    def cache(self):
        with open(os.path.join(self.work, mx.CACHE_NAME), encoding="utf-8") as fh:
            return json.load(fh)["domains"]

    # ---- первый этап ----

    def test_an_address_on_yandex_servers_is_skipped_with_the_reason_and_counted(self):
        dns = FakeDns(yfirm_ru=["mx.yandex.ru"])
        self.assertEqual(self.go(dns=dns), 0)
        self.assertEqual(self.addresses(), ["b@plain.ru", "c@mail.ru", "d@gmail.com"])
        self.assertIn(f"пропущено: {YANDEX_LINE} - 1", self.output())
        self.assertIn("проверено MX доменов: 2, на Яндексе: 1", self.output())
        self.assertNotIn("a@yfirm.ru", self.output())

    def test_mail_ru_group_and_other_big_public_mailboxes_are_not_looked_up(self):
        public = ["mail.ru", "inbox.ru", "list.ru", "bk.ru", "gmail.com"]
        self.write_leads([HEADER] + [lead(f"p{n}@{domain}", "47.91") for n, domain in enumerate(public)]
                         + [lead("own@plain.ru", "47.91")])
        dns = FakeDns()
        self.assertEqual(self.go(dns=dns), 0)
        self.assertEqual(dns.calls, ["plain.ru"])
        self.assertEqual(len(self.addresses()), 6)
        self.assertIn("проверено MX доменов: 1, на Яндексе: 0", self.output())

    def test_public_yandex_domains_stay_with_the_stoplist(self):
        # решение владельца про @yandex.ru с датой («до 2026-10-16») лежит в стоп-листе; MX-проверка его не отменяет и не заменяет
        self.write_leads([HEADER, lead("y@yandex.ru", "47.91")])
        dns = FakeDns()
        self.assertEqual(self.go(dns=dns), 0)
        self.assertEqual(dns.calls, [])
        self.assertEqual(self.addresses(), ["y@yandex.ru"])

    def test_the_answer_is_cached_in_the_data_folder_for_a_day(self):
        dns = FakeDns(yfirm_ru=["mx.yandex.net"])
        self.go(dns=dns)
        self.assertEqual(sorted(self.cache()), ["plain.ru", "yfirm.ru"])
        self.assertEqual(sorted(dns.calls), ["plain.ru", "yfirm.ru"])
        self.logs.clear()
        again = FakeDns()
        self.assertEqual(self.go(dns=again), 0)
        self.assertEqual(again.calls, [])                                    # всё по кешу
        self.assertIn("проверено MX доменов: 2, на Яндексе: 1", self.output())
        self.assertEqual(self.addresses(), ["b@plain.ru", "c@mail.ru", "d@gmail.com"])
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        cache = self.cache()
        for entry in cache.values():
            entry["time"] = old
        with open(os.path.join(self.work, mx.CACHE_NAME), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "domains": cache}, fh)
        stale = FakeDns(yfirm_ru=["mx.yandex.net"])
        self.go(dns=stale)
        self.assertEqual(sorted(stale.calls), ["plain.ru", "yfirm.ru"])      # сутки вышли: спросили снова

    def test_a_dns_failure_neither_crashes_nor_sends(self):
        dns = FakeDns(yfirm_ru=mx.MxError("DNS не ответил (таймаут)"), plain_ru=mx.MxError("DNS не ответил (таймаут)"))
        self.assertEqual(self.go(dns=dns), 0)
        self.assertEqual(self.addresses(), ["c@mail.ru", "d@gmail.com"])    # не проверенные домены не пишем
        self.assertIn(f"пропущено: {compose.MX_UNCHECKED_REASON} - 2", self.output())
        self.assertIn("MX не удалось проверить у доменов: 2 (DNS не ответил (таймаут))", self.output())
        self.assertIn("проверено MX доменов: 0, на Яндексе: 0", self.output())
        self.assertFalse(os.path.exists(os.path.join(self.work, mx.CACHE_NAME)))       # сбой в кеш не пишется

    def test_a_domain_that_failed_is_checked_again_on_the_next_run(self):
        self.go(dns=FakeDns(plain_ru=mx.MxError("таймаут")))
        self.logs.clear()
        dns = FakeDns()
        self.assertEqual(self.go(dns=dns), 0)
        self.assertEqual(self.addresses(), ["a@yfirm.ru", "b@plain.ru", "c@mail.ru", "d@gmail.com"])
        self.assertNotIn("внимание: MX", self.output())

    def test_dns_that_is_down_is_not_waited_for_on_every_domain(self):
        self.write_leads([HEADER] + [lead(f"a@firm{n}.ru", "47.91") for n in range(8)])
        dns = FakeDns(**{f"firm{n}_ru": mx.MxError("таймаут") for n in range(8)})
        self.assertEqual(self.go(dns=dns), 0)
        self.assertEqual(len(dns.calls), mx.MAX_FAILURES)
        self.assertIn(f"пропущено: {compose.MX_UNCHECKED_REASON} - 8", self.output())
        self.assertIn("Писать некому", self.output())

    def test_no_mx_check_switch_does_not_ask_dns_at_all(self):
        dns = FakeDns(yfirm_ru=["mx.yandex.ru"])
        self.assertEqual(self.go("--no-mx-check", dns=dns), 0)
        self.assertEqual(dns.calls, [])
        self.assertEqual(len(self.addresses()), 4)
        self.assertIn("проверка MX выключена", self.output())
        self.assertNotIn("проверено MX доменов", self.output())

    def test_check_mx_off_in_the_environment_is_the_same(self):
        self.assertEqual(os.environ.get("CHECK_MX"), "0")                    # так в тестах (tests/__init__.py)
        self.assertEqual(self.go(), 0)                                       # настоящий DNS не нужен
        self.assertEqual(len(self.addresses()), 4)
        self.assertIn("проверка MX выключена", self.output())

    def test_the_check_is_on_by_default_and_uses_the_real_dns_function(self):
        dns = FakeDns(yfirm_ru=["mx.yandex.ru"])
        with mock.patch.dict(os.environ, {"CHECK_MX": ""}), mock.patch.object(mx, "make_lookup", return_value=dns) as make:
            self.assertEqual(self.go(), 0)
        make.assert_called_once_with()
        self.assertEqual(self.addresses(), ["b@plain.ru", "c@mail.ru", "d@gmail.com"])

    def test_test_letters_to_own_mailboxes_are_not_checked(self):
        dns = FakeDns()
        self.assertEqual(self.go("--test-to", "me@own-domain.ru", dns=dns), 0)
        self.assertEqual(dns.calls, [])
        self.assertNotIn("MX", self.output())

    def test_after_the_batch_is_full_no_more_lookups(self):
        self.write_leads([HEADER] + [lead(f"a@firm{n}.ru", "47.91") for n in range(6)])
        dns = FakeDns()
        self.assertEqual(self.go("--limit", "2", dns=dns), 0)
        self.assertEqual(len(dns.calls), 2)
        self.assertEqual(len(self.addresses()), 2)

    def test_two_addresses_of_one_yandex_domain_ask_dns_once(self):
        self.write_leads([HEADER, lead("a@yfirm.ru", "47.91"), lead("b@yfirm.ru", "47.91")])
        dns = FakeDns(yfirm_ru=["mx.yandex.ru"])
        self.go(dns=dns)
        self.assertEqual(dns.calls, ["yfirm.ru"])
        self.assertIn(f"пропущено: {YANDEX_LINE} - 2", self.output())
        self.assertIn("проверено MX доменов: 1, на Яндексе: 1", self.output())

    # ---- этапы 2 и 3 ----

    def test_follow_up_letters_skip_yandex_domains_too(self):
        monday = date(2026, 10, 5)
        journal.save_state(compose.journal_path(self.work, 1),
                           dict([sent("a@yfirm.ru", monday), sent("b@plain.ru", monday), sent("c@mail.ru", monday)]))
        composer = make_composer()
        dns = FakeDns(yfirm_ru=["mx.yandex.ru"])
        checker = mx.MxChecker(self.work, dns)
        result = compose.compose_next(composer, 2, self.work, frozenset(), frozenset(), monday + timedelta(days=7), checker=checker)
        self.assertEqual(sorted(letter[0] for letter in result.letters), ["b@plain.ru", "c@mail.ru"])
        self.assertEqual(result.skipped[YANDEX_LINE], 1)
        self.assertEqual(sorted(dns.calls), ["plain.ru", "yfirm.ru"])

    def test_stage_two_through_the_command_line_prints_the_report_line(self):
        monday = date(2026, 10, 5)
        journal.save_state(compose.journal_path(self.work, 1), dict([sent("a@yfirm.ru", monday), sent("b@plain.ru", monday)]))
        dns = FakeDns(yfirm_ru=["mx.yandex.ru"])
        self.assertEqual(self.go("--stage", "2", dns=dns, today=monday + timedelta(days=7)), 0)
        self.assertIn("проверено MX доменов: 2, на Яндексе: 1", self.output())
        letters, _ = lettertable.read_letters(os.path.join(self.work, "letters_2.csv"))
        self.assertEqual([letter.email for letter in letters], ["b@plain.ru"])


if __name__ == "__main__":
    unittest.main()
