"""Диагностика соединения с почтовым сервером smtp.mail.ru: порты 465 и 587, сертификат, куда «упирается» связь.

Запуск: правой кнопкой на файле -> Run 'check_cert'. Пароль не нужен и не передаётся: программа только устанавливает соединение
и читает сертификат, письма не отправляются. В конце итог простыми словами: что мешает и что делать.
"""
import os
import smtplib
import socket
import ssl
import sys
import tempfile

from mailing import smtp

HOST = smtp.SMTP_HOST
PORTS = (465, 587)                  # 465 - защищённое соединение сразу, 587 - обычное, затем STARTTLS
OK, UNTRUSTED, DROPPED, NO_CONNECTION = "ok", "untrusted", "dropped", "no_connection"
TIMEOUT = 15


def classify(exc) -> str:
    """Что случилось по виду ошибки: сертификат не принят / соединение оборвано при установке шифрования / нет соединения."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return UNTRUSTED
    if isinstance(exc, (ssl.SSLError, ConnectionResetError, ConnectionAbortedError, smtplib.SMTPServerDisconnected)) \
            or "EOF occurred" in str(exc) or "violation of protocol" in str(exc):
        return DROPPED
    return NO_CONNECTION


def tcp_check(port: int) -> str:
    try:
        with socket.create_connection((HOST, port), timeout=TIMEOUT):
            return OK
    except OSError as exc:
        print(f"    TCP-соединение: нет ({type(exc).__name__}: {exc})")
        return NO_CONNECTION


def tls_check(port: int, context: ssl.SSLContext, label: str) -> str:
    """Защищённое соединение: на 465 сразу, на 587 через STARTTLS. Возвращает OK, UNTRUSTED, DROPPED или NO_CONNECTION."""
    try:
        if port == 465:
            with socket.create_connection((HOST, port), timeout=TIMEOUT) as raw:
                with context.wrap_socket(raw, server_hostname=HOST) as tls:
                    version = tls.version()
        else:
            with smtplib.SMTP(HOST, port, timeout=TIMEOUT) as server:
                server.ehlo()
                server.starttls(context=context)
                version = server.sock.version() if hasattr(server.sock, "version") else "TLS"
        print(f"    {label}: сертификат принят ({version})")
        return OK
    except Exception as exc:                                 # любая ошибка сети или шифрования: классифицируем и покажем
        status = classify(exc)
        what = {UNTRUSTED: "сертификат НЕ принят", DROPPED: "соединение ОБОРВАНО при установке шифрования",
                NO_CONNECTION: "нет соединения"}[status]
        detail = getattr(exc, "verify_message", "") or f"{type(exc).__name__}: {exc}"
        print(f"    {label}: {what} - {detail}")
        return status


def describe(name) -> str:
    return ", ".join(f"{key}={value}" for part in name for key, value in part)


def show_presented_certificate(port: int = 465) -> None:
    """Показывает, кем выдан сертификат сервера (проверка здесь выключена только ради чтения; порт 465)."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((HOST, port), timeout=TIMEOUT) as raw:
            with context.wrap_socket(raw, server_hostname=HOST) as tls:
                der = tls.getpeercert(binary_form=True)
    except Exception as exc:
        print(f"Сертификат прочитать не удалось: {type(exc).__name__}: {exc}")
        return
    pem = ssl.DER_cert_to_PEM_cert(der)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "server.pem")
        with open(path, "w") as fh:
            fh.write(pem)
        try:
            info = ssl._ssl._test_decode_cert(path)           # служебная функция: в новых Python может исчезнуть
        except Exception:
            print("Расшифровать сертификат не удалось (это не страшно). Выдан сервером:\n" + pem)
            return
    print("Сервер прислал сертификат:")
    print("  кому выдан :", describe(info.get("subject", ())))
    print("  кем выдан  :", describe(info.get("issuer", ())))
    print("  действует  :", info.get("notBefore"), "-", info.get("notAfter"))


def verdict(results: dict) -> tuple:
    """(код выхода, текст итога) по результатам {порт: [статусы проверок защищённого соединения]}."""
    working = [port for port, statuses in results.items() if OK in statuses]
    if 465 in working:
        return 0, "Итог: порт 465 работает, соединение безопасно проверяется. Можно отправлять письма."
    if 587 in working:
        return 0, ("Итог: порт 465 не работает, а порт 587 работает. Впишите в local.env строку MAILRU_PORT=587, сохраните файл "
                   "(Ctrl+S) и перезапустите сайт (run_local): письма и пробное письмо пойдут через 587.")
    every = [status for statuses in results.values() for status in statuses]
    if UNTRUSTED in every:
        return 1, ("Итог: этот компьютер не доверяет центру, выдавшему сертификат (его видно выше). Пришлите весь этот вывод; "
                   "если это российский корневой сертификат, его путь вписывают в local.env строкой MAILRU_CA_FILE=путь\\к\\файлу.pem.")
    if DROPPED in every:
        return 1, ("Итог: соединение доходит до сервера, но обрывается при установке шифрования, на всех проверенных портах. Это не пароль "
                   "и не сертификат. Чаще всего виноват VPN (mail.ru не любит входы с адресов VPN), прокси или антивирус с проверкой "
                   "HTTPS (Kaspersky, ESET, Dr.Web). Что сделать по порядку: 1) выключите VPN на минуту и запустите check_cert снова; "
                   "2) если без VPN работает, исключите из VPN программу python.exe, которой запущен сайт (путь написан в начале этого "
                   "вывода); 3) на минуту отключите в антивирусе проверку защищённых соединений и запустите снова. Если не помогло, "
                   "пришлите весь этот вывод.")
    return 1, ("Итог: соединение с сервером не устанавливается вообще. Проверьте интернет, брандмауэр Windows и антивирус "
               "(порты 465 и 587), выключите VPN и запустите снова.")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    print(f"Проверяем {HOST}. Python: {sys.executable}\n")
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(HOST, 465, type=socket.SOCK_STREAM)})
        print(f"Адрес найден: {', '.join(addresses[:4])}\n")
    except OSError as exc:
        print(f"Адрес {HOST} не найден ({exc}): нет интернета или DNS не работает.")
        return 1
    system_context, certifi_context = ssl.create_default_context(), smtp.make_ssl_context()
    results = {}
    for port in PORTS:
        print(f"Порт {port} ({'SSL сразу' if port == 465 else 'STARTTLS'}):")
        if tcp_check(port) != OK:
            results[port] = [NO_CONNECTION]
            continue
        print("    TCP-соединение: есть")
        results[port] = [tls_check(port, system_context, "хранилище Windows"), tls_check(port, certifi_context, "хранилище + certifi")]
    print()
    if any(OK in statuses or UNTRUSTED in statuses for statuses in results.values()):
        show_presented_certificate()
        print()
    code, text = verdict(results)
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
