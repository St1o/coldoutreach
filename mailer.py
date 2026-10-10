"""Отправка готовых писем из таблицы через mail.ru (SMTP). Подробно: LOCAL.md, раздел «Рассылка».

    python mailer.py letters_1.csv                       # проверка, ничего не уходит
    python mailer.py letters_1.csv --send --limit 5      # отправить первые 5
    python mailer.py letters_1.csv --send --background   # то же в фоне, команда сразу возвращается
    python mailer.py letters_1.csv --status              # сколько отправлено и осталось

Доступ к ящику: MAILRU_USER и MAILRU_APP_PASSWORD из окружения или из local.env рядом с программой.
Защита: без --send ничего не уходит; --limit за запуск, --pause между письмами, --daily-limit в сутки;
журнал <таблица>.sent.json (повторно не шлём); stoplist.txt; List-Unsubscribe; одна отправка на папку;
при отказе сервера или лимите сразу остановка. Коды выхода: 0 готово, 1 ошибка в таблице или настройках,
2 не принят логин или пароль, 3 отказ или лимит сервера, 4 остановлено вами, 5 несколько временных отказов.
"""
import argparse
import os
import smtplib
import subprocess
import sys
import time

from mailing import journal
from mailing.config import (
    ENV_FILE, SETTINGS_FILE, DEFAULT_DAILY_LIMIT, check_email, is_stopped, load_env_file, mask_email, read_stoplist,
    redact, resolve_daily_limit)
from mailing.lock import Locked, SendLock, lock_holder
from mailing.smtp import SMTP_PORT, MailruSender, build_message, reply_text
from mailing.table import LettersError, read_letters

LOG_NAME = "mailer.log"          # журнал фоновой отправки (адреса в нём скрыты)


def status_lines(path: str, daily_limit: int, now=None) -> list:
    """Сводка по таблице и журналу без адресов: сколько отправлено, осталось, идёт ли отправка."""
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
        unsubscribe: bool = False, now=None, mask: bool = False) -> int:
    """Возвращает код выхода: 0 - готово, 1 - ошибка в таблице или настройках,
    2 - mail.ru не принял вход (логин/пароль), 3 - mail.ru отказал или ограничил отправку,
    5 - несколько временных отказов по получателям подряд. mask - в сообщениях адреса скрыты (n***@mail.ru)."""
    who = mask_email if mask else (lambda address: address)
    shown = redact if mask else (lambda text: text)
    try:
        letters, problems = read_letters(path)
    except LettersError as exc:
        log(f"Ошибка: {exc}")
        return 1
    for number, reason in problems:
        log(f"  строка {number}: {reason} - пропускаем")

    state_file = journal.state_path(path)
    state = journal.load_state(state_file)
    todo = [letter for letter in letters if letter.email.lower() not in state]
    skipped_done = len(letters) - len(todo)
    stopped = [letter for letter in todo if is_stopped(letter.email, stoplist)]
    if stopped:
        todo = [letter for letter in todo if not is_stopped(letter.email, stoplist)]
        log(f"В стоп-листе: {len(stopped)} адресов, им письма не уйдут.")
    if limit:
        todo = todo[:limit]
    if daily_limit:
        used = journal.sent_last_24h(os.path.dirname(os.path.abspath(path)), now)
        room = max(0, daily_limit - used)
        log(f"Дневной лимит {daily_limit}: за последние 24 часа уже отправлено {used}, можно ещё {room}.")
        todo = todo[:room]
    log(f"Писем в таблице: {len(letters)}; уже обработано раньше: {skipped_done}; "
        f"в этом запуске: {len(todo)}; строк с ошибками: {len(problems)}.")

    if not send:
        for number, letter in enumerate(todo, 1):
            log(f"  {number}. {who(letter.email)} - «{letter.subject}»")
        log("Проверка без отправки: ничего не ушло. Чтобы отправить, добавьте --send.")
        return 1 if problems else 0

    if not todo:
        log("Отправлять нечего.")
        return 0
    if sender is None:
        if not user or not password:
            log("Не заданы MAILRU_USER и MAILRU_APP_PASSWORD (адрес ящика и «пароль для внешних приложений»). "
                "Впишите их в файл local.env рядом с mailer.py (см. LOCAL.md) или задайте переменными окружения.")
            return 1
        if not check_email(user):
            log("MAILRU_USER должен быть полным адресом ящика, например name@mail.ru.")
            return 1
        sender = MailruSender(user, password, port=port)

    code, temp_in_a_row, done = 0, 0, 0
    try:
        for number, letter in enumerate(todo, 1):
            if number > 1:
                sleep(pause)                    # пауза между письмами: репутация ящика важнее скорости
            try:
                sender.send(build_message(letter, sender.user, from_name, unsubscribe))
            except smtplib.SMTPAuthenticationError as exc:
                log(f"mail.ru не принял логин/пароль ({shown(reply_text(exc))}). Нужен «пароль для внешних приложений», "
                    "а не обычный пароль от ящика. Остановлено.")
                code = 2
                break
            except smtplib.SMTPRecipientsRefused as exc:
                replies = list(exc.recipients.values())
                permanent = all(str(reply[0]).startswith("5") for reply in replies)
                note = reply_text(exc) if not replies else f"{replies[0][0]} " + replies[0][1].decode("utf-8", "replace")[:150]
                if permanent:
                    state[letter.email.lower()] = journal.make_entry(journal.REJECTED, letter.subject, letter.meta, note)
                    journal.save_state(state_file, state)
                    log(f"[{number}/{len(todo)}] {who(letter.email)}: адрес не принят ({shown(note)}) - пропускаем")
                    temp_in_a_row = 0
                else:
                    temp_in_a_row += 1
                    log(f"[{number}/{len(todo)}] {who(letter.email)}: временный отказ ({shown(note)}) - попробуем в следующий запуск")
                    if temp_in_a_row >= max_temp_errors:
                        log(f"{temp_in_a_row} временных отказов подряд - останавливаемся. Повторите позже.")
                        code = 5
                        break
                continue
            except (smtplib.SMTPException, OSError) as exc:
                log(f"mail.ru не принял письмо или оборвал связь ({shown(reply_text(exc))}). Останавливаемся: "
                    "если это лимит отправки (например 451 Ratelimit exceeded), продолжите позже, "
                    "а следующий запуск начнёт с этого письма.")
                code = 3
                break
            state[letter.email.lower()] = journal.make_entry(journal.SENT, letter.subject, letter.meta)
            journal.save_state(state_file, state)
            done += 1
            temp_in_a_row = 0
            log(f"[{number}/{len(todo)}] {who(letter.email)}: отправлено")
    finally:
        sender.close()

    sent_total = sum(1 for entry in state.values() if entry["status"] == journal.SENT)
    log(f"Готово. Отправлено в этом запуске: {done}; всего по журналу {state_file}: {sent_total}.")
    return code

def start_background(args, argv, user: str, password: str) -> int:
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
    if not user or not password or not check_email(user):
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
    print(f"Отправка запущена в фоне (процесс {process.pid}): писем в таблице {len(letters)}, за запуск не больше "
          f"{args.limit or 'всех'}, между письмами пауза {args.pause:g} с. Ждать не нужно. "
          f"Состояние: python mailer.py \"{args.letters}\" --status ; журнал: {log_path}")
    return 0

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="Отправка писем из таблицы через mail.ru (SMTP)")
    parser.add_argument("letters", help="CSV с колонками «Почта», «Тема», «Текст»")
    parser.add_argument("--send", action="store_true",
                        help="реально отправить (без этого ключа - только проверка таблицы)")
    parser.add_argument("--background", action="store_true",
                        help="с --send: запустить отправку в фоне и сразу вернуться (ждать окончания не нужно; "
                             "состояние - ключ --status)")
    parser.add_argument("--status", action="store_true",
                        help="показать, сколько отправлено и осталось и идёт ли отправка; ничего не отправляет")
    parser.add_argument("--mask-addresses", action="store_true",
                        help="в сообщениях скрывать адреса (n***@mail.ru); при --background включено само")
    parser.add_argument("--limit", type=int, default=20,
                        help="сколько писем отправить за запуск (по умолчанию 20; 0 - все)")
    parser.add_argument("--pause", type=float, default=30.0, help="пауза между письмами, с (по умолчанию 30)")
    parser.add_argument("--from-name", default="",
                        help="имя отправителя в поле «От» (по умолчанию MAIL_FROM_NAME из local.env)")
    parser.add_argument("--port", type=int, default=SMTP_PORT, choices=(465, 587),
                        help="порт smtp.mail.ru: 465 (SSL, по умолчанию) или 587 (STARTTLS)")
    parser.add_argument("--daily-limit", type=int, default=None,
                        help="не больше писем за последние 24 часа по всем таблицам letters*.sent.json в папке, на один "
                             "почтовый ящик (по умолчанию значение, заданное на странице /mail сайта, затем "
                             f"MAIL_DAILY_LIMIT из local.env, иначе {DEFAULT_DAILY_LIMIT}; для нового ящика правила "
                             "рассылки советуют 30-50 и меньше в первые дни прогрева; 0 - без лимита)")
    parser.add_argument("--settings", default=SETTINGS_FILE, help="файл настроек сайта (settings.json рядом с программой)")
    parser.add_argument("--stoplist", default=None,
                        help="файл стоп-листа: адрес или @домен в строке (по умолчанию stoplist.txt рядом с таблицей, "
                             "если он есть)")
    parser.add_argument("--env", default=ENV_FILE,
                        help="файл с MAILRU_USER и MAILRU_APP_PASSWORD (по умолчанию local.env рядом с mailer.py)")
    args = parser.parse_args(argv)
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
            print("\n".join(status_lines(args.letters, daily_limit)))
        except LettersError as exc:
            print(f"Ошибка: {exc}")
            return 1
        return 0
    if args.background:
        return start_background(args, argv, user, password)

    def go():
        return run(args.letters, args.send, args.limit, args.pause, user, password,
                   args.from_name or os.environ.get("MAIL_FROM_NAME", ""), args.port,
                   stoplist=stoplist, daily_limit=daily_limit, unsubscribe=True, mask=args.mask_addresses)

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
