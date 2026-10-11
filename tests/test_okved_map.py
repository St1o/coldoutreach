"""Карта ОКВЭД от агента: решение по коду главнее таблицы классов; список кодов без решения уходит агенту."""
import csv
import os
import tempfile
import unittest

import compose
from compose import ComposeError
from mailing.agentmd import EXCLUDE_ID, okved_digits, parse_okved_map, parse_segment
from tests.test_compose import HEADER, LETTERS, LINK, SEGMENT, SIGNATURE, lead

# Группа 13 без списка классов: в неё компании попадают только по карте ОКВЭД.
SEGMENT_WITH_MAP_ONLY_GROUP = SEGMENT.replace(
    "| 12. Запасной", "| 13. Интернет-торговля | написано |\n| 12. Запасной")

MAP = """# Карта ОКВЭД

Заметка агента. | 99.99 | не таблица карты | 1 |

## Карта

| Код | Название | Группа | Почему |
|---|---|---|---|
| 47.91 | Торговля через интернет | 13. Интернет-торговля | заказы приходят с сайта |
| 47.91.2 | Розничная торговля через интернет | 13 | то же |
| **60.10** | Радиовещание | 12 | запасной |
| 94.99.55 | Молодёжная политика | не писать | бюджетное учреждение |
| 68.20 | Аренда недвижимости | Услуги для бизнеса: консалтинг, маркетинг | по смыслу |
| 1.11 | Выращивание зерновых | 11 | |

## Заметки
| 46.73 | Эта строка вне раздела «Карта» и не читается | 3 |
"""


def groups():
    return parse_segment(SEGMENT_WITH_MAP_ONLY_GROUP)


class DigitsTest(unittest.TestCase):
    def test_codes_become_digit_strings_with_the_leading_zero(self):
        for raw, expected in [("47.91.2", "47912"), ("47.91", "4791"), ("47", "47"), ("1.11", "0111"), ("01,11", "0111"),
                              (" 94.99.55 ", "949955"), ("", ""), ("abc", ""), ("47.91.2.1", ""), ("470", "")]:
            self.assertEqual(okved_digits(raw), expected, raw)


class ParseMapTest(unittest.TestCase):
    def setUp(self):
        self.mapping = parse_okved_map(MAP, groups())

    def test_groups_by_number_name_fallback_and_exclusion(self):
        self.assertEqual(self.mapping["4791"], "13")
        self.assertEqual(self.mapping["47912"], "13")
        self.assertEqual(self.mapping["6010"], "12")                 # код в «**» тоже читается
        self.assertEqual(self.mapping["949955"], EXCLUDE_ID)
        self.assertEqual(self.mapping["6820"], "6")                  # группа названа словами
        self.assertEqual(self.mapping["0111"], "11")                 # ведущий ноль восстановлен

    def test_only_the_map_section_is_read_when_there_is_one(self):
        self.assertNotIn("4673", self.mapping)
        self.assertNotIn("9999", self.mapping)

    def test_without_a_map_section_the_whole_file_is_read(self):
        mapping = parse_okved_map("| 46.73 | Опт | 3 |\n", groups())
        self.assertEqual(mapping, {"4673": "3"})

    def test_a_group_that_is_not_in_the_segment_table_is_an_error(self):
        with self.assertRaises(ComposeError) as caught:
            parse_okved_map("## Карта\n| 46.73 | Опт | 77 |\n", groups())
        self.assertIn("77", str(caught.exception))
        with self.assertRaises(ComposeError):
            parse_okved_map("## Карта\n| 46.73 | Опт | что-то непонятное |\n", groups())

    def test_one_code_with_two_different_groups_is_an_error(self):
        with self.assertRaises(ComposeError):
            parse_okved_map("## Карта\n| 46.73 | Опт | 1 |\n| 46.73 | Опт | 3 |\n", groups())
        self.assertEqual(parse_okved_map("## Карта\n| 46.73 | Опт | 1 |\n| 46.73 | Опт | 1 |\n", groups()), {"4673": "1"})


class MapOnlyGroupTest(unittest.TestCase):
    def test_a_group_without_classes_is_kept_and_never_chosen_by_the_class_table(self):
        found = {group.id: group for group in groups()}
        self.assertEqual(found["13"].name, "Интернет-торговля")
        self.assertEqual(found["13"].classes, frozenset())
        composer = compose.Composer(groups(), compose.parse_letters(LETTERS), LINK, SIGNATURE)
        self.assertNotEqual(composer.group_for("47.91"), "13")


class ComposerDecisionTest(unittest.TestCase):
    def setUp(self):
        self.composer = compose.Composer(groups(), compose.parse_letters(LETTERS), LINK, SIGNATURE,
                                         okved_map=parse_okved_map(MAP, groups()))

    def test_the_agent_decision_beats_the_class_table_and_the_longest_code_wins(self):
        c = self.composer
        self.assertEqual(c.group_for("47.91.2"), "13")           # по классу 47 это была бы «Торговля» (1)
        self.assertEqual(c.group_for("47.91.19"), "13")          # подкод короткого решения «47.91»
        self.assertEqual(c.group_for("47.11"), "1")              # решения нет: таблица классов
        self.assertEqual(c.group_for("68.20.1"), "6")
        self.assertEqual(c.group_for("1.11"), "11")              # Excel потерял ноль
        self.assertEqual(c.group_for("94.99.55"), EXCLUDE_ID)

    def test_no_decision_means_none_and_the_class_table_decides(self):
        self.assertIsNone(self.composer.decision("46.73"))
        self.assertEqual(self.composer.group_for("46.73"), "1")
        self.assertIsNone(self.composer.decision("не код"))
        self.assertEqual(self.composer.group_for("не код"), "12")


class FirstLetterWithMapTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.composer = compose.Composer(groups(), compose.parse_letters(LETTERS), LINK, SIGNATURE,
                                         include_unwritten=True, okved_map=parse_okved_map(MAP, groups()))

    def first(self, *rows):
        leads = [{"email": r[2], "okved": r[3], "okved_name": r[4], "source": r[5]} for r in rows]
        return compose.compose_first(self.composer, leads, "Тема", self.dir.name, frozenset(), frozenset())

    def test_letters_follow_the_map_exclusions_are_skipped_and_unknown_codes_are_collected(self):
        row = lambda email, code, name="": [x for x in lead(email, code)[:4]] + [name, "ЕГРЮЛ"]
        result = self.first(row("a@x.ru", "47.91.2", "Торговля через интернет"), row("b@x.ru", "94.99.55"),
                            row("c@x.ru", "46.73", "Опт. стройматериалы"), row("d@x.ru", "46.73", "Опт. стройматериалы"),
                            row("e@x.ru", "", ""))
        self.assertEqual({letter[0]: letter[3] for letter in result.letters},
                         {"a@x.ru": "13", "c@x.ru": "1", "d@x.ru": "1", "e@x.ru": "12"})
        self.assertEqual(result.skipped["агент решил не писать этому виду деятельности (карта ОКВЭД)"], 1)
        self.assertEqual(result.by_map, 2)                                       # 47.91.2 и 94.99.55
        self.assertEqual(result.unmapped, {("46.73", "Опт. стройматериалы"): 2, ("", ""): 1})

    def test_an_excluded_company_is_not_written_to_under_a_duplicate_address_either(self):
        row = lambda code: lead("x@x.ru", code)
        result = self.first(row("94.99.55"), row("47.11"))
        self.assertEqual(result.letters, [])


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.composer = compose.Composer(groups(), compose.parse_letters(LETTERS), LINK, SIGNATURE)

    def test_the_report_lists_codes_names_and_counts_but_no_addresses_or_company_names(self):
        path = compose.write_okved_report(self.dir.name, self.composer, compose.Counter(
            {("46.73", "Опт. стройматериалы"): 2, ("68.20", "Аренда | недвижимости"): 1, ("", ""): 1}))
        self.assertEqual(path, os.path.join(self.dir.name, "okved_to_classify.md"))
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("| 46.73 | Опт. стройматериалы | 2 | 1. Торговля |", text)
        self.assertIn("Аренда / недвижимости", text)                              # «|» не ломает таблицу
        self.assertIn("(название не указано)", text)
        for secret in ("@", "СЕКРЕТНАЯ", "Иванов"):
            self.assertNotIn(secret, text)
        self.assertFalse(os.path.exists(path + ".part"))

    def test_nothing_to_classify_removes_the_old_report(self):
        path = os.path.join(self.dir.name, "okved_to_classify.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("старый список")
        self.assertEqual(compose.write_okved_report(self.dir.name, self.composer, compose.Counter()), "")
        self.assertFalse(os.path.exists(path))


class MainWithMapTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.work = os.path.join(self.dir.name, "data")
        self.agents = os.path.join(self.dir.name, "agents", "Рассылка")
        os.makedirs(self.work)
        os.makedirs(os.path.join(self.agents, "01-Segment", "output"))
        os.makedirs(os.path.join(self.agents, "02-Write", "output"))
        open(os.path.join(self.dir.name, "agents", "CLAUDE.md"), "w").close()
        self.segment = os.path.join(self.agents, "01-Segment", "output", "segment.md")
        self.letters = os.path.join(self.agents, "02-Write", "output", "letters.md")
        self.map = os.path.join(self.agents, "01-Segment", "output", "okved-map.md")
        for path, text in [(self.segment, SEGMENT_WITH_MAP_ONLY_GROUP), (self.letters, LETTERS)]:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        self.leads = os.path.join(self.work, "leads.csv")
        with open(self.leads, "w", encoding="utf-8-sig", newline="") as fh:
            csv.writer(fh, delimiter=";").writerows([
                HEADER, lead("a@x.ru", "47.91.2"), lead("b@x.ru", "94.99.55"), lead("c@x.ru", "46.73"),
                lead("d@x.ru", "41.20")])
        self.logs = []

    def go(self, *extra):
        argv = [self.leads, "--letters", self.letters, "--segment", self.segment, "--env", os.path.join(self.dir.name, "нет.env"),
                "--link", LINK, "--signature", SIGNATURE, *extra]
        return compose.main(argv, log=self.logs.append)

    def write_map(self, text=MAP):
        with open(self.map, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_without_a_map_everything_goes_by_class_and_the_report_lists_all_codes(self):
        self.assertEqual(self.go(), 0)
        output = "\n".join(self.logs)
        self.assertIn("ОКВЭД без решения агента: кодов 4, компаний 4", output)
        self.assertIn("okved_to_classify.md", output)
        self.assertTrue(os.path.exists(os.path.join(self.work, "okved_to_classify.md")))

    def test_the_map_next_to_the_segment_file_is_picked_up_by_itself(self):
        self.write_map()
        self.assertEqual(self.go("--include-unwritten"), 0)                      # у группы 13 в тестовых письмах нет блока боли
        output = "\n".join(self.logs)
        self.assertIn("запасной вариант текста: для группы 13", output)
        self.assertIn("агент решил не писать этому виду деятельности (карта ОКВЭД) - 1", output)
        self.assertIn("карта ОКВЭД: решение агента есть у 2 из 4 подходящих адресов", output)
        self.assertIn("кодов 2, компаний 2", output)                              # 46.73 и 41.20 без решения
        with open(os.path.join(self.work, "letters_1.csv"), encoding="utf-8-sig", newline="") as fh:
            rows = {row["Почта"]: row["Группа"] for row in csv.DictReader(fh, delimiter=";")}
        self.assertEqual(rows, {"a@x.ru": "13", "c@x.ru": "1", "d@x.ru": "3"})   # b@x.ru агент исключил

    def test_everything_decided_leaves_no_report_and_no_warning(self):
        self.write_map(MAP + "\n## Карта (дополнение)\n| 46.73 | Опт | 1 |\n| 41.20 | Строит. | 3 |\n")
        self.assertEqual(self.go(), 0)
        self.assertNotIn("ОКВЭД без решения агента", "\n".join(self.logs))
        self.assertFalse(os.path.exists(os.path.join(self.work, "okved_to_classify.md")))

    def test_explicit_map_path_beats_the_neighbour_and_a_missing_file_is_a_clear_error(self):
        self.write_map("## Карта\n| 46.73 | Опт | 3 |\n")
        other = os.path.join(self.dir.name, "other-map.md")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("## Карта\n| 46.73 | Опт | 1 |\n")
        self.assertEqual(self.go("--okved-map", other), 0)
        with open(os.path.join(self.work, "letters_1.csv"), encoding="utf-8-sig", newline="") as fh:
            rows = {row["Почта"]: row["Группа"] for row in csv.DictReader(fh, delimiter=";")}
        self.assertEqual(rows["c@x.ru"], "1")
        self.logs.clear()
        self.assertEqual(self.go("--okved-map", os.path.join(self.dir.name, "нет.md")), 1)
        self.assertIn("Ошибка", "\n".join(self.logs))

    def test_a_broken_map_stops_the_build_with_the_reason(self):
        self.write_map("## Карта\n| 46.73 | Опт | 77 |\n")
        self.assertEqual(self.go(), 1)
        self.assertIn("77", "\n".join(self.logs))

    def test_test_letters_and_follow_ups_do_not_touch_the_report(self):
        self.write_map()
        self.assertEqual(self.go("--test-to", "me@x.ru"), 0)
        self.assertFalse(os.path.exists(os.path.join(self.work, "okved_to_classify.md")))


if __name__ == "__main__":
    unittest.main()
