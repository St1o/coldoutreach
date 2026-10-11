"""Варианты фраз в письмах: раздел «Варианты фраз» файла агента, приветствие по времени суток, тема по кругу и
базовая проверка писем на повторы (compose.uniqueness_by_group, ключ --min-unique)."""
import csv
import os
import tempfile
import unittest
from datetime import datetime

import compose
from compose import ComposeError
from mailing import agentmd, journal
from mailing import table as lettertable
from tests.test_compose import HEADER, LINK, SEGMENT, SIGNATURE, lead

LETTERS = """# Письма

## Темы
1. Первая тема (рекомендую)
2. Вторая тема

## Письмо 1. Шаблон

```
{{приветствие}}

{{представление}}

Пишем вам, потому что компания недавно зарегистрирована{{причина_хвост}}.

Вопросы такого характера: {{боль_1}}

{{призыв}}

{{подпись}}
```

## Блоки боли для письма 1

**Торговля**
боль торговли.

**Запасной вариант (группа 12, неизвестный ОКВЭД)**
запасная боль.

## Дожим. Шаблон

```
{{приветствие}}

Это снова студия, вид деятельности — {{группа_словами}}. Ещё вопрос: {{боль_2}}

{{призыв}}

{{подпись}}
```

**Блоки `{{боль_2}}`**

*Торговля:* дожим торговли.

*Запасной вариант:* запасной дожим.

## Закрывающее письмо. Шаблон

```
{{приветствие}} Это последнее письмо. {{призыв}}

{{подпись}}
```

## Варианты фраз

Пояснение, которое программа пропускает.

### приветствие
- утро: Доброе утро!
- день: Добрый день!
- вечер: Добрый вечер!
- Здравствуйте!
- Привет!

### представление
- Это студия «Альфа»: делаем рабочие процессы быстрее.
- Мы из студии «Альфа» и автоматизируем рабочие процессы.
- Студия «Альфа» помогает бизнесу делать больше теми же людьми.

### причина_хвост
- , сфера — {{группа_словами}}
- , вид деятельности — {{группа_словами}}
- , сфера деятельности — {{группа_словами}}

### призыв
- Пройдите опрос, около 5 минут: {{ссылка_на_опрос}}
- Опрос займёт около 5 минут: {{ссылка_на_опрос}}
- Ответьте на короткий опрос (около 5 минут): {{ссылка_на_опрос}}

### тема
- Тема А
- Тема Б
- Тема В
"""


def composer(letters=LETTERS, now=None):
    groups = agentmd.parse_segment(SEGMENT)
    texts = agentmd.parse_letters(letters)
    agentmd.check_texts(texts, groups)
    return compose.Composer(groups, texts, LINK, SIGNATURE, now=now)


class ParseVariantsTest(unittest.TestCase):
    def test_slots_variants_and_time_tags_are_read(self):
        variants = agentmd.parse_letters(LETTERS).variants
        self.assertEqual(list(variants), ["приветствие", "представление", "причина_хвост", "призыв", "тема"])
        self.assertEqual(variants["приветствие"][:2], [("утро", "Доброе утро!"), ("день", "Добрый день!")])
        self.assertEqual(variants["приветствие"][3], ("", "Здравствуйте!"))
        self.assertEqual(len(variants["представление"]), 3)

    def test_explanations_outside_the_lists_are_skipped(self):
        for slot, items in agentmd.parse_letters(LETTERS).variants.items():
            self.assertNotIn("Пояснение", " ".join(text for _, text in items), slot)

    def test_a_file_without_the_section_has_no_variants(self):
        text = LETTERS.split("## Варианты фраз")[0]
        self.assertEqual(agentmd.parse_letters(text).variants, {})


class CheckVariantsTest(unittest.TestCase):
    def test_the_example_is_valid(self):
        compose.check_variants(agentmd.parse_letters(LETTERS).variants)

    def test_every_time_of_day_needs_a_variant(self):
        with self.assertRaises(ComposeError) as ctx:
            compose.check_variants({"приветствие": [("утро", "Доброе утро!"), ("день", "Добрый день!")]})
        self.assertIn("вечер", str(ctx.exception))

    def test_an_empty_slot_is_an_error(self):
        with self.assertRaises(ComposeError):
            compose.check_variants({"призыв": []})

    def test_a_slot_cannot_take_the_name_of_a_plain_placeholder(self):
        with self.assertRaises(ComposeError):
            compose.check_variants({"подпись": [("", "текст")]})

    def test_a_variant_cannot_hold_another_slot(self):
        with self.assertRaises(ComposeError) as ctx:
            compose.check_variants({"призыв": [("", "{{приветствие}} Опрос")]})
        self.assertIn("приветствие", str(ctx.exception))


class DayPeriodTest(unittest.TestCase):
    def test_hours_map_to_periods(self):
        for hour, period in [(4, "вечер"), (5, "утро"), (10, "утро"), (11, "день"), (16, "день"), (17, "вечер"), (23, "вечер")]:
            self.assertEqual(compose.day_period(hour), period, hour)


class PickingTest(unittest.TestCase):
    def greetings(self, hour, count=12):
        comp = composer(now=datetime(2026, 10, 12, hour, 0))
        return {comp.letter_text(1, "1").split("\n")[0] for _ in range(count)}

    def test_the_greeting_follows_the_time_of_day(self):
        morning, day, evening = self.greetings(9), self.greetings(14), self.greetings(20)
        self.assertEqual(morning, {"Доброе утро!", "Здравствуйте!", "Привет!"})
        self.assertEqual(day, {"Добрый день!", "Здравствуйте!", "Привет!"})
        self.assertEqual(evening, {"Добрый вечер!", "Здравствуйте!", "Привет!"})

    def test_neighbouring_letters_get_different_variants(self):
        comp = composer(now=datetime(2026, 10, 12, 14, 0))
        texts = [comp.letter_text(1, "1") for _ in range(3)]
        for slot_first_words in ("Это студия", "Мы из студии", "Студия «Альфа»"):
            self.assertEqual(sum(slot_first_words in text for text in texts), 1, slot_first_words)

    def test_the_same_composer_state_gives_the_same_letters(self):
        first = composer(now=datetime(2026, 10, 12, 14, 0))
        second = composer(now=datetime(2026, 10, 12, 14, 0))
        self.assertEqual([first.letter_text(1, "1") for _ in range(4)], [second.letter_text(1, "1") for _ in range(4)])

    def test_groups_and_stages_rotate_separately(self):
        comp = composer(now=datetime(2026, 10, 12, 14, 0))
        a = comp.letter_text(1, "1")
        b = comp.letter_text(1, "12")
        self.assertEqual(a.split("\n")[2], b.split("\n")[2])        # у каждой группы счёт начинается заново

    def test_the_subject_rotates_when_the_file_has_the_slot(self):
        comp = composer(now=datetime(2026, 10, 12, 14, 0))
        subjects = []
        for _ in range(3):
            subjects.append(comp.subject_variant("1"))
            comp.letter_text(1, "1")
        self.assertEqual(sorted(subjects), ["Тема А", "Тема Б", "Тема В"])

    def test_without_the_slot_the_subject_stays_the_recommended_one(self):
        comp = composer(LETTERS.replace("### тема", "### не_тема"))
        self.assertIsNone(comp.subject_variant("1"))


class FillingTest(unittest.TestCase):
    def test_slots_are_replaced_and_the_clause_for_the_fallback_group_is_removed(self):
        comp = composer(now=datetime(2026, 10, 12, 14, 0))
        for _ in range(6):
            text = comp.letter_text(1, "12")
            self.assertNotIn("{{", text)
            self.assertNotIn("вид деятельности", text)
            self.assertNotIn("сфера деятельности", text)
            self.assertNotIn("сфера", text)
            self.assertEqual(text.count("docs.google.com/forms/"), 1)

    def test_the_words_of_a_group_come_into_the_variants(self):
        comp = composer(now=datetime(2026, 10, 12, 14, 0))
        texts = [comp.letter_text(1, "1") for _ in range(3)]
        self.assertTrue(all("торговля" in text for text in texts))

    def test_an_unknown_placeholder_in_the_template_is_still_an_error(self):
        with self.assertRaises(ComposeError):
            composer(LETTERS.replace("{{призыв}}", "{{нет_такого}}", 1)).letter_text(1, "1")

    def test_the_signature_in_a_variant_still_requires_the_signature(self):
        text = LETTERS.replace("{{подпись}}", "").replace("- Опрос займёт", "- {{подпись}} Опрос займёт")
        self.assertTrue(compose.uses_placeholder(agentmd.parse_letters(text), "подпись"))
        with self.assertRaises(ComposeError):
            compose.Composer(agentmd.parse_segment(SEGMENT), agentmd.parse_letters(text), LINK, "")


class UniquenessTest(unittest.TestCase):
    def letters(self, texts, group="1"):
        return [(f"a{i}@x.ru", "Тема", text, group, "ЕГРЮЛ") for i, text in enumerate(texts)]

    def test_identical_letters_have_no_difference(self):
        text = "Раз два три четыре пять шесть семь восемь девять десять"
        report = compose.uniqueness_by_group(self.letters([text, text, text]))
        self.assertEqual(report["1"][:2], (0.0, 0.0))

    def test_different_letters_have_a_big_difference(self):
        report = compose.uniqueness_by_group(self.letters(["один два три четыре пять шесть",
                                                           "семь восемь девять десять одиннадцать двенадцать"]))
        self.assertEqual(report["1"][0], 100.0)

    def test_the_link_is_not_compared(self):
        a = "Привет мир как дела сегодня https://docs.google.com/forms/d/e/AAA/viewform"
        b = "Привет мир как дела сегодня https://docs.google.com/forms/d/e/BBB/viewform"
        self.assertEqual(compose.uniqueness_by_group(self.letters([a, b]))["1"][0], 0.0)

    def test_a_group_of_one_letter_is_skipped(self):
        self.assertEqual(compose.uniqueness_by_group(self.letters(["один два три четыре"])), {})

    def test_groups_are_compared_separately(self):
        text = "Раз два три четыре пять шесть семь"
        letters = self.letters([text], "1") + self.letters(["совсем другой текст про другое дело"], "3")
        self.assertEqual(compose.uniqueness_by_group(letters), {})

    def test_many_letters_are_sampled_the_same_way_each_time(self):
        texts = [f"слово{i % 7} слово{i % 5} общее общее общее слово{i % 3} конец" for i in range(80)]
        first = compose.uniqueness_by_group(self.letters(texts), max_pairs=50)
        second = compose.uniqueness_by_group(self.letters(texts), max_pairs=50)
        self.assertEqual(first, second)
        self.assertEqual(first["1"][2], 50)


class MainUniquenessTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.work = os.path.join(self.dir.name, "data")
        self.project = os.path.join(self.dir.name, "agents")
        os.makedirs(self.work)
        os.makedirs(os.path.join(self.project, "Рассылка"))
        open(os.path.join(self.project, "CLAUDE.md"), "w").close()
        self.segment = os.path.join(self.project, "Рассылка", "segment.md")
        with open(self.segment, "w", encoding="utf-8") as fh:
            fh.write(SEGMENT)
        self.leads = os.path.join(self.work, "leads.csv")
        rows = [HEADER] + [lead(f"p{i}@x.ru", "47.91") for i in range(6)]
        with open(self.leads, "w", encoding="utf-8-sig", newline="") as fh:
            csv.writer(fh, delimiter=";").writerows(rows)
        self.logs = []

    def go(self, letters_text, *extra):
        letters = os.path.join(self.project, "Рассылка", "letters.md")
        with open(letters, "w", encoding="utf-8") as fh:
            fh.write(letters_text)
        argv = [self.leads, "--letters", letters, "--segment", self.segment, "--env", os.path.join(self.dir.name, "нет.env"),
                "--link", LINK, "--signature", SIGNATURE, *extra]
        return compose.main(argv, log=self.logs.append)

    def output(self):
        return "\n".join(self.logs)

    def test_letters_with_enough_variants_pass_and_the_report_is_shown(self):
        self.assertEqual(self.go(LETTERS), 0)
        self.assertIn("уникальность писем группы 1", self.output())
        letters, problems = lettertable.read_letters(os.path.join(self.work, "letters_1.csv"))
        self.assertEqual((len(letters), problems), (6, []))
        self.assertGreater(len({letter.body.split("\n")[0] for letter in letters}), 1)
        self.assertEqual(len({letter.subject for letter in letters}), 3)

    def test_too_similar_letters_are_not_written(self):
        one_variant = (LETTERS.replace("- Привет!\n", "").replace("- Здравствуйте!\n", "")
                       .replace("- утро: Доброе утро!\n- день: Добрый день!\n- вечер: Добрый вечер!\n", "- Здравствуйте!\n"))
        for slot in ("представление", "призыв"):
            head, rest = one_variant.split(f"### {slot}\n")
            items, tail = rest.split("\n\n", 1)
            one_variant = f"{head}### {slot}\n{items.splitlines()[0]}\n\n{tail}"
        self.assertEqual(self.go(one_variant), 1)
        self.assertIn("слишком похожи", self.output())
        self.assertFalse(os.path.exists(os.path.join(self.work, "letters_1.csv")))

    def test_the_check_can_be_switched_off(self):
        identical = LETTERS.split("## Варианты фраз")[0].replace("{{приветствие}}", "Здравствуйте!") \
            .replace("{{представление}}", "Это студия.").replace("{{причина_хвост}}", "").replace("{{призыв}}", "Опрос: {{ссылка_на_опрос}}")
        self.assertEqual(self.go(identical, "--min-unique", "0"), 0)

    def test_a_file_without_variants_only_warns(self):
        identical = LETTERS.split("## Варианты фраз")[0].replace("{{приветствие}}", "Здравствуйте!") \
            .replace("{{представление}}", "Это студия.").replace("{{причина_хвост}}", "").replace("{{призыв}}", "Опрос: {{ссылка_на_опрос}}")
        self.assertEqual(self.go(identical), 0)
        self.assertIn("нет раздела «Варианты фраз»", self.output())


class PersonalSubjectTest(unittest.TestCase):
    TEXT = LETTERS.replace("- Тема В\n", "- Тема В\n- Про {{группа_кратко}}: тема\n")

    def subjects(self, group, count=8):
        comp = composer(self.TEXT, now=datetime(2026, 10, 12, 14, 0))
        result = []
        for _ in range(count):
            result.append(comp.subject_variant(group))
            comp.letter_text(1, group)
        return result

    def test_the_trade_group_gets_subjects_with_its_name(self):
        subjects = self.subjects("1")
        self.assertIn("Про торговля: тема", subjects)
        self.assertTrue(all("{{" not in subject for subject in subjects))

    def test_the_fallback_group_never_gets_a_subject_with_a_name(self):
        subjects = self.subjects("12")
        self.assertEqual(sorted(set(subjects)), ["Тема А", "Тема Б", "Тема В"])

    def test_the_short_name_stops_at_a_colon_or_a_bracket(self):
        groups = {group.id: group for group in agentmd.parse_segment(SEGMENT)}
        self.assertEqual(groups["6"].short, "услуги для бизнеса")
        self.assertEqual(groups["1"].short, "торговля")
        self.assertEqual(groups["12"].short, "")

    def test_only_the_subject_may_use_the_short_name(self):
        with self.assertRaises(ComposeError):
            compose.check_variants({"призыв": [("", "{{группа_кратко}} опрос")]})
        with self.assertRaises(ComposeError):
            compose.check_variants({"тема": [("", "Тема {{группа_словами}}")]})

    def test_a_subject_list_needs_a_variant_for_the_fallback_group(self):
        with self.assertRaises(ComposeError) as ctx:
            compose.check_variants({"тема": [("", "О {{группа_кратко}}"), ("", "Про {{группа_кратко}}")]})
        self.assertIn("группа_кратко", str(ctx.exception))


class OneLetterPerDomainTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.comp = composer()

    def run_first(self, *emails):
        leads = [{"email": email, "okved": "47.91", "source": "ЕГРЮЛ"} for email in emails]
        return compose.compose_first(self.comp, leads, "Тема", self.dir.name, frozenset(), frozenset())

    def test_a_second_address_on_a_company_domain_gets_no_letter(self):
        result = self.run_first("fort4@corp.example", "fort5@corp.example", "other@elsewhere.example")
        self.assertEqual([letter[0] for letter in result.letters], ["fort4@corp.example", "other@elsewhere.example"])
        self.assertEqual(sum(result.skipped.values()), 1)
        self.assertIn("одно письмо на домен", list(result.skipped)[0])

    def test_free_mail_domains_are_not_merged(self):
        result = self.run_first("a@mail.ru", "b@mail.ru", "c@gmail.com", "d@gmail.com", "e@yandex.ru")
        self.assertEqual(len(result.letters), 5)

    def test_the_domain_is_case_insensitive(self):
        result = self.run_first("a@Corp.Example", "b@corp.example")
        self.assertEqual(len(result.letters), 1)

    def test_a_domain_that_already_got_a_letter_in_any_earlier_batch_is_skipped(self):
        journal.save_state(compose.journal_path(self.dir.name, 1), {"old@corp.example": {"status": "sent"}})
        result = self.run_first("new@corp.example", "new@fresh.example")
        self.assertEqual([letter[0] for letter in result.letters], ["new@fresh.example"])


if __name__ == "__main__":
    unittest.main()
