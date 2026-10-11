"""Диагностика: какой сертификат присылает smtp.mail.ru и принимает ли его этот компьютер.

Запуск: правой кнопкой на файле -> Run 'check_cert'. Пароль не нужен и не передаётся: программа только
устанавливает защищённое соединение и читает сертификат, письма не отправляются.
"""
import os
import socket
import ssl
import sys
import tempfile

import mailer

HOST, PORT = mailer.SMTP_HOST, mailer.SMTP_PORT


OK, UNTRUSTED, NO_CONNECTION = "ok", "untrusted", "no_connection"


def handshake(context: ssl.SSLContext, label: str) -> str:
    try:
        with socket.create_connection((HOST, PORT), timeout=15) as raw:
            with context.wrap_socket(raw, server_hostname=HOST) as tls:
                print(f"{label}: сертификат принят ({tls.version()})")
                return OK
    except ssl.SSLCertVerificationError as exc:
        print(f"{label}: НЕ принят - {exc.verify_message}")
        return UNTRUSTED
    except OSError as exc:
        print(f"{label}: нет соединения - {exc}")
        return NO_CONNECTION


def describe(name) -> str:
    return ", ".join(f"{key}={value}" for part in name for key, value in part)


def show_presented_certificate() -> None:
    """Показывает, кем выдан сертификат сервера (проверка здесь выключена только ради чтения)."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((HOST, PORT), timeout=15) as raw:
            with context.wrap_socket(raw, server_hostname=HOST) as tls:
                der = tls.getpeercert(binary_form=True)
    except OSError as exc:
        print(f"Сертификат прочитать не удалось: {exc}")
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


def main() -> int:
    print(f"Проверяем {HOST}:{PORT}\n")
    only_system = handshake(ssl.create_default_context(), "1. Только хранилище Windows ")
    with_certifi = handshake(mailer.make_ssl_context(), "2. Хранилище + набор certifi ")
    print()
    show_presented_certificate()
    print()
    if OK in (with_certifi, only_system):
        print("Итог: соединение безопасно проверяется. Можно отправлять письма.")
        return 0
    if NO_CONNECTION in (with_certifi, only_system):
        print("Итог: соединение не устанавливается - это не про сертификат. Проверьте интернет, "
              "брандмауэр и антивирус (порт 465), потом запустите снова.")
    else:
        print("Итог: этот компьютер не доверяет центру, выдавшему сертификат. Пришлите весь этот вывод.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
