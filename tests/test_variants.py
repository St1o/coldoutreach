"""Варианты фраз в письмах: раздел «Варианты фраз» файла агента, приветствие по времени суток, общий счётчик вариантов
и тем (он продолжается между партиями) и проверка писем на повторы по каждому письму (compose.uniqueness_by_letter,
ключи --min-unique и --min-unique-subject)."""
import csv
import os
import re
import tempfile
import unittest
from datetime import date, datetime

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


def composer(letters=LETTERS, now=None, folder=""):
    groups = agentmd.parse_segment(SEGMENT)
    texts = agentmd.parse_letters(letters)
    agentmd.check_texts(texts, groups)
    return compose.Composer(groups, texts, LINK, SIGNATURE, now=now, folder=folder)


def letters_with_variants(count, subjects=None):
    """LETTERS, где у каждого места по count совсем разных вариантов, а тем subjects (по умолчанию тоже count): слова в
    вариантах не повторяются, поэтому письма с разными номерами сильно отличаются, а с номерами через count совпадают."""
    def phrase(tag, number):
        return " ".join(f"{tag}{number}{word}" for word in "абвгдежз")

    slots = [("приветствие", lambda n: phrase("привет", n) + "!"),
             ("представление", lambda n: phrase("студия", n) + "."),
             ("причина_хвост", lambda n: ", " + phrase("причина", n) + " — {{группа_словами}}"),
             ("призыв", lambda n: phrase("призыв", n) + ": {{ссылка_на_опрос}}")]
    section = ["## Варианты фраз", ""]
    for slot, make in slots:
        section += [f"### {slot}"] + [f"- {make(n)}" for n in range(count)] + [""]
    if subjects != 0:
        section += ["### тема"] + [f"- {phrase('тема', n)}" for n in range(count if subjects is None else subjects)]
    return LETTERS.split("## Варианты фраз")[0] + "\n".join(section) + "\n"


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

    def test_letters_of_different_groups_get_different_variants_and_subjects(self):
        comp = composer(now=datetime(2026, 10, 12, 14, 0))
        subjects, picked = [], []
        for group in ("1", "3", "12"):                          # по одному письму от трёх групп, вариантов в местах три
            subjects.append(comp.subject_variant(group))
            paragraphs = comp.letter_text(1, group).split("\n\n")
            picked.append((paragraphs[0], paragraphs[1], paragraphs[4]))      # приветствие, представление, призыв
        self.assertEqual(len(set(subjects)), 3)
        for slot in range(3):
            self.assertEqual(len({letter[slot] for letter in picked}), 3, slot)
        self.assertEqual(len(set(picked)), 3)

    def test_stages_are_counted_separately(self):
        now = datetime(2026, 10, 12, 14, 0)
        first_follow_up = composer(now=now).letter_text(2, "1")
        comp = composer(now=now)
        comp.letter_text(1, "1")
        comp.letter_text(1, "12")
        self.assertEqual(comp.letter_text(2, "1"), first_follow_up)

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


class ContinuedNumbersTest(unittest.TestCase):
    """Номера вариантов и тем продолжаются между партиями: счёт идёт от числа писем, принятых сервером по журналу."""
    NOW = datetime(2026, 10, 12, 14, 0)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.text = letters_with_variants(8)
        self.reference = os.path.join(self.dir.name, "ref")             # папка без журнала: счёт с нуля

    def build(self, folder, first, count, group_okved="47.91"):
        comp = composer(self.text, now=self.NOW, folder=folder)
        leads = [{"email": f"p{n}@x.ru", "okved": group_okved, "source": "ЕГРЮЛ"} for n in range(first, first + count)]
        result = compose.compose_first(comp, leads, "Тема", folder, frozenset(), frozenset())
        return [(letter[0], letter[1], letter[2]) for letter in result.letters]

    def mark(self, letters, stage=1, status=journal.SENT):
        journal.save_state(compose.journal_path(self.dir.name, stage),
                           {address: journal.make_entry(status, subject, {}) for address, subject, _ in letters})

    @staticmethod
    def contents(letters):
        return [(subject, text) for _, subject, text in letters]

    def test_the_second_batch_continues_after_the_sent_first_one(self):
        whole = self.build(self.reference, 0, 6)
        first = self.build(self.dir.name, 0, 3)
        self.mark(first)
        second = self.build(self.dir.name, 3, 3)
        self.assertEqual(self.contents(first + second), self.contents(whole))
        self.assertNotEqual(self.contents(second)[0], self.contents(first)[0])        # не с нуля

    def test_letters_composed_but_not_sent_take_no_numbers(self):
        first = self.build(self.dir.name, 0, 3)
        again = self.build(self.dir.name, 0, 3)                          # первую партию так и не отправили
        self.assertEqual(self.contents(again), self.contents(first))

    def test_only_the_sent_part_of_a_batch_counts(self):
        whole = self.build(self.reference, 0, 6)
        first = self.build(self.dir.name, 0, 4)
        self.mark(first[:2])                                             # ушли два из четырёх
        rest = self.build(self.dir.name, 2, 3)
        self.assertEqual(self.contents(rest), self.contents(whole)[2:5])

    def test_a_letter_the_server_rejected_takes_no_number(self):
        whole = self.build(self.reference, 0, 3)
        first = self.build(self.dir.name, 0, 2)
        journal.save_state(compose.journal_path(self.dir.name, 1), {
            first[0][0]: journal.make_entry(journal.SENT, first[0][1], {}),
            first[1][0]: journal.make_entry(journal.REJECTED, first[1][1], {})})
        second = self.build(self.dir.name, 2, 1)
        self.assertEqual(self.contents(second), self.contents(whole)[1:2])

    def test_the_numbers_of_a_stage_follow_only_the_journal_of_that_stage(self):
        fresh = composer(self.text, now=self.NOW).letter_text(2, "1")
        self.mark(self.build(self.reference, 0, 3), stage=1)
        self.assertEqual(composer(self.text, now=self.NOW, folder=self.dir.name).letter_text(2, "1"), fresh)
        self.mark(self.build(self.reference, 0, 3), stage=2)
        self.assertNotEqual(composer(self.text, now=self.NOW, folder=self.dir.name).letter_text(2, "1"), fresh)

    def test_a_missing_folder_or_journal_means_the_count_starts_at_zero(self):
        fresh = composer(self.text, now=self.NOW).letter_text(1, "1")
        self.assertEqual(composer(self.text, now=self.NOW, folder=self.reference).letter_text(1, "1"), fresh)


class FillingTest(unittest.TestCase):
    def test_slots_are_replaced_and_the_clause_for_the_fallback_group_is_removed(self):
        comp = composer(now=datetime(2026, 10, 12, 14, 0))
        for _ in range(6):
            text = comp.letter_text(1, "12")
            self.assertNotIn("{{", text)
            self.assertNotIn("вид деятельности", text)
            self.assertNotIn("сфера деятельности", text)
            self.assertNotIn("сфера", text)
            self.assertEqual(text.count(LINK), 1)

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
    def letters(self, texts, subjects=None, groups=None):
        return [(f"a{i}@x.ru", subjects[i] if subjects else "Тема", text, groups[i] if groups else "1", "ЕГРЮЛ")
                for i, text in enumerate(texts)]

    def test_identical_letters_have_no_difference_and_name_each_other(self):
        text = "Раз два три четыре пять шесть семь восемь девять десять"
        report = compose.uniqueness_by_letter(self.letters([text, text, text]))
        self.assertEqual([row[:2] for row in report], [(0.0, 1), (0.0, 0), (0.0, 0)])

    def test_different_letters_have_a_big_difference(self):
        report = compose.uniqueness_by_letter(self.letters(["один два три четыре пять шесть",
                                                            "семь восемь девять десять одиннадцать двенадцать"]))
        self.assertEqual([row[:2] for row in report], [(100.0, 1), (100.0, 0)])

    def test_the_link_is_not_compared(self):
        a = "Привет мир как дела сегодня https://docs.google.com/forms/d/e/AAA/viewform"
        b = "Привет мир как дела сегодня https://docs.google.com/forms/d/e/BBB/viewform"
        self.assertEqual(compose.uniqueness_by_letter(self.letters([a, b]))[0][0], 0.0)

    def test_one_letter_has_nobody_to_be_compared_with(self):
        self.assertEqual(compose.uniqueness_by_letter(self.letters(["один два три четыре"])), [(None,) * 4])
        self.assertEqual(compose.uniqueness_by_letter([]), [])

    def test_letters_of_different_groups_are_compared_with_each_other(self):
        text = "Раз два три четыре пять шесть семь"
        report = compose.uniqueness_by_letter(self.letters([text, text], groups=["1", "3"]))
        self.assertEqual([row[:2] for row in report], [(0.0, 1), (0.0, 0)])

    def test_every_letter_gets_its_own_closest_letter(self):
        a, b, c = "а б в г д е", "а б в г д ж", "щ ы э ю я ё"        # a и b делят три тройки слов из пяти: разница 40%
        report = compose.uniqueness_by_letter(self.letters([a, b, c]))
        self.assertEqual([row[:2] for row in report], [(40.0, 1), (40.0, 0), (100.0, 0)])

    def test_the_least_difference_decides_not_the_average(self):
        far = "щ ы э ю я ё"
        twin = "а б в г д е"
        report = compose.uniqueness_by_letter(self.letters([twin, far, twin]))
        self.assertEqual(report[0][:2], (0.0, 2))                       # в среднем 50%, но у пары одинаковых 0%
        self.assertEqual(report[1][:2], (100.0, 0))

    def test_the_subject_is_compared_by_the_set_of_words(self):
        subjects = ["Вопрос про торговлю", "Вопрос про стройку", "Тема А!", "тема а", "!!!", "..."]
        report = compose.uniqueness_by_letter(self.letters(["текст"] * 6, subjects))
        self.assertEqual(report[0][2:], (50.0, 1))                      # два общих слова из четырёх
        self.assertEqual(report[2][2:], (0.0, 3))                       # регистр и знаки не в счёт
        self.assertEqual(report[4][2:], (0.0, 5))                       # слов нет вовсе: деления на ноль нет

    def test_weak_pairs_lists_each_pair_once_most_similar_first(self):
        report = compose.uniqueness_by_letter(self.letters(["а б в г д е", "а б в г д ж", "а б в г д е", "щ ы э ю я ё"]))
        self.assertEqual(compose.weak_pairs(report, 0, 25), [(0.0, 0, 2)])
        self.assertEqual(compose.weak_pairs(report, 0, 41), [(0.0, 0, 2), (40.0, 0, 1)])   # у письма 1 два равных ближайших: берётся первое
        self.assertEqual(compose.weak_pairs(report, 0, 0), [])

    def test_many_distinct_letters_are_compared_exactly(self):
        texts = [f"слово{i % 7} слово{i % 5} общее общее общее слово{i % 3} конец" for i in range(80)]
        report = compose.uniqueness_by_letter(self.letters(texts))
        self.assertEqual(len(report), 80)
        self.assertEqual(report, compose.uniqueness_by_letter(self.letters(texts)))
        for number, (diff, other, _, _) in enumerate(report):
            self.assertNotEqual(number, other)
            first, second = compose._shingles(texts[number]), compose._shingles(texts[other])
            self.assertAlmostEqual(diff, 100.0 * (1 - len(first & second) / len(first | second)))


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

    def go(self, letters_text, *extra, **kw):
        letters = os.path.join(self.project, "Рассылка", "letters.md")
        with open(letters, "w", encoding="utf-8") as fh:
            fh.write(letters_text)
        argv = [self.leads, "--letters", letters, "--segment", self.segment, "--env", os.path.join(self.dir.name, "нет.env"),
                "--link", LINK, "--signature", SIGNATURE, *extra]
        return compose.main(argv, log=self.logs.append, **kw)

    def output(self):
        return "\n".join(self.logs)

    def test_letters_with_enough_variants_pass_and_the_smallest_numbers_are_shown(self):
        self.assertEqual(self.go(letters_with_variants(6)), 0)
        self.assertRegex(self.output(), r"уникальность: наименьшая разница письма с любым другим письмом сборки "
                                        r"по тексту \d+% \(письма \d+ и \d+; порог 25%\), по теме \d+% \(письма \d+ и \d+; порог 25%\)")
        letters, problems = lettertable.read_letters(os.path.join(self.work, "letters_1.csv"))
        self.assertEqual((len(letters), problems), (6, []))
        self.assertEqual(len({letter.body.split("\n")[0] for letter in letters}), 6)
        self.assertEqual(len({letter.subject for letter in letters}), 6)

    def test_two_letters_with_the_same_subject_stop_the_build(self):
        self.assertEqual(self.go(letters_with_variants(6, subjects=5)), 1)          # тем пять, писем шесть: первая тема вернулась
        self.assertIn("слишком похожи", self.output())
        self.assertIn("по теме (порог 25%) ниже порога писем 2; пары (номера по порядку в сборке): 1 и 6 - 0%", self.output())
        self.assertNotIn("по тексту (порог", self.output())                          # тексты шести писем разные
        self.assertNotIn("@", self.output())                                          # адресов в сообщении нет
        self.assertFalse(os.path.exists(os.path.join(self.work, "letters_1.csv")))
        self.logs.clear()
        self.assertEqual(self.go(letters_with_variants(6, subjects=5), "--min-unique-subject", "0"), 0)
        self.assertTrue(os.path.exists(os.path.join(self.work, "letters_1.csv")))

    def test_a_pair_of_letters_below_the_threshold_stops_the_build_with_both_numbers(self):
        close = letters_with_variants(1, subjects=2)                  # фразы везде одни, кроме призыва: там два варианта
        call = "- призыв0а призыв0б призыв0в призыв0г призыв0д призыв0е призыв0ж призыв0з: {{ссылка_на_опрос}}"
        self.assertIn(call, close)
        close = close.replace(call, call + "\n" + call.replace("призыв0а", "другое"))      # они отличаются одним словом
        rows = [HEADER] + [lead(f"p{i}@x.ru", "47.91") for i in range(2)]
        with open(self.leads, "w", encoding="utf-8-sig", newline="") as fh:
            csv.writer(fh, delimiter=";").writerows(rows)
        self.assertEqual(self.go(close), 1)
        found = re.search(r"по тексту \(порог 25%\) ниже порога писем 2; пары \(номера по порядку в сборке\): 1 и 2 - (\d+)%",
                          self.output())
        self.assertIsNotNone(found, self.output())
        self.assertTrue(0 < int(found.group(1)) < 25)
        self.assertNotIn("по теме (порог", self.output())                            # темы двух писем разные
        self.assertNotIn("@", self.output())
        self.assertFalse(os.path.exists(os.path.join(self.work, "letters_1.csv")))
        self.logs.clear()
        self.assertEqual(self.go(close, "--min-unique", "5"), 0)                     # а с порогом ниже их разницы собираются

    def test_a_batch_bigger_than_the_variants_names_a_repeated_pair_of_texts(self):
        self.assertEqual(self.go(letters_with_variants(5, subjects=6)), 1)          # шесть писем, пять кругов: 1 и 6 одинаковы
        self.assertIn("по тексту (порог 25%) ниже порога писем 2; пары (номера по порядку в сборке): 1 и 6 - 0%",
                      self.output())

    def test_the_subject_is_not_checked_without_the_slot_or_on_follow_up_stages(self):
        self.assertEqual(self.go(letters_with_variants(6, subjects=0)), 0)           # места «тема» нет: тема одна на все письма
        self.assertNotIn("по теме", self.output())
        self.logs.clear()
        old = {f"p{i}@x.ru": {"status": journal.SENT, "time": "2026-10-05T09:00:00+00:00", "note": "", "subject": "Тема",
                              "meta": {"Группа": "1", "Источник адреса": "ЕГРЮЛ"}} for i in range(3)}
        journal.save_state(compose.journal_path(self.work, 1), old)                  # тема дожима берётся из журнала: у всех одна
        self.assertEqual(self.go(letters_with_variants(6), "--stage", "2", today=date(2026, 10, 20)), 0)
        self.assertIn("по тексту", self.output())
        self.assertNotIn("по теме", self.output())

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

    def test_the_fallback_group_takes_its_number_from_the_shared_counter(self):
        comp = composer(self.TEXT, now=datetime(2026, 10, 12, 14, 0))
        offset = sum(map(ord, agentmd.SUBJECT_SLOT))
        with_name = ["Тема А", "Тема Б", "Тема В", "Про торговля: тема"]        # список группы с названием сферы
        without = ["Тема А", "Тема Б", "Тема В"]                                # у запасной группы короче
        got = []
        for group in ("1", "12", "1", "12"):
            got.append(comp.subject_variant(group))
            comp.letter_text(1, group)
        self.assertEqual(got, [with_name[offset % 4], without[(1 + offset) % 3],
                               with_name[(2 + offset) % 4], without[(3 + offset) % 3]])

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
