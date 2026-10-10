"""Запуск сайта у себя на компьютере (например, из PyCharm): откройте этот файл и нажмите Run.

Что делает:
    1. Берёт настройки из файла local.env рядом с программой (его нет в git: пароли в нём остаются
       только на вашем компьютере). Если файла нет, создаёт его из local.env.example и просит заполнить.
    2. Берёт пароль страницы из LEADS_TOKEN в local.env; если его там нет, придумывает и дописывает туда один
       раз. Вводить пароль не нужно: браузер открывается со ссылкой, в которой он уже есть. Сайт слушает
       только этот компьютер (127.0.0.1).
    3. Папка данных рассылки (таблицы, письма, журналы): OUTREACH_DATA из local.env, иначе папка outreach-data
       рядом с папкой проекта.
    4. Запускает сайт и открывает в браузере страницу /leads: таблица новых компаний и кнопка «Отправить».
"""
import argparse
import os
import secrets
import shutil
import sys
import threading
import webbrowser

from mailing.config import read_env_file                  # общий разбор local.env с рассылкой

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "local.env")
DEFAULT_DATA_DIR = os.path.join(os.path.dirname(HERE), "outreach-data")      # рядом с папкой проекта, вне git
MIN_TOKEN_LENGTH = 12                                                          # как в leads_job
EXAMPLE_PATH = os.path.join(HERE, "local.env.example")
REQUIRED = ("MAILRU_USER", "MAILRU_APP_PASSWORD")
DEFAULT_PORT = 8000


def stored_token(env_path: str, values: dict) -> str:
    """Пароль страницы из local.env. Нет (или короче 12 знаков) - создаёт новый и дописывает в файл, чтобы он
    не менялся от запуска к запуску и его не нужно было вводить. Не удалось записать - пароль на этот запуск."""
    token = values.get("LEADS_TOKEN", "")
    if len(token) >= MIN_TOKEN_LENGTH:
        return token
    token = secrets.token_urlsafe(16)
    if values.get("LEADS_TOKEN"):
        print("LEADS_TOKEN в local.env короче 12 знаков: на этот запуск создан другой пароль. "
              "Замените строку в файле или удалите её, и пароль сохранится сам.")
        return token
    try:
        with open(env_path, "a", encoding="utf-8") as fh:
            fh.write("\n# Пароль страниц /leads и /mail: создан автоматически, вводить его не нужно\n"
                     f"LEADS_TOKEN={token}\n")
    except OSError as exc:
        print(f"Не удалось сохранить пароль страницы в {env_path} ({exc.strerror or exc}): он действует только "
              "на этот запуск.")
    return token


def serve(port: int, token: str, values: dict, open_browser: bool = True) -> None:
    for key, value in values.items():
        os.environ.setdefault(key, value)                   # настоящие переменные окружения главнее файла
    os.environ["LEADS_TOKEN"] = token
    os.environ["HOST"] = "127.0.0.1"                        # только этот компьютер
    os.environ["TRUSTED_PROXIES"] = "0"
    if open_browser:
        url = f"http://127.0.0.1:{port}/leads#{token}"      # то, что после #, на сервер не уходит
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    import app
    app.main(["--port", str(port)])


def main(argv=None, env_path: str = ENV_PATH, example_path: str = EXAMPLE_PATH, run=serve) -> int:
    parser = argparse.ArgumentParser(description="Запуск сайта (таблица новых компаний, отправка, пробное письмо) на этом компьютере")
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
    token = stored_token(env_path, values)
    if not values.get("OUTREACH_DATA"):
        values["OUTREACH_DATA"] = DEFAULT_DATA_DIR
    print(f"Папка данных рассылки: {values['OUTREACH_DATA']}")
    print(f"Если браузер не открылся сам, откройте http://127.0.0.1:{port}/leads#<пароль из строки LEADS_TOKEN "
          "в файле local.env>")
    try:
        run(port, token, values, not args.no_browser)
    except OSError as exc:
        print(f"Не удалось запустить сайт на порту {port}: {exc.strerror or exc}. "
              f"Порт занят? Попробуйте другой: добавьте в запуск --port {port + 1}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
