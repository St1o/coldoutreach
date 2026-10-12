"""Какая версия программ рассылки стоит в этой папке. Запуск: python check_version.py (или правый клик -> Run 'check_version').

Только чтение: ничего не меняет, адреса и пароли не печатает (local.env программа читает сама, как mailer.py, но выводит только
число ящиков). Печатает ветку и коммит git, чистая ли папка, не отстаёт ли ветка от origin (по последнему fetch), есть ли нужные
возможности (ключи --mailboxes, --mailbox, --jitter, --status, прогрев с недельными пределами, дата первого письма в local.env,
копия отправленного письма в «Отправленные»)
и их числа. Итог: «ВЕРСИЯ ОК» (код 0) или список того, чего не хватает (код 1: тогда отправлять нельзя, сообщите владельцу).
"""
import contextlib
import inspect
import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def git(*args):
    """Вывод команды git в этой папке или None (нет git, не репозиторий, ошибка). Меняющих команд здесь нет."""
    try:
        done = subprocess.run(["git", "-C", HERE, *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def git_info() -> dict:
    """Ветка, коммит, изменённые файлы (только имена) и отставание от origin; None вместо значения, если узнать нельзя."""
    status = git("status", "--short")
    changed = [line[3:].strip() for line in status.splitlines()] if status else ([] if status == "" else None)
    behind = ahead = None
    counts = git("rev-list", "--left-right", "--count", "HEAD...@{u}")
    if counts:
        ahead, behind = (int(part) for part in counts.split()[:2])
    return {"branch": git("branch", "--show-current"), "commit": git("log", "-1", "--format=%h %s"),
            "changed": changed, "ahead": ahead, "behind": behind}


def features() -> list:
    """[(что проверяем, есть ли, подробность)]: наличие возможностей по коду, не по номеру версии."""
    import mailer
    from mailing import config, mailboxes
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            mailer.main(["--help"])
        except SystemExit:
            pass
    help_text = out.getvalue()
    result = []
    for key, pattern in (("--mailboxes N (с какого числа ящиков)", r"--mailboxes\b"), ("--mailbox ID (только с этих ящиков)", r"--mailbox(?!es)"),
                         ("--jitter (случайная пауза)", r"--jitter\b"), ("--status (в том числе по папке данных)", r"--status\b")):
        result.append((f"mailer.py: ключ {key}", bool(re.search(pattern, help_text)), ""))
    params = inspect.signature(mailer.run).parameters
    result.append(("mailer.run: ящики, разброс паузы, дожим с того же ящика", all(p in params for p in ("mailboxes", "jitter", "configured")), ""))
    for name in ("week_cap", "warmup_quota", "first_message_env", "assign", "set_first_message"):
        result.append((f"mailing.mailboxes.{name}", hasattr(mailboxes, name), ""))
    result.append(("прогрев: WARMUP_WEEK_CAPS, WARMUP_CAP_LATER", hasattr(mailboxes, "WARMUP_WEEK_CAPS") and hasattr(mailboxes, "WARMUP_CAP_LATER"),
                   f"первый день {getattr(mailboxes, 'WARMUP_FIRST_DAY', '?')}, шаг +{getattr(mailboxes, 'WARMUP_STEP', '?')}, "
                   f"недели {getattr(mailboxes, 'WARMUP_WEEK_CAPS', '?')}, дальше {getattr(mailboxes, 'WARMUP_CAP_LATER', '?')}; "
                   f"потолок /mail 1..{config.DAILY_LIMIT_MAX}; разброс паузы x(1+{getattr(mailer, 'DEFAULT_JITTER', '?')})"))
    from mailing import sentcopy
    result.append(("копия отправленного письма в «Отправленные» (mailing.sentcopy, SAVE_SENT_COPY)",
                   hasattr(sentcopy, "SentFolderSaver") and hasattr(config, "resolve_sent_copy") and "copiers" in params,
                   "сейчас включена" if config.resolve_sent_copy() else "сейчас выключена (SAVE_SENT_COPY=0)"))
    import send_service
    result.append(("send_service.mailbox_infos (квоты на страницах)", hasattr(send_service.SendService, "mailbox_infos"), ""))
    import compose
    result.append(("compose.py: правило «один домен — одно письмо» и проверка вариантов", hasattr(compose, "PUBLIC_DOMAINS") and hasattr(compose, "MIN_UNIQUE"), ""))
    return result


def mailbox_summary() -> str:
    """Сколько ящиков подключено (только числа и номера). local.env читается так же, как это делает mailer.py."""
    from mailing import config, mailboxes
    try:
        config.load_env_file(config.ENV_FILE)
    except (OSError, ValueError):
        return "local.env прочитать не удалось"
    found = mailboxes.load_mailboxes()
    ready = [box.id for box in found if box.ready]
    return f"подключено ящиков: {len(ready)} (номера: {', '.join(ready) or 'нет'})" + (
        f"; заполнены не полностью: {', '.join(box.id for box in found if not box.ready)}" if len(found) > len(ready) else "")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    print(f"Папка программ: {HERE}")
    print(f"Python {sys.version.split()[0]}: {sys.executable}")
    info = git_info()
    print(f"git: ветка {info['branch'] or '(неизвестна)'}, коммит {info['commit'] or '(неизвестен)'}")
    if info["changed"] is None:
        print("git: чистая ли папка, узнать не удалось")
    elif info["changed"]:
        print(f"git: ИЗМЕНЕНЫ файлы ({len(info['changed'])}): {', '.join(info['changed'][:12])}" + (" ..." if len(info["changed"]) > 12 else ""))
    else:
        print("git: рабочая папка чистая (файлы совпадают с коммитом)")
    if info["behind"] is None:
        print("git: сравнить с origin не удалось (нет ветки на сервере или не делали Fetch)")
    else:
        print(f"git: относительно origin (по последнему Fetch) позади на {info['behind']}, впереди на {info['ahead']}"
              + (" (нужен Update в PyCharm)" if info["behind"] else ""))
    print(f"local.env - {'есть' if os.path.exists(os.path.join(HERE, 'local.env')) else 'нет'}, "
          f"settings.json - {'есть' if os.path.exists(os.path.join(HERE, 'settings.json')) else 'нет'} (содержимое не показывается)")
    print(mailbox_summary())
    missing = []
    print("\nВозможности:")
    for name, ok, detail in features():
        print(f"  [{'есть' if ok else 'НЕТ '}] {name}" + (f": {detail}" if detail else ""))
        if not ok:
            missing.append(name)
    print("\nИТОГ: " + ("ВЕРСИЯ ОК." if not missing else "СТАРАЯ ИЛИ НЕПОЛНАЯ ВЕРСИЯ, отправлять нельзя: не хватает " + "; ".join(missing) + "."))
    if info["changed"] and not missing:
        print("Замечание: в папке есть изменённые файлы; сверьте с владельцем, что это его правки.")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
