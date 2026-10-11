"""Почтовые ящики рассылки: подключение (local.env), прогрев, выбор ящиков и раздача писем по очереди.

Ящики в local.env: первый - MAILRU_USER / MAILRU_APP_PASSWORD / MAIL_FROM_NAME, второй - те же имена с суффиксом _2,
третий - _3 и так до _5. Пароли живут только там и сюда попадают из окружения; в repr и в сообщения не выводятся.

Прогрев (решение владельца 2026-10-11): у каждого ящика своя дата первого письма. В первый день с ящика можно отправить
7 писем, в каждый следующий на 3 больше (7, 10, 13, …), но не больше жёсткого предела недели прогрева (дни 1-7: 15,
дни 8-14: 30, с дня 15: 40; зашит в программу и от настройки на /mail не зависит) и не больше лимита в сутки
(страница /mail, по умолчанию 40, может быть только ниже предела недели; выше 50 его не поставить).
Дату можно задать на странице /mail (settings.json); если не задана, это день самого раннего письма ящика в журнале,
а если писем ещё не было, прогрев начинается сегодня."""
import math
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from . import config, journal


MAX_MAILBOXES = 5
WARMUP_FIRST_DAY = 7              # писем с ящика в первый день
WARMUP_STEP = 3                   # на сколько больше с каждым следующим днём
# Жёсткий предел писем за день по неделям прогрева (чтобы письма не шли в спам). От настройки на /mail не зависит:
# она может только снизить предел. (последний день недели, предел) для дней 1-7 и 8-14, дальше WARMUP_CAP_LATER.
WARMUP_WEEK_CAPS = ((7, 15), (14, 30))
WARMUP_CAP_LATER = 40             # с дня 15 (с третьей недели) и дальше
SETTINGS_KEY = "mailboxes"        # settings.json: {"mailboxes": {"2": {"first_message": "2026-10-12"}}}
FIRST_KEY = "first_message"
SOURCE_SETTING, SOURCE_JOURNAL, SOURCE_NONE = "задана вручную", "по журналу отправки", "ещё не отправляли"


@dataclass(frozen=True)
class Mailbox:
    id: str
    user: str
    password: str = field(default="", repr=False)          # пароль не попадает ни в repr, ни в журналы
    from_name: str = ""

    @property
    def ready(self) -> bool:
        return config.check_email(self.user) and bool(self.password)

    @property
    def label(self) -> str:
        return f"ящик {self.id}"

    @property
    def title(self) -> str:
        return f"Ящик {self.id}"          # с начала строки


def env_suffix(number: int) -> str:
    return "" if number == 1 else f"_{number}"


def env_names(number: int) -> tuple:
    """Имена переменных local.env для ящика номер number: адрес, пароль для внешних приложений, имя отправителя."""
    suffix = env_suffix(number)
    return f"MAILRU_USER{suffix}", f"MAILRU_APP_PASSWORD{suffix}", f"MAIL_FROM_NAME{suffix}"


def load_mailboxes(environ=None) -> list:
    """Ящики из окружения (после load_env_file): в порядке номеров, только те, где вписано хоть что-то.
    Имя отправителя второго и следующих ящиков по умолчанию как у первого."""
    environ = os.environ if environ is None else environ
    boxes, first_name = [], ""
    for number in range(1, MAX_MAILBOXES + 1):
        user_key, password_key, name_key = env_names(number)
        user, password, name = (environ.get(user_key, "").strip(), environ.get(password_key, ""),
                                environ.get(name_key, "").strip())
        if number == 1:
            first_name = name
        if not user and not password:
            continue
        boxes.append(Mailbox(str(number), user, password, name or first_name))
    return boxes


def problems(boxes) -> list:
    """Что не так с настройкой ящиков (без паролей и адресов получателей): пусто - всё в порядке."""
    found, seen = [], {}
    for box in boxes:
        user_key, password_key, _ = env_names(int(box.id))
        if not box.ready:
            found.append(f"{box.label}: впишите оба значения в local.env: {user_key} (полный адрес ящика) и "
                         f"{password_key} («пароль для внешних приложений»).")
        elif box.user.lower() in seen:
            found.append(f"{box.label} и {seen[box.user.lower()]} с одним адресом: у каждого ящика свой адрес.")
        else:
            seen[box.user.lower()] = box.label
    return found


def select(boxes, count=None, only=None) -> list:
    """Ящики этой отправки. По умолчанию все подключённые. count - первые N ящиков (N больше, чем подключено,
    значит все); only - список номеров («2» или «1,2»): ровно эти. Оба сразу нельзя. Ошибка - ValueError."""
    ready = [box for box in boxes if box.ready]
    if not ready:
        raise ValueError("Не подключён ни один ящик: впишите MAILRU_USER и MAILRU_APP_PASSWORD в local.env.")
    if count is not None and only:
        raise ValueError("Укажите либо число ящиков, либо номер ящика, но не оба сразу.")
    if only:
        wanted = [part.strip() for part in str(only).replace(";", ",").split(",") if part.strip()]
        known = {box.id: box for box in ready}
        missing = [number for number in wanted if number not in known]
        if missing or not wanted:
            raise ValueError("Нет такого подключённого ящика: " + ", ".join(missing or ["(пусто)"])
                             + ". Есть: " + ", ".join(sorted(known)) + ".")
        return [known[number] for number in sorted(set(wanted), key=int)]
    if count is not None:
        try:
            number = int(count)
        except (TypeError, ValueError):
            number = 0
        if number < 1:
            raise ValueError("Со скольки ящиков отправлять: целое число от 1.")
        return ready[:number]
    return ready


# ---- прогрев ----

def today_msk(now=None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(config.MSK).date()


def parse_day(raw) -> Optional[date]:
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError:
        return None


def first_message_day(folder: str, settings: dict, mailbox_id: str):
    """(день первого письма ящика, откуда он известен): задан на странице /mail, иначе по журналу, иначе нет."""
    boxes = settings.get(SETTINGS_KEY)
    entry = boxes.get(str(mailbox_id)) if isinstance(boxes, dict) else None
    chosen = parse_day(entry.get(FIRST_KEY, "")) if isinstance(entry, dict) else None
    if chosen:
        return chosen, SOURCE_SETTING
    logged = journal.first_sent_day(folder, mailbox_id)
    return (logged, SOURCE_JOURNAL) if logged else (None, SOURCE_NONE)


def set_first_message(settings_path: str, mailbox_id: str, raw: str) -> Optional[date]:
    """Записывает дату первого письма ящика в settings.json (страница /mail). Пустое значение стирает дату: тогда
    день берётся по журналу. Неверная дата - ValueError. Возвращает записанную дату или None."""
    raw = (raw or "").strip()
    day = parse_day(raw) if raw else None
    if raw and day is None:
        raise ValueError("Дата первого письма: ГГГГ-ММ-ДД, например 2026-10-12.")
    settings = config.load_settings(settings_path)
    boxes = settings.get(SETTINGS_KEY)
    boxes = dict(boxes) if isinstance(boxes, dict) else {}
    entry = dict(boxes.get(str(mailbox_id)) or {})
    if day:
        entry[FIRST_KEY] = day.isoformat()
    else:
        entry.pop(FIRST_KEY, None)
    if entry:
        boxes[str(mailbox_id)] = entry
    else:
        boxes.pop(str(mailbox_id), None)
    settings[SETTINGS_KEY] = boxes
    config.save_settings(settings, settings_path)
    return day


def warmup_day(first: Optional[date], today: date) -> int:
    """Номер дня прогрева: в день первого письма 1. Даты нет - первое письмо сегодня, день 1. До даты - 0 и меньше."""
    return 1 if first is None else (today - first).days + 1


def week_cap(day: int) -> int:
    """Жёсткий предел писем за день для дня прогрева day: 15 в дни 1-7, 30 в дни 8-14, 40 с дня 15. До начала (день < 1) 0."""
    if day < 1:
        return 0
    for last_day, cap in WARMUP_WEEK_CAPS:
        if day <= last_day:
            return cap
    return WARMUP_CAP_LATER


def week_number(day: int) -> int:
    """Номер недели прогрева (дни 1-7 - неделя 1, 8-14 - неделя 2, с дня 15 - неделя 3 и дальше); до начала 0."""
    return 0 if day < 1 else min((day - 1) // 7 + 1, len(WARMUP_WEEK_CAPS) + 1)


def warmup_quota(day: int, ceiling: int = 0) -> int:
    """Сколько писем с ящика можно отправить за день прогрева: 7, 10, 13, … не больше предела недели (week_cap) и не
    больше лимита в сутки с /mail (0 - без лимита; он может только снизить предел недели, поднять его не может)."""
    if day < 1:
        return 0
    quota = min(WARMUP_FIRST_DAY + WARMUP_STEP * (day - 1), week_cap(day))
    return min(quota, ceiling) if ceiling else quota


def mailbox_state(folder: str, mailbox_id: str, settings: dict, ceiling: int = 0, now=None, warmup: bool = True) -> dict:
    """Сколько ящик может отправить сейчас. room - сколько ещё писем (math.inf, если ограничений нет).
    Два предела: лимит в сутки (скользящие 24 часа, страница /mail) и квота дня прогрева (календарный день по Москве).
    warmup=False - только лимит в сутки, как было до прогрева."""
    now = now or datetime.now(timezone.utc)
    today = today_msk(now)
    sent_24h = journal.sent_last_24h(folder, now, mailbox_id)
    room = max(0, ceiling - sent_24h) if ceiling else math.inf
    state = {"id": str(mailbox_id), "ceiling": ceiling, "sent_24h": sent_24h, "warmup": warmup, "today": today.isoformat(),
             "first_message": None, "source": "", "day": None, "quota": None, "sent_today": None,
             "week": None, "week_cap": None}
    if warmup:
        first, source = first_message_day(folder, settings, mailbox_id)
        day = warmup_day(first, today)
        quota, sent_today = warmup_quota(day, ceiling), journal.sent_on_day(folder, today, mailbox_id)
        room = min(room, max(0, quota - sent_today))
        state.update(first_message=first.isoformat() if first else None, source=source, day=day, quota=quota,
                     sent_today=sent_today, week=week_number(day), week_cap=week_cap(day))
    state["room"] = room
    return state


def describe_state(state: dict) -> str:
    """Одна строка о ящике для журнала и статуса: без адресов и паролей."""
    if not state["warmup"]:
        room = "без ограничений" if state["room"] == math.inf else str(state["room"])
        return f"за 24 часа отправлено {state['sent_24h']}" + (f" из {state['ceiling']}" if state["ceiling"] else "") \
            + f", можно ещё {room}"
    if state["day"] < 1:
        return f"прогрев начнётся {state['first_message']}: до этой даты писем нет"
    ceiling = f"; лимит в сутки {state['ceiling']}, за 24 часа отправлено {state['sent_24h']}" if state["ceiling"] else ""
    week = f"{state['week']}" if state["week"] <= len(WARMUP_WEEK_CAPS) else f"{state['week']} и дальше"
    return (f"день прогрева {state['day']} (неделя {week}, жёсткий предел недели {state['week_cap']}), "
            f"на сегодня можно {state['quota']}, отправлено сегодня {state['sent_today']}, осталось {state['room']}{ceiling}")


# ---- раздача писем ----

def assign(letters, rooms: dict, preferred: Optional[dict] = None, limit: int = 0):
    """Раздаёт письма по ящикам по очереди (1, 2, 1, 2, …), пока у ящика есть место на сегодня. rooms - {номер ящика:
    сколько ещё можно}, в порядке очереди; preferred - {адрес: номер ящика}: письмо идёт с «своего» ящика (дожим с того же,
    что и первое письмо), а если там нет места или он не выбран, письмо ждёт. limit - не больше стольких писем (0 - все).
    Возвращает (план [(письмо, номер ящика)], ждут {номер ящика: сколько писем})."""
    rooms, order, waiting, plan = dict(rooms), list(rooms), Counter(), []
    preferred, turn = preferred or {}, 0
    for letter in letters:
        if limit and len(plan) >= limit:
            break
        own = preferred.get(letter.email.lower())
        if own:
            if rooms.get(own, 0) > 0:
                rooms[own] -= 1
                plan.append((letter, own))
            else:
                waiting[own] += 1
            continue
        for step in range(len(order)):
            candidate = order[(turn + step) % len(order)]
            if rooms[candidate] > 0:
                rooms[candidate] -= 1
                plan.append((letter, candidate))
                turn = (order.index(candidate) + 1) % len(order)
                break
        else:
            break                                   # нигде нет места на сегодня: остальное уйдёт в следующие дни
    return plan, waiting
