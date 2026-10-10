"""Разбор выписки из ЕГРЮЛ/ЕГРИП (PDF): ФИО руководителя и адрес электронной почты.

Текст из PDF достаётся библиотекой pypdf, дальше всё делается по тексту, склеенному в одну
строку. В настоящих выписках (проверено на двух образцах) это выглядит так:

    Сведения о лице, имеющем право без доверенности действовать от имени юридического лица
    ...
    26 Фамилия / Имя / Отчество          <- три подписи подряд,
    ИВАНОВ / ИВАН / ИВАНОВИЧ             <- затем три значения
    27 ИНН ...

    Адрес электронной почты              <- раздел есть только если почта указана
    10 E-mail INFO@EXAMPLE.RU

ФИО берётся из раздела руководителя (для ИП - из «Сведений об индивидуальном
предпринимателе»), почта - из строки «E-mail».

Основной ОКВЭД - из раздела «Сведения об основном виде деятельности»: код вида «62.01» и
название после него. Раскладку этого раздела на настоящих выписках ещё не проверяли, поэтому
код ищется терпимо (любой код с точкой после заголовка раздела), а если не нашёлся - поле пустое.
"""
import io
import re
from dataclasses import dataclass

from pypdf import PdfReader

_WORD = r"[^\W\d_]+(?:[-'][^\W\d_]+)*"      # слово из букв (возможно через дефис)

DIRECTOR_SECTION_RE = re.compile(
    r"имеющем\s+право\s+без\s+доверенности\s+действовать\s+от\s+имени\s+юридического\s+лица",
    re.IGNORECASE)
ENTREPRENEUR_SECTION_RE = re.compile(r"Сведения\s+об\s+индивидуальном\s+предпринимателе", re.IGNORECASE)
NEXT_SECTION_RE = re.compile(r"Сведения\s+об?\s")
# подписи подряд, затем 2-3 значения, затем номер следующей строки таблицы
FIO_COLUMNS_RE = re.compile(
    rf"Фамилия\s+Имя(?:\s+Отчество)?\s+({_WORD}(?:\s+{_WORD}){{1,2}})(?=\s+\d)")
# запасной вариант: «Фамилия ИВАНОВ Имя ИВАН Отчество ИВАНОВИЧ»
FIO_ROWS_RE = re.compile(
    rf"Фамилия\s+({_WORD})\s+(?:\d+\s+)?Имя\s+({_WORD})"
    rf"(?:\s+(?:\d+\s+)?Отчество\s+({_WORD}))?")
MAIN_OKVED_RE = re.compile(r"Сведения\s+об\s+основном\s+виде\s+деятельности", re.IGNORECASE)
# код с точкой («62.01», «47.19.1»), затем название до номера следующей строки таблицы
OKVED_CODE_RE = re.compile(
    r"(?<![\d.])(\d{2}\.\d{1,2}(?:\.\d{1,2})?)(?![\d.])\s+(.+?)(?=\s+\d{1,3}\s+[А-ЯЁA-Z]|$)")
EMAIL_ROW_RE = re.compile(r"(?<!\w)E-?mail\s+(.+?)(?=\s+\d+\s+\D|$)", re.IGNORECASE)
EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
# адреса налоговых органов в выписке - не почта компании
IGNORED_EMAIL_DOMAINS = ("nalog.ru", "nalog.gov.ru")

SECTION_WINDOW = 2500       # сколько знаков после заголовка раздела просматриваем


class ExtractError(Exception):
    """PDF не удалось открыть или прочитать."""


@dataclass
class ExtractData:
    director: str = ""      # ФИО (Фамилия Имя Отчество)
    email: str = ""
    okved: str = ""         # код основного вида деятельности, например «62.01»
    okved_name: str = ""


def pdf_to_text(pdf: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:    # pypdf бросает разные ошибки на повреждённых файлах
        raise ExtractError(f"Не удалось прочитать PDF: {exc}") from exc


def _flatten(text: str) -> str:
    return " ".join(text.split())


def _title(word: str) -> str:
    return "-".join(part.capitalize() for part in word.split("-"))


def find_director(flat: str) -> str:
    for section_re in (DIRECTOR_SECTION_RE, ENTREPRENEUR_SECTION_RE):
        for section in section_re.finditer(flat):
            window = flat[section.end(): section.end() + SECTION_WINDOW]
            following = NEXT_SECTION_RE.search(window)
            if following:
                window = window[:following.start()]      # не заходим в следующий раздел
            match = FIO_COLUMNS_RE.search(window)
            words = match.group(1).split() if match else []
            if not words:
                match = FIO_ROWS_RE.search(window)
                words = [part for part in match.groups() if part] if match else []
            if words:
                return " ".join(_title(word) for word in words)
    return ""


def _valid_email(candidate: str) -> str:
    candidate = candidate.rstrip(".-").lower()
    if any(candidate.endswith("@" + d) or candidate.endswith("." + d) for d in IGNORED_EMAIL_DOMAINS):
        return ""
    return candidate


def find_email(flat: str) -> str:
    # 1) строка «E-mail ...» (длинный адрес в ячейке может быть перенесён - склеиваем без пробелов)
    # 2) запасной вариант: любой адрес в тексте
    chunks = [match.group(1).replace(" ", "") for match in EMAIL_ROW_RE.finditer(flat)]
    chunks.append(re.sub(r"\s*@\s*", "@", flat))
    for chunk in chunks:
        for candidate in EMAIL_RE.findall(chunk):
            email = _valid_email(candidate)
            if email:
                return email
    return ""


def find_main_okved(flat: str):
    """(код, название) основного вида деятельности; ("", "") если раздел не найден."""
    for section in MAIN_OKVED_RE.finditer(flat):
        window = flat[section.end(): section.end() + SECTION_WINDOW]
        following = NEXT_SECTION_RE.search(window)
        if following:
            window = window[:following.start()]
        match = OKVED_CODE_RE.search(window)
        if match:
            return match.group(1), match.group(2).strip()[:200]
    return "", ""


def parse_extract(text: str) -> ExtractData:
    flat = _flatten(text)
    okved, okved_name = find_main_okved(flat)
    return ExtractData(director=find_director(flat), email=find_email(flat),
                       okved=okved, okved_name=okved_name)
