"""Отправка готовых писем из таблицы через mail.ru (SMTP). Подробно: LOCAL.md, раздел «Рассылка».

    python mailer.py letters_1.csv                       # проверка, ничего не уходит (видно, с какого ящика что уйдёт)
    python mailer.py letters_1.csv --send --limit 5      # отправить первые 5
    python mailer.py letters_1.csv --send --background   # то же в фоне, команда сразу возвращается
    python mailer.py letters_1.csv --send --mailboxes 1  # только с первого ящика (по умолчанию со всех подключённых)
    python mailer.py letters_1.csv --send --mailbox 2    # только с ящика 2
    python mailer.py letters_1.csv --status              # сколько отправлено и осталось, и сколько можно с каждого ящика
    python mailer.py "<папка данных>" --status           # то же без таблицы: только квоты ящиков на сегодня

Доступ к ящикам: MAILRU_USER и MAILRU_APP_PASSWORD (первый ящик), MAILRU_USER_2 и MAILRU_APP_PASSWORD_2 (второй) и т. д.
из окружения или из local.env рядом с программой. Письма идут по очереди с подключённых ящиков, каждому по его квоте на
сегодня (прогрев: 7 писем в первый день, потом на 3 больше, не больше лимита в сутки).
Защита: без --send ничего не уходит; --limit за запуск, --pause (со случайной добавкой --jitter) между письмами,
--daily-limit в сутки на ящик; журнал <таблица>.sent.json (повторно не шлём); stoplist.txt; List-Unsubscribe; одна отправка
на папку (два ящика работают в одном процессе, по очереди); при отказе сервера или лимите сразу остановка.
Коды выхода: 0 готово, 1 ошибка в таблице или настройках, 2 не принят логин или пароль, 3 отказ или лимит сервера,
4 остановлено вами, 5 несколько временных отказов.
"""
import argparse
import dataclasses
import os
import random
import smtplib
import subprocess
import sys
import time
from collections import Counter

from mailing import journal, mailboxes as boxes
from mailing.config import (
    ENV_FILE, SETTINGS_FILE, DEFAULT_DAILY_LIMIT, check_email, is_stopped, load_env_file, load_settings, mask_email,
    read_stoplist, redact, resolve_daily_limit)
from mailing.lock import Locked, SendLock, lock_holder
from mailing.smtp import SMTP_PORT, MailruSender, build_message, reply_text
from mailing.table import LettersError, read_letters

LOG_NAME = "mailer.log"          # журнал фоновой отправки (адреса в нём скрыты)
DEFAULT_JITTER = 1.5             # пауза от --pause до --pause * (1 + 1.5): 30-75 секунд при --pause 30


def mailbox_lines(folder: str, mailboxes, daily_limit: int, settings_path: str = "", now=None) -> list:
    """По строке на ящик: сколько он может отправить сегодня (прогрев и лимит в сутки). Адреса скрыты, паролей нет."""
    settings = load_settings(settings_path)
    lines = []
    for box in mailboxes:
        state = boxes.mailbox_state(folder, box.id, settings, daily_limit, now)
        first = (f"; первое письмо {state['first_message']} ({state['source']})" if state["first_message"]
                 else "; с ящика ещё ничего не отправляли (день 1 - день первого письма)")
        lines.append(f"{box.title} ({mask_email(box.user)}): {boxes.describe_state(state)}{first}.")
    return lines


def status_lines(path: str, daily_limit: int, now=None, mailboxes=None, settings_path: str = "") -> list:
    """Сводка по таблице и журналу без адресов: сколько отправлено, осталось, идёт ли отправка и что можно с каждого ящика."""
    letters, problems = read_letters(path)
    state = journal.load_state(journal.state_path(path))
    folder = os.path.dirname(os.path.abspath(path))
    counts = {kind: sum(1 for l in letters if state.get(l.email.lower(), {}).get("status") == kind)
              for kind in (journal.SENT, journal.REJECTED)}
    pending = sum(1 for l in letters if l.email.lower() not in state)
    used = journal.sent_last_24h(folder, now)
    holder = lock_holder(folder)
    lines = [f"Писем в таблице: {len(letters)}; отправлено: {counts[journal.SENT]}; адрес не принят: {counts[journal.REJECTED]}; "
             f"ещё не отправлялось: {pending}.",
             f"За последние 24 часа по папке отправлено {used}" + (f" из {daily_limit}." if daily_limit else "."),
             f"Сейчас идёт отправка (процесс {holder})." if holder else "Сейчас отправка не идёт."]
    if mailboxes:
        lines.append("Ящики на сегодня:")
        lines.extend("  " + line for line in mailbox_lines(folder, mailboxes, daily_limit, settings_path, now))
    try:
        with open(os.path.join(folder, LOG_NAME), encoding="utf-8", errors="replace") as fh:
            tail = [line.rstrip() for line in fh.readlines()[-5:]]
        if tail:
            lines.append("Последние строки " + LOG_NAME + ":")
            lines.extend("  " + line for line in tail)
    except OSError:
        pass
    return lines

def run(path: str, send: bool = False, limit: int = 20, pause: float = 30.0, user: str = "",
        password: str = "", from_name: str = "", port: int = SMTP_PORT, sender=None, log=print,
        sleep=time.sleep, max_temp_errors: int = 3, stoplist=frozenset(), daily_limit: int = 0,
        unsubscribe: bool = False, now=None, mask: bool = False, mailboxes=None, senders=None, configured=None,
        jitter: float = 0.0, rng=random, settings_path: str = "") -> int:
    """Возвращает код выхода: 0 - готово, 1 - ошибка в таблице или настройках,
    2 - mail.ru не принял вход (логин/пароль), 3 - mail.ru отказал или ограничил отправку,
    5 - несколько временных отказов по получателям подряд. mask - в сообщениях адреса скрыты (n***@mail.ru).

    Без mailboxes работает один ящик (sender или user/password), без прогрева: только лимит в сутки.
    С mailboxes (список mailing.mailboxes.Mailbox, выбранные для этой отправки) письма идут по очереди с этих ящиков,
    каждому по его квоте на сегодня (прогрев и лимит в сутки); senders - {номер ящика: объект отправки}, иначе они
    создаются по паролям; configured - все подключённые ящики (чтобы понять, чей «свой» ящик у дожима)."""
    who = mask_email if mask else (lambda address: address)
    shown = redact if mask else (lambda text: text)
    try:
        letters, problems = read_letters(path)
    except LettersError as exc:
        log(f"Ошибка: {exc}")
        return 1
    for number, reason in problems:
        log(f"  строка {number}: {reason} - пропускаем")

    legacy = mailboxes is None                 # один ящик без прогрева: прежнее поведение
    if legacy:
        slots = [boxes.Mailbox("1", getattr(sender, "user", "") or user, password, from_name)]
        senders = {"1": sender} if sender is not None else None
    else:
        slots = list(mailboxes)
    multi = len(slots) > 1
    folder = os.path.dirname(os.path.abspath(path))
    settings = load_settings(settings_path)

    state_file = journal.state_path(path)
    state = journal.load_state(state_file)
    todo = [letter for letter in letters if letter.email.lower() not in state]
    skipped_done = len(letters) - len(todo)
    stopped = [letter for letter in todo if is_stopped(letter.email, stoplist)]
    if stopped:
        todo = [letter for letter in todo if not is_stopped(letter.email, stoplist)]
        log(f"В стоп-листе: {len(stopped)} адресов, им письма не уйдут.")

    states = {box.id: boxes.mailbox_state(folder, box.id, settings, daily_limit, now, warmup=not legacy) for box in slots}
    if legacy:
        if daily_limit:
            used = states["1"]["sent_24h"]
            log(f"Дневной лимит {daily_limit}: за последние 24 часа уже отправлено {used}, "
                f"можно ещё {max(0, daily_limit - used)}.")
    else:
        for box in slots:
            log(f"{box.title}: {boxes.describe_state(states[box.id])}.")

    preferred = {}
    if not legacy and journal.stage_of(path) > 1:           # дожим и закрывающее идут с того же ящика, что и первое письмо
        known = {box.id for box in (configured if configured is not None else slots)}
        preferred = {address: own for address, own in journal.previous_mailboxes(folder, journal.stage_of(path)).items()
                     if own in known}
    plan, waiting = boxes.assign(todo, {box.id: states[box.id]["room"] for box in slots}, preferred, limit)
    for own, number in sorted(waiting.items()):
        log(f"  ждут своего ящика {own}: {number} (дожим идёт с того же ящика, что и первое письмо; у него нет места "
            "на сегодня или он не выбран для этой отправки)")
    log(f"Писем в таблице: {len(letters)}; уже обработано раньше: {skipped_done}; "
        f"в этом запуске: {len(plan)}; строк с ошибками: {len(problems)}.")
    labels = {box.id: box.label for box in slots}
    if multi and plan:
        per_box = Counter(own for _, own in plan)
        log("Раздача по ящикам: " + ", ".join(f"{labels[own]} - {per_box[own]}" for own in labels if per_box[own]) + ".")

    if not send:
        for number, (letter, own) in enumerate(plan, 1):
            log(f"  {number}. " + (f"[{labels[own]}] " if multi else "") + f"{who(letter.email)} - «{letter.subject}»")
        log("Проверка без отправки: ничего не ушло. Чтобы отправить, добавьте --send.")
        return 1 if problems else 0

    if not plan:
        if not legacy and todo and not any(states[box.id]["room"] for box in slots):
            log("Квота ящиков на сегодня исчерпана: остаток уйдёт в следующие дни той же командой.")
        log("Отправлять нечего.")
        return 0
    if senders is None:
        senders = {}
        if legacy:
            if not user or not password:
                log("Не заданы MAILRU_USER и MAILRU_APP_PASSWORD (адрес ящика и «пароль для внешних приложений»). "
                    "Впишите их в файл local.env рядом с mailer.py (см. LOCAL.md) или задайте переменными окружения.")
                return 1
            if not check_email(user):
                log("MAILRU_USER должен быть полным адресом ящика, например name@mail.ru.")
                return 1
        for box in slots:
            if not box.ready:
                log(f"{box.label}: не заданы адрес и «пароль для внешних приложений» (см. local.env.example).")
                return 1
            senders[box.id] = MailruSender(box.user, box.password, port=port)
    names = {box.id: box.from_name for box in slots}

    draw = getattr(rng, "random", rng)           # rng - либо объект с .random() (random, random.Random), либо функция без аргументов

    def pause_for() -> float:
        return pause * (1 + draw() * jitter) if jitter else pause           # ровная пауза похожа на программу

    code, temp_in_a_row, done, done_by = 0, 0, 0, Counter()
    try:
        for number, (letter, own) in enumerate(plan, 1):
            if number > 1:
                sleep(pause_for())                  # пауза между любыми двумя письмами: репутация ящиков важнее скорости
            current, note = senders[own], f" ({labels[own]})" if multi else ""
            try:
                current.send(build_message(letter, current.user, names[own], unsubscribe))
            except smtplib.SMTPAuthenticationError as exc:
                log(f"mail.ru не принял логин/пароль{note} ({shown(reply_text(exc))}). Нужен «пароль для внешних приложений», "
                    "а не обычный пароль от ящика. Остановлено.")
                code = 2
                break
            except smtplib.SMTPRecipientsRefused as exc:
                replies = list(exc.recipients.values())
                permanent = all(str(reply[0]).startswith("5") for reply in replies)
                text = reply_text(exc) if not replies else f"{replies[0][0]} " + replies[0][1].decode("utf-8", "replace")[:150]
                if permanent:
                    state[letter.email.lower()] = journal.make_entry(journal.REJECTED, letter.subject, letter.meta, text,
                                                                     "" if legacy else own)
                    journal.save_state(state_file, state)
                    log(f"[{number}/{len(plan)}] {who(letter.email)}: адрес не принят ({shown(text)}){note} - пропускаем")
                    temp_in_a_row = 0
                else:
                    temp_in_a_row += 1
                    log(f"[{number}/{len(plan)}] {who(letter.email)}: временный отказ ({shown(text)}){note} - "
                        "попробуем в следующий запуск")
                    if temp_in_a_row >= max_temp_errors:
                        log(f"{temp_in_a_row} временных отказов подряд - останавливаемся. Повторите позже.")
                        code = 5
                        break
                continue
            except (smtplib.SMTPException, OSError) as exc:
                log(f"mail.ru не принял письмо или оборвал связь{note} ({shown(reply_text(exc))}). Останавливаемся: "
                    "если это лимит отправки (например 451 Ratelimit exceeded), продолжите позже, "
                    "а следующий запуск начнёт с этого письма.")
                code = 3
                break
            state[letter.email.lower()] = journal.make_entry(journal.SENT, letter.subject, letter.meta, "",
                                                             "" if legacy else own)
            journal.save_state(state_file, state)
            done += 1
            done_by[own] += 1
            temp_in_a_row = 0
            log(f"[{number}/{len(plan)}] {who(letter.email)}: отправлено{note}")
    finally:
        for current in senders.values():
            current.close()

    sent_total = sum(1 for entry in state.values() if entry["status"] == journal.SENT)
    log(f"Готово. Отправлено в этом запуске: {done}; всего по журналу {state_file}: {sent_total}.")
    if multi and done:
        log("По ящикам: " + ", ".join(f"{labels[own]} - {done_by[own]}" for own in labels if done_by[own]) + ".")
    return code

def start_background(args, argv, user: str, password: str, selected=None) -> int:
    """Запускает ту же отправку отдельным процессом и сразу возвращает управление. Всё проверяется заранее,
    чтобы ошибка была видна сразу, а не пропала в журнале."""
    if not args.send:
        print("Ошибка: --background работает только вместе с --send.")
        return 1
    try:
        letters, _ = read_letters(args.letters)
    except LettersError as exc:
        print(f"Ошибка: {exc}")
        return 1
    if selected is None and (not user or not password or not check_email(user)):
        print("Ошибка: не заданы MAILRU_USER и MAILRU_APP_PASSWORD (local.env рядом с mailer.py). Отправка не запущена.")
        return 1
    folder = os.path.dirname(os.path.abspath(args.letters))
    holder = lock_holder(folder)
    if holder:
        print(f"Ошибка: в этой папке уже идёт отправка (процесс {holder}). Состояние: --status.")
        return 1
    command = [sys.executable, os.path.abspath(__file__)] + [a for a in argv if a != "--background"]
    if "--mask-addresses" not in command:
        command.append("--mask-addresses")                       # в журнале фоновой отправки адресов нет
    log_path = os.path.join(folder, LOG_NAME)
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write(f"--- запуск {journal.now_iso()} ---\n")
    log_file = open(log_path, "ab")
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": log_file, "stderr": subprocess.STDOUT, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    kwargs["env"] = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")   # кириллица и сразу в журнал
    process = subprocess.Popen(command, **kwargs)
    log_file.close()
    pause_text = f"{args.pause:g}" + (f"-{args.pause * (1 + args.jitter):g}" if args.jitter else "")
    where = f", ящиков: {len(selected)}" if selected and len(selected) > 1 else ""
    print(f"Отправка запущена в фоне (процесс {process.pid}): писем в таблице {len(letters)}, за запуск не больше "
          f"{args.limit or 'всех'}{where}, между письмами пауза {pause_text} с. Ждать не нужно. "
          f"Состояние: python mailer.py \"{args.letters}\" --status ; журнал: {log_path}")
    return 0

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="Отправка писем из таблицы через mail.ru (SMTP)")
    parser.add_argument("letters", help="CSV с колонками «Почта», «Тема», «Текст» (для --status можно указать папку данных)")
    parser.add_argument("--send", action="store_true",
                        help="реально отправить (без этого ключа - только проверка таблицы)")
    parser.add_argument("--background", action="store_true",
                        help="с --send: запустить отправку в фоне и сразу вернуться (ждать окончания не нужно; "
                             "состояние - ключ --status)")
    parser.add_argument("--status", action="store_true",
                        help="показать, сколько отправлено и осталось, идёт ли отправка и сколько можно отправить с каждого "
                             "ящика сегодня; ничего не отправляет")
    parser.add_argument("--mask-addresses", action="store_true",
                        help="в сообщениях скрывать адреса (n***@mail.ru); при --background включено само")
    parser.add_argument("--limit", type=int, default=20,
                        help="сколько писем отправить за запуск (по умолчанию 20; 0 - все)")
    parser.add_argument("--mailboxes", type=int, default=None,
                        help="с какого числа ящиков отправлять (первые N по порядку номеров); по умолчанию со всех "
                             "подключённых (сейчас максимум), больше подключённых не бывает")
    parser.add_argument("--mailbox", default=None,
                        help="отправлять только с этого ящика: номер (2) или несколько через запятую (1,2); "
                             "нельзя вместе с --mailboxes")
    parser.add_argument("--pause", type=float, default=30.0, help="пауза между письмами, с (по умолчанию 30)")
    parser.add_argument("--jitter", type=float, default=DEFAULT_JITTER,
                        help="случайная добавка к паузе: пауза от --pause до --pause * (1 + --jitter) секунд "
                             f"(по умолчанию {DEFAULT_JITTER:g}, то есть 30-75 с; 0 - ровная пауза, она похожа на программу)")
    parser.add_argument("--from-name", default="",
                        help="имя отправителя в поле «От» (по умолчанию MAIL_FROM_NAME из local.env; одно для всех ящиков)")
    parser.add_argument("--port", type=int, default=SMTP_PORT, choices=(465, 587),
                        help="порт smtp.mail.ru: 465 (SSL, по умолчанию) или 587 (STARTTLS)")
    parser.add_argument("--daily-limit", type=int, default=None,
                        help="не больше писем за последние 24 часа на каждый почтовый ящик, считая по всем таблицам "
                             "letters*.sent.json в папке (по умолчанию значение, заданное на странице /mail сайта, затем "
                             f"MAIL_DAILY_LIMIT из local.env, иначе {DEFAULT_DAILY_LIMIT}); новому ящику прогрев даёт ещё "
                             "меньше: 7 писем в первый день, потом на 3 больше; 0 - без лимита)")
    parser.add_argument("--settings", default=SETTINGS_FILE, help="файл настроек сайта (settings.json рядом с программой)")
    parser.add_argument("--stoplist", default=None,
                        help="файл стоп-листа: адрес или @домен в строке (по умолчанию stoplist.txt рядом с таблицей, "
                             "если он есть)")
    parser.add_argument("--env", default=ENV_FILE,
                        help="файл с MAILRU_USER и MAILRU_APP_PASSWORD (по умолчанию local.env рядом с mailer.py)")
    args = parser.parse_args(argv)
    if args.pause < 0 or args.jitter < 0:
        print("Ошибка: --pause и --jitter не могут быть отрицательными.")
        return 1
    try:
        load_env_file(args.env)
    except (OSError, ValueError) as exc:
        print(f"Ошибка: не удалось прочитать {args.env}: {getattr(exc, 'strerror', None) or exc}")
        return 1
    try:
        daily_limit, _ = resolve_daily_limit(args.daily_limit, args.settings)
    except ValueError:
        print("Ошибка: MAIL_DAILY_LIMIT в local.env должно быть целым числом.")
        return 1

    # ящики: первый - MAILRU_USER, второй - MAILRU_USER_2 и так далее; ни одного нет - работает прежний путь с одним ящиком
    configured = boxes.load_mailboxes()
    ready = [box for box in configured if box.ready]
    selected = None
    if args.send or args.background:
        issues = boxes.problems(configured)
        if issues:
            print("Ошибка: " + " ".join(issues))
            return 1
    if ready or args.mailboxes is not None or args.mailbox:
        try:
            selected = boxes.select(configured, args.mailboxes, args.mailbox)
        except ValueError as exc:
            print(f"Ошибка: {exc}")
            return 1
        if args.from_name:
            selected = [dataclasses.replace(box, from_name=args.from_name) for box in selected]

    if os.path.isdir(args.letters):                 # папка данных вместо таблицы: только квоты ящиков
        if not args.status:
            print("Ошибка: вместо таблицы папку можно указать только вместе с --status.")
            return 1
        print("\n".join(mailbox_lines(os.path.abspath(args.letters), ready, daily_limit, args.settings)
                        or ["Ящики не подключены: впишите MAILRU_USER и MAILRU_APP_PASSWORD в local.env."]))
        return 0

    stop_path = args.stoplist or os.path.join(os.path.dirname(os.path.abspath(args.letters)), "stoplist.txt")
    try:
        stoplist = read_stoplist(stop_path)
    except FileNotFoundError:
        if args.stoplist:
            print(f"Ошибка: нет файла стоп-листа {args.stoplist}")
            return 1
        stoplist = frozenset()                  # файла нет - стоп-лист пока пуст
    except (OSError, ValueError) as exc:
        print(f"Ошибка: не удалось прочитать стоп-лист {stop_path}: {getattr(exc, 'strerror', None) or exc}")
        return 1

    user, password = os.environ.get("MAILRU_USER", ""), os.environ.get("MAILRU_APP_PASSWORD", "")
    if args.status:
        try:
            print("\n".join(status_lines(args.letters, daily_limit, mailboxes=ready, settings_path=args.settings)))
        except LettersError as exc:
            print(f"Ошибка: {exc}")
            return 1
        return 0
    if args.background:
        return start_background(args, argv, user, password, selected)

    def go():
        return run(args.letters, args.send, args.limit, args.pause, user, password,
                   args.from_name or os.environ.get("MAIL_FROM_NAME", ""), args.port,
                   stoplist=stoplist, daily_limit=daily_limit, unsubscribe=True, mask=args.mask_addresses,
                   mailboxes=selected, configured=ready, jitter=args.jitter, settings_path=args.settings)

    try:
        if not args.send:
            return go()
        with SendLock(os.path.dirname(os.path.abspath(args.letters))):
            return go()
    except Locked as exc:
        print(f"Ошибка: {exc}. Две отправки одновременно нарушили бы паузы и дневной лимит.")
        return 1
    except KeyboardInterrupt:
        print("Остановлено вами. Отправленное записано в журнал.")
        return 4


if __name__ == "__main__":
    sys.exit(main())
