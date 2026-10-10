"""Запуск сайта у себя на компьютере (например, из PyCharm): откройте этот файл и нажмите Run.

Что делает:
    1. Берёт настройки из файла local.env рядом с программой (его нет в git: пароли в нём остаются
       только на вашем компьютере). Если файла нет, создаёт его из local.env.example и просит заполнить.
    2. Придумывает пароль для страницы на этот запуск (его вводить не нужно: браузер открывается
       со ссылкой, в которой он уже есть). Сайт слушает только этот компьютер (127.0.0.1).
    3. Запускает сайт и открывает в браузере страницу /mail с готовым примером письма.
"""
import argparse
import os
import secrets
import shutil
import sys
import threading
import webbrowser

from mailer import read_env_file                            # общий разбор local.env с рассылкой

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "local.env")
EXAMPLE_PATH = os.path.join(HERE, "local.env.example")
REQUIRED = ("MAILRU_USER", "MAILRU_APP_PASSWORD")
DEFAULT_PORT = 8000


def serve(port: int, token: str, values: dict, open_browser: bool = True) -> None:
    for key, value in values.items():
        os.environ.setdefault(key, value)                   # настоящие переменные окружения главнее файла
    os.environ["LEADS_TOKEN"] = token
    os.environ["HOST"] = "127.0.0.1"                        # только этот компьютер
    os.environ["TRUSTED_PROXIES"] = "0"
    if open_browser:
        url = f"http://127.0.0.1:{port}/mail#{token}"       # то, что после #, на сервер не уходит
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    import app
    app.main(["--port", str(port)])


def main(argv=None, env_path: str = ENV_PATH, example_path: str = EXAMPLE_PATH, run=serve) -> int:
    parser = argparse.ArgumentParser(description="Запуск сайта и страницы /mail на этом компьютере")
    parser.add_argument("--port", type=int, default=None, help=f"порт (по умолчанию {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    args = parser.parse_args(argv)

    if not os.path.exists(env_path):
        shutil.copyfile(example_path, env_path)
        print(f"Создан файл {env_path}.\nОткройте его, впишите MAILRU_USER и MAILRU_APP_PASSWORD "
              "(инструкция внутри файла), сохраните и запустите снова.")
        return 1
    values = read_env_file(env_path)
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        print(f"В файле {env_path} не заполнено: {', '.join(missing)}. Впишите значения после знака = "
              "(без пробелов и кавычек), сохраните файл и запустите снова.")
        return 1

    port = args.port or int(values.get("PORT") or DEFAULT_PORT)
    token = values.get("LEADS_TOKEN", "")
    if len(token) < 12:
        token = secrets.token_urlsafe(16)                   # новый на каждый запуск; в файл не записывается
    print(f"Если браузер не открылся сам, откройте http://127.0.0.1:{port}/mail и введите пароль: {token}")
    try:
        run(port, token, values, not args.no_browser)
    except OSError as exc:
        print(f"Не удалось запустить сайт на порту {port}: {exc.strerror or exc}. "
              f"Порт занят? Попробуйте другой: добавьте в запуск --port {port + 1}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
