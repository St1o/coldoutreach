import unittest
from datetime import date

import requests

from checko_updates import (
    CheckoBlocked, CheckoClient, CheckoError, PastedPages, items_from_text, page_url_template, parse_page,
)
from egrul_inn_search import validate_inn


def make_inn(prefix9: str) -> str:
    return next(prefix9 + str(d) for d in range(10) if validate_inn(prefix9 + str(d)))


INNS = [make_inn(f"77070{n:04d}") for n in range(10, 20)]


def item(n, inn, day="9 октября 2026 года", name=None):
    ogrn = f"1269600{n:06d}"
    name = name or f'ООО "ФИРМА {n}"'
    return (f'<li><span>{n}.</span> <a href="/company/firma-{ogrn}">{name}</a>'
            f'<div>620042, Свердловская область, г. Екатеринбург, ул. Избирателей, д. {n}</div>'
            f'<div><span>Дата регистрации</span> {day} <span>ОГРН</span> {ogrn} '
            f'<span>ИНН</span> {inn}</div></li>')


def page(items, pager='<a href="/company/updates?page=2">2</a>'):
    return ('<html><head><script>var s = "Дата регистрации 1 января 2000 года ОГРН 1000000000000 ИНН '
            f'{INNS[0]}";</script></head><body><a href="/company/updates">Организации</a>'
            f'<h1>Новые организации</h1><ol>{"".join(items)}</ol>'
            f'<nav><a href="/company/updates">1</a> {pager}</nav></body></html>')


class FakeResponse:
    def __init__(self, html="", status=200):
        self.content, self.status_code = html.encode("utf-8"), status


class FakeSession:
    def __init__(self, pages):
        self.headers = {}
        self.pages = pages          # адрес -> ответ
        self.requested = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        response = self.pages[url]
        if isinstance(response, Exception):
            raise response
        return response


class ParsePageTest(unittest.TestCase):
    def test_items_dates_and_names(self):
        items, _ = parse_page(page([item(1, INNS[1]), item(2, INNS[2], day="8 октября 2026 года")]))
        self.assertEqual([i.inn for i in items], [INNS[1], INNS[2]])
        self.assertEqual([i.reg_date for i in items], [date(2026, 10, 9), date(2026, 10, 8)])
        self.assertEqual(items[0].ogrn, "1269600000001")
        self.assertEqual([i.name for i in items], ['ООО "ФИРМА 1"', 'ООО "ФИРМА 2"'])

    def test_script_text_is_ignored(self):
        items, _ = parse_page(page([item(1, INNS[1])]))
        self.assertNotIn(date(2000, 1, 1), [i.reg_date for i in items])

    def test_invalid_inn_and_bad_date_are_skipped(self):
        bad_inn = item(1, "7707001011")
        bad_date = item(2, INNS[2], day="31 февраля 2026 года")
        bad_month = item(3, INNS[3], day="9 октябрьря 2026 года")
        items, _ = parse_page(page([bad_inn, bad_date, bad_month, item(4, INNS[4])]))
        self.assertEqual([i.inn for i in items], [INNS[4]])

    def test_twelve_digit_inn(self):
        items, _ = parse_page(page([item(1, "500100732259")]))
        self.assertEqual([i.inn for i in items], ["500100732259"])

    def test_names_left_empty_when_links_do_not_match_rows(self):
        html = page([item(1, INNS[1])]).replace("</ol>", '<li><a href="/company/extra-1">Лишняя</a></li></ol>')
        items, _ = parse_page(html)
        self.assertEqual(items[0].name, "")

    def test_works_without_markup(self):
        text = f"Дата регистрации 9 октября 2026 года ОГРН 1269600032888 ИНН {INNS[1]}"
        self.assertEqual(len(parse_page(text)[0]), 1)

    def test_pager_links(self):
        _, pager = parse_page(page([item(1, INNS[1])]))
        self.assertIn(("/company/updates?page=2", "2"), pager)


class PageUrlTemplateTest(unittest.TestCase):
    def test_query_parameter(self):
        self.assertEqual(page_url_template([("/company/updates?page=2", "2")], "https://checko.ru/company/updates"),
                         "https://checko.ru/company/updates?page={n}")

    def test_path_segment(self):
        self.assertEqual(page_url_template([("/company/updates/2", "2")], "https://checko.ru/company/updates"),
                         "https://checko.ru/company/updates/{n}")

    def test_missing(self):
        self.assertIsNone(page_url_template([("/x", "5")], "https://checko.ru/company/updates"))


BASE = "https://checko.ru/company/updates"


def make_client(pages):
    session = FakeSession(pages)
    return CheckoClient(session), session


class NewCompaniesTest(unittest.TestCase):
    def three_pages(self):
        return {
            BASE: FakeResponse(page([item(1, INNS[1]), item(2, INNS[2])])),
            BASE + "?page=2": FakeResponse(page(
                [item(3, INNS[3]), item(4, INNS[4], day="8 октября 2026 года")])),
            BASE + "?page=3": FakeResponse(page([item(5, INNS[5], day="8 октября 2026 года")])),
            BASE + "?page=4": FakeResponse(page([item(6, INNS[6], day="7 октября 2026 года")])),
        }

    def test_collects_the_day_and_stops_after_older_pages(self):
        client, session = make_client(self.three_pages())
        found = client.new_companies(date(2026, 10, 9), pause=0)
        self.assertEqual([c.inn for c in found], [INNS[1], INNS[2], INNS[3]])
        # страница 3 уже целиком старая - на ней останавливаемся, страницу 4 не трогаем
        self.assertEqual(session.requested, [BASE, BASE + "?page=2", BASE + "?page=3"])

    def test_yesterday_skips_newer_items_and_continues(self):
        client, _ = make_client(self.three_pages())
        found = client.new_companies(date(2026, 10, 8), pause=0)
        self.assertEqual([c.inn for c in found], [INNS[4], INNS[5]])

    def test_duplicates_across_pages_are_removed(self):
        pages = self.three_pages()
        pages[BASE + "?page=2"] = FakeResponse(page([item(1, INNS[1]), item(3, INNS[3])]))
        pages[BASE + "?page=3"] = FakeResponse(page([item(5, INNS[5], day="8 октября 2026 года")]))
        found = make_client(pages)[0].new_companies(date(2026, 10, 9), pause=0)
        self.assertEqual([c.inn for c in found], [INNS[1], INNS[2], INNS[3]])

    def test_max_pages(self):
        pages = {BASE: FakeResponse(page([item(1, INNS[1])])),
                 BASE + "?page=2": FakeResponse(page([item(2, INNS[2])]))}
        client, session = make_client(pages)
        found = client.new_companies(date(2026, 10, 9), max_pages=1, pause=0)
        self.assertEqual(len(found), 1)
        self.assertEqual(session.requested, [BASE])

    def test_uses_pager_url_pattern(self):
        pages = {
            BASE: FakeResponse(page([item(1, INNS[1])], pager='<a href="/company/updates/p/2/">2</a>')),
            "https://checko.ru/company/updates/p/2/": FakeResponse(
                page([item(2, INNS[2], day="8 октября 2026 года")])),
        }
        client, session = make_client(pages)
        client.new_companies(date(2026, 10, 9), pause=0)
        self.assertEqual(session.requested[1], "https://checko.ru/company/updates/p/2/")

    def test_blocked(self):
        for status in (403, 429):
            client, _ = make_client({BASE: FakeResponse(status=status)})
            with self.assertRaises(CheckoBlocked):
                client.new_companies(date(2026, 10, 9), pause=0)

    def test_empty_first_page_is_an_error(self):
        client, _ = make_client({BASE: FakeResponse("<html>Проверка, что вы не робот</html>")})
        with self.assertRaises(CheckoError):
            client.new_companies(date(2026, 10, 9), pause=0)

    def test_network_error(self):
        client, _ = make_client({BASE: requests.ConnectionError("нет сети")})
        with self.assertRaises(CheckoError):
            client.new_companies(date(2026, 10, 9), pause=0)


PASTED = (
    "Новые организации\nОрганизации с 1 по 100 из 2000\n"
    "1. ООО <<ТАТЬЯНА>> & Ко\n624992, Свердловская область, д. 23\n"
    "Дата регистрации 9 октября 2026 года ОГРН 1269600032877 ИНН {a}\n"
    "2.\tООО \"ФИРМА\"\n620042, г. Екатеринбург\n"
    "Дата регистрации\t9 октября 2026 года\tОГРН 1269600032888\tИНН {b}\n"
    "3. ООО \"СТАРАЯ\"\nДата регистрации 8 октября 2026 года ОГРН 1269600032855 ИНН {c}\n"
    "4. ООО \"ДУБЛЬ\"\nДата регистрации 9 октября 2026 года ОГРН 1269600032888 ИНН {b}\n"
).format(a=INNS[1], b=INNS[2], c=INNS[3])


class PastedPagesTest(unittest.TestCase):
    def test_plain_text_with_angle_brackets_in_names(self):
        items = items_from_text(PASTED)
        self.assertEqual([i.inn for i in items], [INNS[1], INNS[2], INNS[3], INNS[2]])

    def test_names_are_taken_from_the_pasted_text(self):
        found = PastedPages(PASTED).new_companies(date(2026, 10, 9))
        self.assertEqual([c.name for c in found], ["ООО <<ТАТЬЯНА>> & Ко", 'ООО "ФИРМА"'])

    def test_number_on_its_own_line_and_no_header_text_in_the_name(self):
        text = ("Новые организации\nОрганизации с 1 по 100 из 2000\n"
                f"1.\nООО \"ПЕРВАЯ\"\n620042, г. Екатеринбург, д. 2.\nДата регистрации 9 октября 2026 года ОГРН 1269600032877 ИНН {INNS[1]}\n"
                f"2.\nАО ВТОРАЯ\nДата регистрации 9 октября 2026 года ОГРН 1269600032888 ИНН {INNS[2]}\n")
        found = PastedPages(text).new_companies(date(2026, 10, 9))
        self.assertEqual([c.name for c in found], ['ООО "ПЕРВАЯ"', "АО ВТОРАЯ"])

    def test_name_is_left_empty_when_it_cannot_be_found(self):
        text = (f"Организации\nООО БЕЗ НОМЕРА\nДата регистрации 9 октября 2026 года ОГРН 1269600032877 ИНН {INNS[1]}\n")
        self.assertEqual([c.name for c in PastedPages(text).new_companies(date(2026, 10, 9))], [""])

    def test_filters_by_date_and_removes_duplicates(self):
        found = PastedPages(PASTED).new_companies(date(2026, 10, 9))
        self.assertEqual([c.inn for c in found], [INNS[1], INNS[2]])
        self.assertEqual([c.inn for c in PastedPages(PASTED).new_companies(date(2026, 10, 8))], [INNS[3]])

    def test_several_pages_pasted_one_after_another(self):
        text = PASTED + "\n" + PASTED.replace(INNS[1], INNS[4]).replace("9 октября", "8 октября")
        found = PastedPages(text).new_companies(date(2026, 10, 9))
        self.assertEqual([c.inn for c in found], [INNS[1], INNS[2]])

    def test_nothing_for_the_day(self):
        self.assertEqual(PastedPages(PASTED).new_companies(date(2026, 10, 1)), [])
        self.assertEqual(PastedPages("").new_companies(date(2026, 10, 9)), [])
        self.assertEqual(PastedPages("просто текст без организаций").new_companies(date(2026, 10, 9)), [])


if __name__ == "__main__":
    unittest.main()
