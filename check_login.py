"""Почему «mail.ru не принял логин/пароль». Запуск: правый клик на файле -> Run 'check_login' (или python check_login.py).

Письма НЕ отправляются: проверяется только вход в SMTP, по ОДНОЙ попытке на ящик (повторные неудачные входы могут заблокировать
ящик). Сначала контрольный ящик 2, потом ящик 1; отказ на ящике 2 значит, что причина не в ящике 1, и ящик 1 не трогается.
Вход делает одна команда AUTH PLAIN (smtplib.login() при отказе пробует ещё и LOGIN: два отказа за раз). Журнал попыток
check_login.json не даёт повторить вход на тот же ящик в течение часа (ключ --again снимает защиту).

Печатает только производные признаки (длины, да/нет, адрес в виде c***@mail.ru) и ответ сервера. Пароли и адреса целиком не
печатаются и не пишутся в файлы. Отчёт сохраняется в check_login.txt (в git не попадает): пришлите его или вывод из окна Run.
Что смотрит: формат значений в local.env (дубли ключей, пробелы, `# комментарий` в значении, кавычки, кириллица, похожая на
латиницу), перекрытия local.env (переменные окружения, настройки запуска PyCharm, другие копии программы, процессы, запущенные
до последнего сохранения local.env), затем вход. Ключ --no-login: только проверка файлов, в сеть не ходит.
Код выхода: 0 - входы приняты или входов не было, 2 - отказ логина/пароля, 3 - связь/шифрование, 1 - ошибка запуска.
"""
import argparse
import datetime
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from mailing import config, mailboxes, smtp          # noqa: E402  (после sys.path)

LEDGER = os.path.join(HERE, "check_login.json")
REPORT = os.path.join(HERE, "check_login.txt")
MAILRU_DOMAINS = ("mail.ru", "inbox.ru", "list.ru", "bk.ru", "internet.ru")
LOOKALIKES = set("аАвВеЕкКмМнНоОрРсСтТуУхХіІјЈѕЅ")        # кириллица, внешне неотличимая от латиницы
SKIP_DIRS = {".git", "__pycache__", "node_modules", "site-packages", "Lib", "Scripts", "venv", ".venv", "1venv"}
HOUR = 3600.0
CONTROL, MAIN = 2, 1                                     # контрольный ящик и ящик, который не входит


class Report:
    def __init__(self, quiet=False):
        self.lines, self.quiet = [], quiet

    def __call__(self, text=""):
        self.lines.append(text)
        if not self.quiet:
            print(text)


def yn(flag) -> str:
    return "да" if flag else "нет"


def scrub(text, secrets) -> str:
    text = str(text)
    for secret in sorted((s for s in secrets if s and len(s) >= 3), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


# ---------------------------------------------------------------- local.env ----

def watched_keys() -> set:
    keys = {"MAILRU_PORT", "MAILRU_CA_FILE"}
    for number in range(1, mailboxes.MAX_MAILBOXES + 1):
        keys.update(mailboxes.env_names(number))
    return keys


def _norm(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "", key).upper()


def scan_env(path: str, keys) -> dict:
    """Построчный разбор local.env по правилам config.read_env_file плюс то, что программа молча пропускает:
    defs {ключ: [(строка, значение как в файле)]}, commented {ключ: [строки]}, near [(строка, ключ, почему не подходит)]."""
    with open(path, "rb") as fh:
        raw = fh.read()
    info = {"size": len(raw), "mtime": os.path.getmtime(path), "defs": {}, "commented": {}, "near": [],
            "utf16": raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw, "bom": raw.startswith(b"\xef\xbb\xbf")}
    for number, line in enumerate(raw.decode("utf-8-sig", "replace").splitlines(), 1):
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            found = re.match(r"#+\s*([A-Za-z0-9_]+)\s*=", text)
            if found and found.group(1) in keys:
                info["commented"].setdefault(found.group(1), []).append(number)
            continue
        key, sep, value = text.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key in keys:
            info["defs"].setdefault(key, []).append((number, value))
            continue
        exported = key.lower().startswith("export ")
        bare = key[7:].strip() if exported else key
        if _norm(bare) in keys:
            why = (["префикс export, программа его не понимает"] if exported else []) \
                + (["регистр букв"] if bare != bare.upper() else []) \
                + (["посторонние знаки в имени"] if re.search(r"[^A-Za-z0-9_]", bare) else [])
            info["near"].append((number, _norm(bare), "; ".join(why) or "имя не совпадает точно"))
    return info


def value_facts(raw_value: str) -> dict:
    """Признаки значения без самого значения (raw_value - всё, что после первого = в строке файла)."""
    stripped = raw_value.strip()
    quoted = len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'"
    value = stripped[1:-1] if quoted else stripped
    return {"value": value, "length": len(value), "spaces": value.count(" "), "tabs": value.count("\t"),
            "quotes": sum(value.count(q) for q in "\"'`"),
            "one_sided_quote": (not quoted) and bool(stripped) and (stripped[0] in "\"'" or stripped[-1] in "\"'"),
            "comment": bool(re.search(r"\s#", value)),
            "non_ascii": sum(1 for ch in value if ord(ch) > 127), "lookalikes": sum(1 for ch in value if ch in LOOKALIKES),
            "invisible": sum(1 for ch in value if unicodedata.category(ch) in ("Cf", "Cc")
                             or (ord(ch) > 127 and unicodedata.category(ch) == "Zs")),
            "alnum": bool(value) and value.isascii() and value.isalnum()}


def env_findings(info: dict, number: int, say) -> list:
    """Печатает признаки ящика number и возвращает список замечаний (пусто - формат в порядке)."""
    user_key, pass_key, _ = mailboxes.env_names(number)
    problems = []
    for key in (user_key, pass_key):
        lines = [n for n, _ in info["defs"].get(key, [])]
        if len(lines) > 1:
            problems.append(f"{key} указан {len(lines)} раза (строки {lines}): действует ПОСЛЕДНЯЯ, проверьте, что это новая")
        if not lines and info["commented"].get(key):
            problems.append(f"{key} есть только закомментированным (строки {info['commented'][key]}): программа его не видит")
        elif not lines:
            problems.append(f"{key} в файле нет")
    for line, key, why in info["near"]:
        if key in (user_key, pass_key):
            problems.append(f"строка {line}: похоже на {key}, но не совпадает точно ({why}); программа её не использует")
    if user_key in info["defs"]:
        f = value_facts(info["defs"][user_key][-1][1])
        user = f["value"]
        domain = user.partition("@")[2].lower()
        say(f"  адрес: {config.mask_email(user)}; длина {f['length']}; полный адрес (check_email): {yn(config.check_email(user))}; "
            f"домен из семейства mail.ru: {yn(domain in MAILRU_DOMAINS)}")
        for ok, text in ((config.check_email(user), "MAILRU_USER не похож на полный адрес"),
                         (domain in MAILRU_DOMAINS, f"домен адреса не из семейства mail.ru ({domain or 'нет @'}): проверьте адрес"),
                         (not (f["non_ascii"] or f["invisible"] or f["spaces"] or f["tabs"] or f["quotes"]),
                          "в адресе есть пробелы, кавычки, невидимые или не-ASCII знаки")):
            if not ok:
                problems.append(f"ящик {number}: {text}")
    if pass_key in info["defs"]:
        f = value_facts(info["defs"][pass_key][-1][1])
        say(f"  пароль: длина {f['length']}; только латинские буквы и цифры: {yn(f['alnum'])}; пробелов {f['spaces']}, "
            f"табов {f['tabs']}, кавычек {f['quotes']}, знаков не-ASCII {f['non_ascii']} (похожих на латиницу {f['lookalikes']})")
        for bad, text in ((f["spaces"] or f["tabs"], "в пароле есть пробелы или табы (скопирован с пробелом?)"),
                          (f["comment"], "после пароля стоит `# комментарий`: он станет частью пароля"),
                          (f["quotes"] or f["one_sided_quote"], "в пароле кавычки: парные по краям программа снимет, остальные останутся"),
                          (f["non_ascii"] or f["invisible"], "в пароле не-ASCII или невидимые знаки (русская буква, похожая на латинскую?)"),
                          (not f["length"], "пароль пустой")):
            if bad:
                problems.append(f"ящик {number}: {text}")
    return problems


# ---------------------------------------------------------------- перекрытия ----

def python_processes() -> list:
    """[(pid, время старта или None, командная строка)] процессов python; не удалось узнать - []."""
    rows = []
    try:
        if os.name == "nt":
            cmd = ["powershell", "-NoProfile", "-Command",
                   "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | Select-Object ProcessId,"
                   "@{n='Start';e={$_.CreationDate.ToString('yyyy-MM-dd HH:mm:ss')}},CommandLine | ConvertTo-Json -Compress"]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace").stdout.strip()
            data = json.loads(out) if out else []
            for item in ([data] if isinstance(data, dict) else data):
                try:
                    start = time.mktime(time.strptime(item.get("Start") or "", "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    start = None
                rows.append((int(item.get("ProcessId") or 0), start, item.get("CommandLine") or ""))
        else:
            out = subprocess.run(["ps", "-eo", "pid,lstart,args"], capture_output=True, text=True, timeout=30,
                                 env=dict(os.environ, LC_ALL="C")).stdout.splitlines()[1:]
            for line in out:
                parts = line.split(None, 6)
                if len(parts) == 7 and "python" in parts[6]:
                    try:
                        start = time.mktime(time.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y"))
                    except ValueError:
                        start = None
                    rows.append((int(parts[0]), start, parts[6]))
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return rows


def override_findings(file_values: dict, project: str, mtime: float, say, scan_files=True, processes=python_processes) -> tuple:
    """Что может перекрывать local.env: (problems, warnings). problems - то, что видит ЭТОТ процесс (переменные окружения):
    оно влияет на вход, который проверка делает ниже. warnings - то, что касается других процессов (настройки запуска PyCharm,
    другие копии программы, сайт или терминал, запущенные до правки файла): проверка читает свежий файл, поэтому на её вход они не
    влияют, но рассылка из тех мест возьмёт старые значения. scan_files=False пропускает поиск по .idea и соседним папкам (тесты)."""
    problems, warnings = [], []
    watched = sorted(k for k in os.environ if k.upper().startswith(("MAIL", "SMTP")))
    say(f"Переменные окружения этого процесса на MAIL*/SMTP*: {len(watched)}")
    for name in watched:
        value, in_file = os.environ[name], file_values.get(name)
        differs = bool(value) and in_file is not None and value != in_file
        say(f"  {name}: непустая {yn(value)}; есть в local.env {yn(in_file is not None)}; значение совпадает с local.env: "
            f"{yn(in_file is not None and value == in_file)}")
        if differs:
            problems.append(f"{name} задана в окружении и ОТЛИЧАЕТСЯ от local.env: программа берёт значение из окружения")
        elif in_file and not value:
            problems.append(f"{name} пуста в окружении: сайт (run_local) из-за этого тоже не возьмёт значение из local.env")

    idea = os.path.join(project, ".idea")
    for root, _, files in os.walk(idea) if scan_files and os.path.isdir(idea) else []:
        for fname in files:
            if not fname.endswith(".xml"):
                continue
            try:
                with open(os.path.join(root, fname), encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
            except OSError:
                continue
            for name, value in re.findall(r'<env\s+name="([^"]+)"\s+value="([^"]*)"', body):
                if name.upper().startswith(("MAIL", "SMTP")):
                    same = file_values.get(name) == value
                    say(f"В настройках запуска PyCharm ({fname}) задана {name}; совпадает с local.env: {yn(same)}")
                    if not same:
                        warnings.append(f"{name} задана в настройках запуска PyCharm ({fname}) и отличается от local.env: "
                                        "программа, запущенная этой настройкой, возьмёт её значение")

    parent = os.path.dirname(os.path.abspath(project))
    others = []
    base = parent.rstrip("\\/").count(os.sep)
    for root, dirs, files in os.walk(parent) if scan_files else []:
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        if root.rstrip("\\/").count(os.sep) - base >= 3:
            dirs[:] = []
        if "mailer.py" in files and os.path.abspath(root) != os.path.abspath(project):
            others.append(os.path.relpath(root, parent))
    if others:
        warnings.append("есть другие копии программы (" + ", ".join(others[:5]) + "): mailer.py оттуда читает СВОЙ local.env, "
                        "а не этот; запускали ли вы рассылку оттуда?")
    if scan_files:
        say(f"Другие копии mailer.py рядом (поиск в {parent}, до 3 уровней): {len(others)}")

    for pid, start, cmdline in processes():
        found = re.search(r"run_local|app\.py|mailer\.py", cmdline)
        if pid == os.getpid() or not found:
            continue
        if start is not None and start < mtime - 1:
            warnings.append(f"процесс {pid} ({found.group(0)}) запущен ДО последнего сохранения local.env и держит старые "
                            "значения: остановите его (для сайта красный квадрат в PyCharm) и запустите заново")
    return problems, warnings


# ---------------------------------------------------------------- вход ----

def load_ledger(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_ledger(path: str, ledger: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, ensure_ascii=False, indent=1)


def login_once(box, port, connect, ledger, ledger_path, again, clock, say) -> str:
    """Одна попытка входа (без письма). Возвращает ok / auth / conn / skipped."""
    secrets = {box.password, box.user}
    say(f"\n-- {box.label}: {config.mask_email(box.user)} --")
    last = ledger.get(box.id, {}).get("at")
    if last and not again and clock() - last < HOUR:
        say(f"  ПРОПУЩЕНО: вход на этот ящик пробовали {int((clock() - last) // 60)} мин назад (журнал check_login.json). "
            "Повтор может заблокировать ящик; ключ --again снимает защиту.")
        return "skipped"
    mode = "SSL сразу, порт 465" if port == 465 else "STARTTLS, порт 587"
    say(f"  {smtp.SMTP_HOST}, {mode}, проверка сертификата включена, команда AUTH PLAIN (одна попытка)")
    ledger[box.id] = {"at": clock(), "result": "started"}
    save_ledger(ledger_path, ledger)            # до попытки: если процесс оборвётся, повторять вслепую нельзя
    server = None
    try:
        server = connect(smtp.SMTP_HOST, port, 60.0)
        server.ehlo_or_helo_if_needed()
        sock = getattr(server, "sock", None)
        tls = f"{sock.version()}" if isinstance(sock, ssl.SSLSocket) else "?"
        say(f"  соединение установлено (TLS {tls}); сервер заявил AUTH: {server.esmtp_features.get('auth', '(нет)').strip()}")
        server.user, server.password = box.user, box.password
        code, resp = server.auth("PLAIN", server.auth_plain)
        text = scrub(resp.decode("utf-8", "replace") if isinstance(resp, bytes) else resp, secrets)
        say(f"  ОТВЕТ СЕРВЕРА: {code} {text}")
        ok = code in (235, 503)
        ledger[box.id]["result"] = str(code)
        say("  вход ПРИНЯТ (письмо не отправлялось)" if ok else "  вход не принят")
        return "ok" if ok else "auth"
    except smtplib.SMTPAuthenticationError as exc:
        error = exc.smtp_error.decode("utf-8", "replace") if isinstance(exc.smtp_error, bytes) else exc.smtp_error
        say(f"  ОТВЕТ СЕРВЕРА: {exc.smtp_code} {scrub(error, secrets)}")
        say("  логин/пароль ОТКЛОНЕНЫ сервером")
        ledger[box.id]["result"] = str(exc.smtp_code)
        return "auth"
    except ssl.SSLCertVerificationError as exc:
        say(f"  сертификат сервера не принят ({scrub(exc, secrets)}): это до ввода пароля; запустите check_cert.py")
        ledger[box.id]["result"] = "cert"
        return "conn"
    except (ssl.SSLError, OSError, smtplib.SMTPException) as exc:
        say(f"  связь/шифрование: {type(exc).__name__}: {scrub(smtp.reply_text(exc), secrets)}; пароль не проверялся "
            "(VPN, прокси, антивирус: запустите check_cert.py)")
        ledger[box.id]["result"] = type(exc).__name__
        return "conn"
    finally:
        save_ledger(ledger_path, ledger)
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


def verdict(problems: list, results: dict, warnings=()) -> list:
    """Итог простыми словами: замечания, которые влияют на вход этой проверки (problems), предупреждения о других процессах
    (warnings) и результаты входа {номер ящика: ok/auth/conn/skipped}."""
    lines = []
    if problems:
        lines.append(f"Найдено замечаний к настройке: {len(problems)} (отмечены знаком ! выше). Они действуют и на вход этой "
                     "проверки, поэтому чините их первыми: без них отказ мог бы пропасть.")
    if warnings:
        lines.append(f"Предупреждений о других процессах: {len(warnings)} (знак ~ выше). На вход этой проверки они НЕ влияют "
                     "(она читает свежий local.env), и объяснить отказ ими нельзя. Но сайт или терминал со старыми значениями "
                     "отправят с ними: перезапустите их.")
    control, main = results.get(CONTROL), results.get(MAIN)
    if "conn" in results.values():
        lines.append("До проверки пароля не дошло: сбой связи или шифрования. Запустите check_cert.py.")
    elif control == "ok" and main == "ok":
        lines.append("Оба входа приняты: данные верные. Если рассылка всё равно падает, её запускает процесс со СТАРЫМИ значениями "
                     "(сайт или окно терминала): остановите сайт, закройте терминалы и запустите заново.")
    elif control == "ok" and main == "auth":
        lines.append("Ящик 2 принят, ящик 1 отклонён тем же способом, с той же связью и шифрованием: программа и сеть исправны, "
                     "дело в пароле или состоянии ящика 1. "
                     + ("Формат значения и перекрытия в порядке, значит причина на стороне mail.ru: " if not problems
                        else "Сначала исправьте замечания выше, а если они не помогут, причина на стороне mail.ru: ")
                     + "пароль приложения создан в другом аккаунте, аннулирован, или у него нет доступа «Почтовые программы» "
                     "(IMAP/SMTP), либо ящик ещё в ограничении после блокировки.")
    elif control == "auth":
        lines.append("Контрольный ящик 2 тоже отклонён: причина общая (окружение, код, правило mail.ru), а не в ящике 1. "
                     "Ящик 1 не пробовался.")
    elif main == "auth":
        lines.append("Ящик 1 отклонён, контрольного ящика 2 нет или он пропущен: сравнить не с чем.")
    elif results and all(v == "skipped" for v in results.values()):
        lines.append("Входы сегодня уже пробовали (журнал попыток): новых попыток нет. Не повторяйте раньше чем через час.")
    elif not results:
        lines.append("Вход не выполнялся (ключ --no-login или нет подключённых ящиков).")
    return lines


def run(env_path=config.ENV_FILE, project=HERE, login=True, again=False, ledger_path=LEDGER, connect=smtp.default_connect,
        clock=time.time, deep=True, quiet=False, report_path=REPORT) -> tuple:
    """Возвращает (код выхода, строки отчёта). deep=False пропускает поиск по .idea, соседним папкам и процессам (тесты)."""
    say = Report(quiet)
    say(f"Проверка входа в mail.ru, {datetime.datetime.now():%Y-%m-%d %H:%M:%S}. Python {sys.version.split()[0]}, "
        f"{ssl.OPENSSL_VERSION}, {sys.platform}. Письма не отправляются.")
    say(f"local.env, который читает mailer.py: {env_path}")
    keys, problems, warnings = watched_keys(), [], []
    if not os.path.isfile(env_path):
        say("ФАЙЛА НЕТ: программа работает только с переменными окружения.")
        info, file_values = {"defs": {}, "commented": {}, "near": [], "mtime": 0.0, "utf16": False, "bom": False}, {}
    else:
        info = scan_env(env_path, keys)
        say(f"Файл: {info['size']} байт, изменён {datetime.datetime.fromtimestamp(info['mtime']):%Y-%m-%d %H:%M:%S}")
        if info["utf16"]:
            problems.append("файл сохранён не в UTF-8 (UTF-16?): программа его не прочитает")
        try:
            file_values = config.read_env_file(env_path)
        except (OSError, ValueError) as exc:
            problems.append(f"config.read_env_file не смог прочитать файл ({type(exc).__name__})")
            file_values = {}

    say("\n== Формат значений (сами значения не печатаются) ==")
    for number in (CONTROL, MAIN):
        say(f"Ящик {number}:" + ("  (контрольный)" if number == CONTROL else ""))
        problems += env_findings(info, number, say)
    configured = [n for n in range(1, mailboxes.MAX_MAILBOXES + 1)
                  if mailboxes.env_names(n)[0] in file_values or mailboxes.env_names(n)[1] in file_values]
    say(f"Ящики, заданные в файле: {configured or 'нет'}")

    say("\n== Что может перекрывать local.env ==")
    found, notes = override_findings(file_values, project, info["mtime"], say, scan_files=deep,
                                     processes=python_processes if deep else (lambda: []))
    problems += found
    warnings += notes

    readable = True
    try:
        config.load_env_file(env_path)         # как в mailer.py: окружение главнее файла
    except (OSError, ValueError):
        readable = False
    results = {}
    say("\n== Вход (только login, письмо не отправляется) ==")
    if not login:
        say("Не выполнялся (ключ --no-login).")
    elif not readable:
        say("Не выполнялся: local.env не читается (mailer.py остановился бы на этом же месте). Сохраните файл в UTF-8.")
        login = False
    else:
        try:
            port = config.resolve_port()
        except ValueError as exc:
            say(f"Ошибка: {exc}")
            return 1, say.lines
        boxes = {b.id: b for b in mailboxes.load_mailboxes()}
        ledger = load_ledger(ledger_path)
        for number in (CONTROL, MAIN):
            box = boxes.get(str(number))
            if box is None or not box.ready:
                say(f"\n-- ящик {number}: не подключён (нет адреса или пароля) --")
                continue
            if results.get(CONTROL) == "auth":
                say(f"\n-- ящик {number}: ПРОПУЩЕН. Контрольный ящик получил отказ, значит причина не в ящике {number}; "
                    "лишняя неудачная попытка только навредит. --")
                continue
            results[number] = login_once(box, port, connect, ledger, ledger_path, again, clock, say)
            if results[number] == "conn":
                say("  Связь не работает: остальные ящики не пробуются.")
                break

    say("\n== ИТОГ ==")
    for text in problems:
        say(f"  ! {text}")
    for text in warnings:
        say(f"  ~ {text}")
    for text in verdict(problems, results, warnings):
        say(text)
    if report_path:
        try:
            with open(report_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(say.lines) + "\n")
            say(f"\nОтчёт сохранён: {report_path} (без паролей; адреса скрыты)")
        except OSError:
            pass
    codes = {"auth": 2, "conn": 3}
    return max([codes.get(kind, 0) for kind in results.values()] or [0]), say.lines


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description="Проверка входа в mail.ru без отправки письма")
    parser.add_argument("--no-login", action="store_true", help="только проверить файлы и окружение, в сеть не ходить")
    parser.add_argument("--again", action="store_true", help="разрешить второй вход на ящик в течение часа (не нужно)")
    args = parser.parse_args(argv)
    code, _ = run(login=not args.no_login, again=args.again)
    return code


if __name__ == "__main__":
    sys.exit(main())
