"""Ручной сбор куки Arena (arena.ai) по одному аккаунту.

Порядок работы:
  1) скрипт проверяет окружение и браузер Chromium (при необходимости предложит
     доустановить его той же командой, тем же интерпретатором Python);
  2) открывает окно браузера на arena.ai — логинишься сам;
  3) жмёшь Enter — скрипт проверяет, что вход действительно произошёл,
     и сохраняет куки в cookies/account_N.json;
  4) Enter — следующий аккаунт (account_2.json, account_3.json, ...), q — выход.

Запуск:
    python auth.py                     # обычный режим
    python auth.py --check             # только диагностика, без окна браузера
    python auth.py --install           # доустановить Chromium и выйти
    python auth.py --all               # сохранять куки всех доменов, не только arena.ai
    python auth.py --dir D:\\cookies     # другая папка для куки

Формат файла совместим с arena_generator.py: это то, что возвращает
context.storage_state() (объект с полями "cookies" и "origins"), при --all —
как есть, без --all — только домены arena.ai. Существующие файлы не
перезаписываются, нумерация продолжается с первого свободного номера.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ARENA_URL = "https://arena.ai/"
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_COOKIES_DIR = BASE_DIR / "cookies"

LOGIN_TIMEOUT_MS = 25000          # ожидание загрузки arena.ai
VERIFY_SECONDS = 8                # сколько искать признаки входа после Enter

# ── Подсказки по типовым сбоям запуска Chromium ───────────────────────────────

def browser_hint(text):
    """Человеческое объяснение вместо трейсбека Playwright."""
    low = text.lower()
    install_cmd = f'"{sys.executable}" -m playwright install chromium'
    if "executable doesn't exist" in low or "executable does not exist" in low:
        return ("Не установлен Chromium нужной ревизии для этой версии Playwright.\n"
                f"    Выполни: {install_cmd}\n"
                "    Важно: тем же интерпретатором Python, которым запускаешь этот скрипт.")
    if "host system is missing dependencies" in low or "error while loading shared libraries" in low:
        return ("В системе не хватает библиотек для Chromium.\n"
                "    Debian/Ubuntu: sudo python3 -m playwright install-deps chromium\n"
                "    Или: sudo apt-get install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 "
                "libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 "
                "libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2")
    if "missing x server" in low or "$display" in low or "cannot open display" in low:
        return ("Нет графического дисплея, а нужен режим с окном.\n"
                "    Linux/VPS: xvfb-run -a python auth.py\n"
                "    Либо запускай на машине с рабочим столом.")
    if "target page, context or browser has been closed" in low:
        return "Окно браузера закрылось слишком рано (или его закрыли вручную)."
    if "failed to launch" in low and os.name == "nt":
        return ("Chromium не запустился. На Windows чаще всего мешает антивирус/Defender\n"
                "    (блокирует chrome.exe). Добавь папку ms-playwright в исключения.")
    if "profile" in low and "in use" in low:
        return "Профиль браузера занят: закрой все окна Chromium, запущенные этим скриптом."
    return None


def one_line(text, limit=400):
    """Ошибки Playwright многострочные (ASCII-рамки) — сжимаем в одну строку."""
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + " ..."


def print_failure(prefix, exc, extra=True):
    print(f"  ! {prefix}: {type(exc).__name__}: {one_line(exc)}")
    if extra:
        hint = browser_hint(str(exc))
        if hint:
            print(f"    -> {hint}")


# ── Окружение и браузер ──────────────────────────────────────────────────────

def load_playwright():
    """Возвращает sync_playwright или None с понятным сообщением."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        print(f"  ! Playwright не установлен или не импортируется ({exc}).")
        print(f"    Выполни: \"{sys.executable}\" -m pip install playwright")
        print("    (или полный набор: python -m pip install -r requirements.txt)")
        return None
    return sync_playwright


def chromium_path(playwright):
    try:
        return Path(playwright.chromium.executable_path)
    except Exception as exc:
        print_failure("Не удалось определить путь к Chromium", exc)
        return None


def install_chromium(with_deps=False):
    cmd = [sys.executable, "-m", "playwright", "install"]
    if with_deps:
        cmd.append("--with-deps")
    cmd.append("chromium")
    print("  Запускаю:", " ".join(f'"{part}"' if " " in part else part for part in cmd))
    try:
        return subprocess.call(cmd)
    except OSError as exc:
        print_failure("Не удалось запустить установщик", exc)
        return 1


def trial_launch(playwright, headless=True):
    """Пробный запуск: ловим проблему до того, как покажем окно пользователю."""
    try:
        browser = playwright.chromium.launch(headless=headless)
    except Exception as exc:
        return exc
    try:
        browser.close()
    except Exception:
        pass
    return None


def trial(playwright, headless=False):
    """Проверка запуска. Возвращает (ошибка_или_None, режим_строкой).

    Скрипту нужен режим с окном (headed). Если включён headless, а Playwright
    (>= 1.49) не нашёл отдельно поставляемую chrome-headless-shell, пробуем
    headed: бинарник Chromium тот же, ложное «браузер не работает» не нужно.
    """
    error = trial_launch(playwright, headless=headless)
    if error is not None and headless and "headless-shell" in str(error).lower():
        fallback = trial_launch(playwright, headless=False)
        return fallback, ("headed (headless-оболочки нет)" if fallback is None else "браузер не запускается")
    return error, ("headless" if headless else "headed (с окном)")


def ensure_browser(args):
    """True, если Chromium реально запускается. Иначе печатает, что делать."""
    sync_playwright = load_playwright()
    if sync_playwright is None:
        return False
    try:
        with sync_playwright() as playwright:
            exe = chromium_path(playwright)
            if exe is None:
                return False
            if not exe.exists():
                print(f"  ! Chromium не установлен: {exe}")
                if args.install or confirm("    Установить сейчас (скачает ~120 МБ)? [Y/n]: "):
                    install_chromium(with_deps=(os.name != "nt"))
                    exe = chromium_path(playwright)
                if exe is None or not exe.exists():
                    print("  ! Браузер всё ещё отсутствует.")
                    print(f"    Выполни вручную: \"{sys.executable}\" -m playwright install chromium")
                    return False
                print(f"  OK Chromium на месте: {exe}")

            error = trial_launch(playwright)
            if error is not None and browser_hint(str(error)) and "executable" in str(error).lower():
                if args.install or confirm("    Попробовать доустановить Chromium? [Y/n]: "):
                    install_chromium(with_deps=(os.name != "nt"))
                    error = trial_launch(playwright)
            if error is not None:
                print_failure("Chromium не запускается", error)
                return False
            print("  OK Chromium запускается в режиме с окном")
    except Exception as exc:
        print_failure("Проверка окружения не удалась", exc)
        return False
    return True


def do_check():
    """--check: отчёт без открытия окна. Код возврата 0 — всё готово."""
    print("Диагностика окружения")
    print(f"  Python: {sys.version.split()[0]} ({sys.executable})")
    print(f"  Рабочая папка скрипта: {BASE_DIR}")
    ok = True

    sync_playwright = load_playwright()
    if sync_playwright is None:
        return 1
    try:
        from importlib.metadata import version as package_version
        print(f"  Playwright: {package_version('playwright')}")
    except Exception:
        print("  Playwright: версия неизвестна")

    try:
        with sync_playwright() as playwright:
            exe = chromium_path(playwright)
            if exe and exe.exists():
                print(f"  Chromium: есть ({exe})")
            else:
                print(f"  Chromium: НЕТ (ожидался {exe})")
                print(f"    -> \"{sys.executable}\" -m playwright install chromium")
                ok = False
            if exe and exe.exists():
                error, mode = trial(playwright, headless=True)
                if error is None:
                    print(f"  Пробный запуск: OK ({mode})")
                else:
                    print("  Пробный запуск: ошибка")
                    print_failure("Причина", error)
                    ok = False
            else:
                print("  Пробный запуск: пропущен (браузера нет)")
    except Exception as exc:
        print_failure("Проверка Playwright не удалась", exc)
        ok = False

    cookies_dir = Path(DEFAULT_COOKIES_DIR)
    try:
        cookies_dir.mkdir(parents=True, exist_ok=True)
        probe = cookies_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print(f"  Папка куки: OK ({cookies_dir})")
    except Exception as exc:
        print_failure(f"Папка куки недоступна ({cookies_dir})", exc)
        ok = False

    print("\nИтог:", "всё готово" if ok else "есть проблемы, см. выше")
    return 0 if ok else 1


# ── Куки ─────────────────────────────────────────────────────────────────────

def is_arena_host(value):
    host = (value or "").lstrip(".").split("://")[-1].split("/")[0]
    return host == "arena.ai" or host.endswith(".arena.ai")


def keep_arena_only(state):
    state["cookies"] = [c for c in state.get("cookies", []) if is_arena_host(c.get("domain"))]
    state["origins"] = [o for o in state.get("origins", []) if is_arena_host(o.get("origin", ""))]
    return state


def next_free_index(directory):
    index = 1
    while (directory / f"account_{index}.json").exists():
        index += 1
    return index


def verify_login(page):
    """Best-effort проверка входа: ищем Log out / меню аккаунта (как bootstrap_google)."""
    try:
        page.goto(ARENA_URL, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
    except Exception:
        pass
    deadline = time.monotonic() + VERIFY_SECONDS
    while time.monotonic() < deadline:
        for role in ("menuitem", "button", "link"):
            try:
                control = page.get_by_role(role, name=re.compile(r"^\s*(log ?out|sign ?out)\s*$", re.I))
                if control.count() and control.first.is_visible():
                    return True, "видна кнопка Log out / Sign out"
            except Exception:
                pass
        try:
            marker = page.locator(
                '[aria-label*="account" i], [aria-label*="profile" i], '
                '[data-testid*="user" i], [data-testid*="account" i], [data-testid*="avatar" i]')
            if marker.count():
                return True, "виден элемент меню аккаунта"
        except Exception:
            pass
        if page.is_closed():
            return False, "страница закрыта"
        page.wait_for_timeout(400)
    return False, "признаков входа не найдено"


# ── Интерактив ───────────────────────────────────────────────────────────────

def confirm(prompt):
    try:
        return input(prompt).strip().lower() in ("", "y", "yes", "д", "да")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def collect(playwright, cookies_dir, start_url, all_domains, verify):
    index = next_free_index(cookies_dir)
    saved = []
    print(f"Папка куки: {cookies_dir}")
    print(f"Первый аккаунт: #{index}")

    browser = playwright.chromium.launch(headless=False)   # ошибки уже отловлены в ensure_browser
    try:
        while True:
            context = browser.new_context(locale="en-US", viewport={"width": 1600, "height": 1000})
            try:
                page = context.new_page()
                try:
                    page.goto(start_url, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
                except Exception as exc:
                    print_failure("Не удалось открыть Arena", exc)
                    return saved
                before = {c["name"] for c in context.cookies()}

                print(f"\n=== Аккаунт #{index} ===")
                print("  1) залогинься (Continue with Google) и дождись arena.ai")
                print("  2) открой меню аккаунта: должны быть видны e-mail и Log out")
                try:
                    input("  3) затем нажми Enter здесь... ")
                except (EOFError, KeyboardInterrupt):
                    print("\nПрервано.")
                    return saved

                confirmed, reason = (False, "проверка отключена")
                if verify:
                    if page.is_closed():
                        confirmed, reason = False, "окно браузера закрыто"
                    else:
                        confirmed, reason = verify_login(page)
                    print(f"  проверка входа: {'да' if confirmed else 'НЕТ'} ({reason})")

                state = context.storage_state()
                state = state if all_domains else keep_arena_only(state)
                names = [c["name"] for c in state["cookies"]]
                added = sorted({c["name"] for c in state["cookies"]} - before)

                if not names:
                    print("  ! Куки arena.ai не найдены — похоже, вход не выполнялся.")
                    if not confirm("    Сохранить всё равно? [y/N]: "):
                        continue
                elif verify and not confirmed:
                    print("  ! Вход не подтверждён (меню аккаунта не найдено).")
                    if not confirm("    Сохранить всё равно? [Y/n]: "):
                        continue
                elif not added:
                    # Если вход подтверждён по интерфейсу, это лишь замечание:
                    # часть сессионных куки могла уже существовать до входа.
                    print("  ! Новых куки после входа не появилось — проверь, что вход выполнен.")
                    if not confirmed and not confirm("    Сохранить всё равно? [y/N]: "):
                        continue

                path = cookies_dir / f"account_{index}.json"
                path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                saved.append(path)
                print(f"  + сохранено: {path.name} (куки: {len(names)}, новых: {len(added)})")
                if names:
                    print(f"    {', '.join(names[:8])}{' ...' if len(names) > 8 else ''}")
                index += 1
            except KeyboardInterrupt:
                print("\nПрервано.")
                return saved
            finally:
                context.close()

            try:
                answer = input("\nEnter - следующий аккаунт, q - выход: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if answer in ("q", "quit", "exit", "й"):
                break
    finally:
        try:
            browser.close()
        except Exception:
            pass
    return saved


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Ручной сбор Arena-куки по одному аккаунту (account_N.json)")
    parser.add_argument("--check", action="store_true", help="только диагностика, без браузера")
    parser.add_argument("--install", action="store_true", help="доустановить Chromium и действовать без вопросов")
    parser.add_argument("--dir", default=str(DEFAULT_COOKIES_DIR), help="папка для account_N.json")
    parser.add_argument("--url", default=ARENA_URL, help="стартовый адрес (по умолчанию arena.ai)")
    parser.add_argument("--all", action="store_true", help="сохранять куки всех доменов, а не только arena.ai")
    parser.add_argument("--no-verify", action="store_true", help="не проверять вход после Enter")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.check:
        return do_check()

    sync_playwright = load_playwright()
    if sync_playwright is None:
        return 1
    if args.install:
        install_chromium(with_deps=(os.name != "nt"))
    if not ensure_browser(args):
        print("\nПодсказка: запусти 'python auth.py --check' и покажи вывод целиком.")
        return 2

    cookies_dir = Path(args.dir).expanduser()
    try:
        cookies_dir.mkdir(parents=True, exist_ok=True)     # storage_state() папку НЕ создаёт
    except Exception as exc:
        print_failure(f"Не удалось создать папку {cookies_dir}", exc)
        return 1

    try:
        with sync_playwright() as playwright:
            saved = collect(playwright, cookies_dir.resolve(), args.url, args.all, not args.no_verify)
    except KeyboardInterrupt:
        print("\nПрервано.")
        saved = []
    except Exception as exc:
        print_failure("Работа остановлена", exc)
        return 1

    if saved:
        print("\nСохранённые профили (arena_generator.py подхватит их сам):")
        for path in saved:
            print(f"  {path}")
    else:
        print("\nНичего не сохранено.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:                     # без трейсбека и без секретов в логе
        print(f"Ошибка: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
