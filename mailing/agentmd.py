"""Разбор файлов агентов рассылки: таблица групп из файла сегмента и шаблоны писем с блоками боли."""
import re
from dataclasses import dataclass, field

FALLBACK_ID = "12"


class ComposeError(Exception):
    """Письма собрать нельзя; текст понятен пользователю."""


@dataclass
class Group:
    id: str
    name: str
    classes: frozenset          # первые два знака ОКВЭД; у запасной группы пусто

    @property
    def words(self) -> str:
        """{{группа_словами}}: название группы маленькими буквами; у запасной группы пусто."""
        return "" if self.id == FALLBACK_ID else self.name[:1].lower() + self.name[1:]


@dataclass
class Texts:
    subjects: list                                    # [(текст, рекомендована)]
    templates: dict                                   # этап -> текст шаблона
    pain1: dict = field(default_factory=dict)         # ключ группы -> блок боли письма 1
    pain2: dict = field(default_factory=dict)
    fallback1: str = ""
    fallback2: str = ""


def _key(name: str) -> str:
    return re.sub(r"\(.*?\)", "", name).split(":")[0].strip().casefold()


def _sections(text: str):
    """Файл -> [(заголовок «## …» без решёток, строки до следующего «## »)]."""
    result, heading, lines = [], None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if heading is not None:
                result.append((heading, lines))
            heading, lines = line[3:].strip(), []
        elif heading is not None:
            lines.append(line)
    if heading is not None:
        result.append((heading, lines))
    return result


def _fenced(lines):
    inside, block = False, []
    for line in lines:
        if line.strip().startswith("```"):
            if inside:
                return "\n".join(block).strip("\n")
            inside = True
        elif inside:
            block.append(line)
    return ""


def parse_segment(text: str) -> list:
    """Таблица групп (раздел «4. …» файла сегмента) -> [Group]. Запасная группа 12 есть всегда."""
    groups, section = [], None
    for heading, lines in _sections(text):
        if re.match(r"4\.", heading):
            section = lines
    if section is None:
        raise ComposeError("в файле сегмента нет раздела «4. …» с таблицей групп видов деятельности")
    for line in section:
        if not line.lstrip().startswith("|"):
            continue
        cell = line.strip().strip("|").split("|", 1)[0].strip()
        found = re.match(r"^(\d+[а-яa-z]?)\.\s+(.+)$", cell)
        if not found:
            continue
        group_id, rest = found.groups()
        with_classes = re.match(r"^(.+?)\s*\(([\d\s,–—-]+)\)\s*$", rest)
        if group_id == FALLBACK_ID or not with_classes:
            if group_id == FALLBACK_ID:
                groups.append(Group(FALLBACK_ID, rest.split(":")[0].strip(), frozenset()))
            continue
        classes = set()
        for token in with_classes.group(2).split(","):
            token = token.strip()
            if re.match(r"^\d+\s*[–—-]\s*\d+$", token):
                low, high = (int(part) for part in re.split(r"\s*[–—-]\s*", token))
                classes.update(range(low, high + 1))
            elif token.isdigit():
                classes.add(int(token))
        groups.append(Group(group_id, with_classes.group(1).strip(), frozenset(classes)))
    if not any(group.id == FALLBACK_ID for group in groups):
        groups.append(Group(FALLBACK_ID, "Запасной", frozenset()))
    if len(groups) < 2:
        raise ComposeError("в таблице групп файла сегмента не нашлось ни одной группы с классами ОКВЭД")
    return groups


def parse_letters(text: str) -> Texts:
    """Файл писем -> Texts: темы, шаблоны трёх писем, блоки боли по группам, запасные блоки."""
    subjects, templates, texts = [], {}, Texts([], {})
    for heading, lines in _sections(text):
        if heading.startswith("Темы"):
            for line in lines:
                found = re.match(r"^\s*\d+\.\s+(.*\S)\s*$", line)
                if found:
                    raw = found.group(1)
                    subjects.append((re.sub(r"\s*\(рекомендую\)\s*", "", raw).strip(), "(рекомендую)" in raw))
        elif re.match(r"Письмо 1\b", heading):
            templates[1] = _fenced(lines)
        elif heading.startswith("Дожим"):
            templates[2] = _fenced(lines)
            for line in lines:
                found = re.match(r"^\*([^*\n]+?):\*\s*(.+?)\s*$", line)
                if found:
                    _store(texts, 2, found.group(1), found.group(2))
        elif heading.startswith("Закрывающее"):
            templates[3] = _fenced(lines)
        elif heading.startswith("Блоки боли для письма 1"):
            name, parts = None, []
            for line in lines + ["**"]:
                found = re.match(r"^\*\*(.+?)\*\*\s*$", line)
                if found or line == "**":
                    if name is not None and parts:
                        _store(texts, 1, name, " ".join(parts))
                    name, parts = (found.group(1) if found else None), []
                elif name is not None and line.strip():
                    parts.append(line.strip())
    texts.subjects, texts.templates = subjects, templates
    return texts


def _store(texts: Texts, stage: int, name: str, block: str) -> None:
    key = _key(name)
    if key.startswith("запасной"):
        if stage == 1:
            texts.fallback1 = block
        else:
            texts.fallback2 = block
    else:
        (texts.pain1 if stage == 1 else texts.pain2)[key] = block


def check_texts(texts: Texts, groups: list) -> None:
    known = {_key(group.name) for group in groups}
    for key in list(texts.pain1) + list(texts.pain2):
        if key not in known:
            raise ComposeError(f"блок боли «{key}» из файла писем не найден в таблице групп файла сегмента: "
                               "названия должны совпадать")
    for stage in (1, 2, 3):
        if not texts.templates.get(stage):
            raise ComposeError(f"в файле писем нет шаблона письма {stage} (блок ``` под заголовком "
                               "«Письмо 1», «Дожим», «Закрывающее»)")
    if not texts.subjects:
        raise ComposeError("в файле писем нет тем (нумерованный список под заголовком «Темы»)")
    if not texts.fallback1 or not texts.fallback2:
        raise ComposeError("в файле писем нет запасных блоков боли («Запасной вариант» для письма 1 и для дожима)")
