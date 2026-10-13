"""Ручной сбор куки Arena (arena.ai) по одному аккаунту.

Три способа получить cookies/account_N.json:

  A. Через окно браузера (основной):
       python auth.py
     Логинишься сам, жмёшь Enter — файл сохраняется. Enter — следующий аккаунт.

  B. Перенос куки из обычного Chrome (если Arena не пускает автоматизированный
     браузер: "Failed to verify your browser" — это Vercel Security Checkpoint):
       1) залогинься на arena.ai в своём Chrome;
       2) расширением Cookie-Editor нажми Export -> JSON для домена arena.ai;
       3) сохрани файл и выполни:
            python auth.py --import cookies_export.json
     Браузер вообще не запускается, Playwright не нужен.

  C. Диагностика без входа в аккаунт:
       python auth.py --check      # окружение, браузеры, папка куки
       python auth.py --install    # доустановить Chromium и выйти

Прочее: --channel chrome|msedge|chromium|auto (по умолчанию auto: реальный
Chrome -> Edge -> встроенный Chromium), --profile DIR (постоянный профиль,
один аккаунт за запуск), --dir (папка куки), --url, --all (куки всех доменов),
--no-verify. Существующие account_N.json не перезаписываются.
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
DEFAULT_PROFILE_DIR = BASE_DIR / "auth_profile"

LOGIN_TIMEOUT_MS = 30000          # ожидание загрузки arena.ai
VERIFY_SECONDS = 8                # сколько искать признаки входа после Enter
CHECKPOINT_SECONDS = 15           # сколько ждать, пока чекап Vercel пропустит сам

# Флаги как в arena_generator.py: убирают самые явные признаки автоматизации.
ANTI_AUTOMATION_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--window-size=1600,1000",
]
# Убираем сам флаг --enable-automation, который Playwright добавляет по умолчанию.
IGNORE_DEFAULT_ARGS = ["--enable-automation"]
STEALTH_SCRIPT = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"

CHANNEL_CANDIDATES = {
    "auto": (None, "chrome", "msedge"),      # None = встроенный Chromium
    "chromium": (None,),
    "chrome": ("chrome",),
    "msedge": ("msedge",),
}
CHANNEL_TITLES = {None: "встроенный Chromium", "chrome": "Google Chrome", "msedge": "Microsoft Edge"}

CHECKPOINT_MARKERS = (
    "failed to verify your browser",
    "vercel security checkpoint",
    "security checkpoint",
)


# ── Подсказки по типовым сбоям запуска Chromium ───────────────────────────────

def browser_hint(text):
    """Человеческое объяснение вместо трейсбека Playwright."""
    low = text.lower()
    install_cmd = f'"{sys.executable}" -m playwright install chromium'
    if "executable doesn't exist" in low or "executable does not exist" in low:
        return ("Не установлен Chromium нужной ревизии для этой версии Playwright.\n"
                f"    Выполни: {install_cmd}\n"
                "    Важно: тем же интерпретатором Python, которым запускаешь этот скрипт.")
    if "distribution" in low and "is not found" in low:
        return ("Такого браузера нет в системе (например, не установлен Google Chrome).\n"
                "    Запусти без --channel (auto) или укажи --channel msedge / chromium.")
    if "host system is missing dependencies" in low or "error while loading shared libraries" in low:
        return ("В системе не хватает библиотек для Chromium.\n"
                "    Debian/Ubuntu: sudo python3 -m playwright install-deps chromium")
    if "missing x server" in low or "$display" in low or "cannot open display" in low:
        return ("Нет графического дисплея, а нужен режим с окном.\n"
                "    Linux/VPS: xvfb-run -a python auth.py")
    if "target page, context or browser has been closed" in low:
        return "Окно браузера закрылось слишком рано (или его закрыли вручную)."
    if "failed to launch" in low and os.name == "nt":
        return ("Chromium не запустился. На Windows чаще всего мешает антивирус/Defender\n"
                "    (блокирует chrome.exe). Добавь папку ms-playwright в исключения.")
    if "profile" in low and "in use" in low:
        return "Профиль браузера занят: закрой все окна, запущенные этим скриптом."
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
        print("    Не хочешь ставить браузер вообще? Используй --import (перенос куки из Chrome).")
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


def trial_launch(playwright, headless=True, channel=None):
    """Пробный запуск: ловим проблему до того, как покажем окно пользователю."""
    options = {"headless": headless, "ignore_default_args": list(IGNORE_DEFAULT_ARGS)}
    if channel:
        options["channel"] = channel
    try:
        browser = playwright.chromium.launch(**options)
    except Exception as exc:
        return exc
    try:
        browser.close()
    except Exception:
        pass
    return None


def trial(playwright, headless=False, channel=None):
    """Проверка запуска. Возвращает (ошибка_или_None, описание).

    Если включён headless, а Playwright (>= 1.49) не нашёл отдельно поставляемую
    chrome-headless-shell, пробуем headed: бинарник Chromium тот же.
    """
    error = trial_launch(playwright, headless=headless, channel=channel)
    if error is not None and headless and "headless-shell" in str(error).lower():
        fallback = trial_launch(playwright, headless=False, channel=channel)
        return fallback, ("headed (headless-оболочки нет)" if fallback is None else "браузер не запускается")
    return error, ("headless" if headless else "headed (с окном)")


def probe_channels(playwright):
    """Какие браузеры реально запускаются (проверка в headless, без окон)."""
    results = {}
    for channel in (None, "chrome", "msedge"):
        error = trial_launch(playwright, headless=True, channel=channel)
        results[channel] = error
    return results


def ensure_browser(args):
    """True, если браузер реально запускается. Иначе печатает, что делать."""
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
                    print("    Или используй --import: перенос куки из обычного Chrome.")
                    return False
                print(f"  OK Chromium на месте: {exe}")

            if args.channel != "auto":
                error = trial_launch(playwright, headless=False, channel=CHANNEL_CANDIDATES[args.channel][0])
                if error is not None:
                    print_failure("Браузер не запускается", error)
                    return False
                print(f"  OK {CHANNEL_TITLES[CHANNEL_CANDIDATES[args.channel][0]]}: запускается с окном")
                return True

            results = probe_channels(playwright)
            usable = [channel for channel, error in results.items() if error is None]
            if not usable:
                print("  ! Ни один браузер не запускается.")
                for channel, error in results.items():
                    if error is not None:
                        print_failure(CHANNEL_TITLES[channel], error)
                if browser_hint(str(next(iter(results.values())))) and (
                        args.install or confirm("    Попробовать доустановить Chromium? [Y/n]: ")):
                    install_chromium(with_deps=(os.name != "nt"))
                    results = probe_channels(playwright)
                    usable = [channel for channel, error in results.items() if error is None]
                if not usable:
                    print("    Обходной путь без браузера: --import (перенос куки из обычного Chrome).")
                    return False
            print("  OK доступно:", ", ".join(CHANNEL_TITLES[channel] for channel in usable))
            print(f"    будет использован: {CHANNEL_TITLES[next(iter(usable))]}")
    except Exception as exc:
        print_failure("Проверка окружения не удалась", exc)
        return False
    return True


def do_check(channel="auto"):
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

            results = probe_channels(playwright)
            for channel_name, error in results.items():
                title = CHANNEL_TITLES[channel_name]
                if error is None:
                    print(f"  {title}: запускается")
                else:
                    print(f"  {title}: недоступен")
                    print_failure("Причина", error, extra=(channel_name is None))
            if all(error is not None for error in results.values()):
                ok = False
            if channel != "auto":
                wanted = CHANNEL_CANDIDATES[channel][0]
                if results.get(wanted) is not None:
                    print(f"  ! Запрошенный браузер ({CHANNEL_TITLES[wanted]}) недоступен, "
                          "а с --channel запасных вариантов нет.")
                    ok = False
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


# ── Куки: импорт из обычного браузера ────────────────────────────────────────

SAME_SITE_MAP = {
    "no_restriction": "None", "none": "None",
    "lax": "Lax", "unspecified": "Lax", "": "Lax",
    "strict": "Strict",
}


def is_arena_host(value):
    host = (value or "").lstrip(".").split("://")[-1].split("/")[0]
    return host == "arena.ai" or host.endswith(".arena.ai")


def keep_arena_only(state):
    state["cookies"] = [c for c in state.get("cookies", []) if is_arena_host(c.get("domain"))]
    state["origins"] = [o for o in state.get("origins", []) if is_arena_host(o.get("origin", ""))]
    return state


def normalize_cookie(raw, all_domains=False):
    """Cookie-Editor/EditThisCookie -> формат Playwright add_cookies()."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not name:
        return None
    domain = str(raw.get("domain") or "arena.ai").strip()
    if not all_domains and not is_arena_host(domain):
        return None
    cookie = {
        "name": str(name),
        "value": "" if raw.get("value") is None else str(raw.get("value")),
        "domain": domain,
        "path": str(raw.get("path") or "/"),
        "httpOnly": bool(raw.get("httpOnly")),
        "secure": bool(raw.get("secure")),
        "sameSite": SAME_SITE_MAP.get(str(raw.get("sameSite", "")).strip().lower().replace(" ", "_"), "Lax"),
    }
    expires = raw.get("expires", raw.get("expirationDate"))
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0:
        cookie["expires"] = float(expires)
    # SameSite=None без Secure Chromium отклонит — понижаем до Lax.
    if cookie["sameSite"] == "None" and not cookie["secure"]:
        cookie["sameSite"] = "Lax"
    return cookie


def convert_export(data, all_domains=False):
    """Принимает экспорт Cookie-Editor/EditThisCookie или уже готовый storage_state."""
    if isinstance(data, dict):
        raw_cookies = data.get("cookies", [])
        origins = data.get("origins", []) if all_domains else []
    elif isinstance(data, list):
        raw_cookies, origins = data, []
    else:
        raise ValueError("Ожидался JSON-массив куки или объект с полем \"cookies\"")
    cookies = [c for c in (normalize_cookie(raw, all_domains) for raw in raw_cookies) if c]
    return {"cookies": cookies, "origins": list(origins)}


def next_free_index(directory):
    index = 1
    while (directory / f"account_{index}.json").exists():
        index += 1
    return index


def import_file(source, cookies_dir, all_domains):
    """--import: пишет account_N.json из экспорта обычного браузера. Возвращает путь."""
    path = Path(source).expanduser()
    if not path.is_file():
        print(f"  ! Файл не найден: {path}")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print_failure("Не удалось прочитать JSON", exc)
        return None
    try:
        state = convert_export(data, all_domains)
    except ValueError as exc:
        print(f"  ! {exc}")
        return None

    names = [c["name"] for c in state["cookies"]]
    if not names:
        scope = "куки" if all_domains else "куки домена arena.ai"
        print(f"  ! В файле нет подходящих куки ({scope}).")
        if not all_domains:
            print("    Убедись, что экспорт сделан для arena.ai, либо повтори с --all.")
        return None

    cookies_dir.mkdir(parents=True, exist_ok=True)
    target = cookies_dir / f"account_{next_free_index(cookies_dir)}.json"
    target.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"  + сохранено: {target.name} (куки: {len(names)})")
    print(f"    {', '.join(names[:8])}{' ...' if len(names) > 8 else ''}")
    if not any(re.search(r"session|token|auth|client", name, re.I) for name in names):
        print("  ! Среди куки нет похожих на сессионные (session/token/auth/client).")
        print("    Если в генераторе будет «не авторизован» — переэкспортируй с включёнными HttpOnly-куки.")
    return target


# ── Проверка страницы и входа ────────────────────────────────────────────────

def page_text(page):
    try:
        return page.content().lower()
    except Exception:
        return ""


def checkpoint_code(text):
    match = re.search(r"code\s*(\d{1,4})", text)
    return match.group(1) if match else None


def checkpoint_seen(page):
    text = page_text(page)
    return any(marker in text for marker in CHECKPOINT_MARKERS)


def wait_out_checkpoint(page, seconds=None):
    """Vercel Security Checkpoint обычно пропускает страницу сам через несколько секунд."""
    deadline = time.monotonic() + (CHECKPOINT_SECONDS if seconds is None else seconds)
    while time.monotonic() < deadline:
        if not checkpoint_seen(page):
            return True
        page.wait_for_timeout(1000)
    return not checkpoint_seen(page)


def print_checkpoint_help(page, url):
    text = page_text(page)
    code = checkpoint_code(text)
    print("  ! Arena не пропустила браузер: 'Failed to verify your browser'"
          + (f", Code {code}" if code else ""))
    print("    Это Vercel Security Checkpoint (антибот-защита), а не ошибка скрипта. Что делать:")
    print("      1) подожди 10-30 секунд и нажми Enter ещё раз — чекап часто пропускает сам;")
    print("      2) перезапусти с реальным Chrome: python auth.py --channel chrome")
    print("         (встроенный Chromium палится чаще; Chrome/Edge проходят лучше);")
    print("      3) выключи VPN и попробуй другую сеть — «засвеченные» IP чекап не пропускает;")
    print("      4) надёжный обход: залогинься в обычном Chrome и перенеси куки:")
    print("         python auth.py --import экспорт_из_Cookie_Editor.json")
    print(f"    (адрес проверки: {url})")


def open_arena(page, url):
    """Открывает Arena и, если сработал чекап, ждёт/подсказывает. True — страница доступна."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=LOGIN_TIMEOUT_MS)
    except Exception as exc:
        print_failure("Не удалось открыть Arena", exc)
        print("    Проверь интернет/VPN. Если сайт открывается в обычном браузере —")
        print("    проблема в сети или в прокси; для переноса куки есть --import.")
        return False
    if checkpoint_seen(page):
        print("  ... Arena проверяет браузер (Vercel Security Checkpoint), жду...")
        if wait_out_checkpoint(page):
            print("  OK проверка пройдена")
            return True
        print_checkpoint_help(page, url)
        return True          # пользователь может успеть пройти вручную; логин проверим после Enter
    return True


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


def apply_stealth(page):
    """Прячем navigator.webdriver (плюс playwright-stealth, если установлен)."""
    applied = []
    try:
        page.add_init_script(STEALTH_SCRIPT)
        applied.append("init-script")
    except Exception:
        pass
    for import_path, call in (("Stealth", lambda module: module.Stealth().apply_stealth_sync(page)),
                              ("stealth_sync", lambda module: module.stealth_sync(page))):
        try:
            module = __import__("playwright_stealth", fromlist=[import_path])
            getattr(module, import_path)
            call(module)
            applied.append("playwright-stealth")
            break
        except Exception:
            continue
    return ", ".join(applied) if applied else None


class Session:
    """Открытая сессия браузера: либо общий browser + контексты, либо профиль."""

    def __init__(self, browser=None, persistent=None, label="", channel_title=""):
        self.browser = browser
        self.persistent = persistent
        self.label = label
        self.channel_title = channel_title


CONTEXT_OPTIONS = dict(locale="en-US", viewport={"width": 1600, "height": 1000})


def open_session(playwright, channel="auto", profile=None):
    """Поднимает браузер. Ошибки не глотает: если ничего не запустилось, кидает последнюю."""
    last_error = None
    for candidate in CHANNEL_CANDIDATES.get(channel, (None,)):
        options = {
            "headless": False,
            "args": list(ANTI_AUTOMATION_ARGS),
            "ignore_default_args": list(IGNORE_DEFAULT_ARGS),
        }
        if candidate:
            options["channel"] = candidate
        title = CHANNEL_TITLES[candidate]
        try:
            if profile:
                context = playwright.chromium.launch_persistent_context(
                    str(profile), **options, **CONTEXT_OPTIONS)
                return Session(persistent=context, label=f"профиль {profile}",
                               channel_title=title)
            browser = playwright.chromium.launch(**options)
            return Session(browser=browser, channel_title=title)
        except Exception as exc:
            last_error = exc
            print(f"  . {title}: не удалось ({one_line(exc, 120)})")
    raise last_error if last_error else RuntimeError("Не удалось запустить браузер")


def session_context(session):
    """Возвращает (контекст, создан_ли_сейчас). Для профиля — сам профиль."""
    if session.persistent is not None:
        return session.persistent, False
    context = session.browser.new_context(**CONTEXT_OPTIONS)
    return context, True


def collect(playwright, cookies_dir, start_url, all_domains, verify, args):
    index = next_free_index(cookies_dir)
    saved = []
    print(f"Папка куки: {cookies_dir}")
    print(f"Первый аккаунт: #{index}")

    profile = None
    if args.profile:
        profile = Path(args.profile).expanduser() if args.profile != "__default__" else DEFAULT_PROFILE_DIR
        print(f"Профиль: {profile} (один аккаунт за запуск)")

    try:
        session = open_session(playwright, args.channel, profile)
    except Exception as exc:
        print_failure("Не удалось запустить браузер", exc)
        print("    Обходной путь без браузера: python auth.py --import файл_куки.json")
        return saved
    print(f"Браузер: {session.channel_title}{' , ' + session.label if session.label else ''}")
    if session.persistent is not None:
        print("  (постоянный профиль: Arena-чекап и логин сохраняются между запусками)")

    try:
        while True:
            context, owned = session_context(session)
            try:
                page = context.pages[0] if (session.persistent is not None and context.pages) else context.new_page()
                stealth = apply_stealth(page)
                if stealth:
                    print(f"  маскировка автоматизации: {stealth}")
                if not open_arena(page, start_url):
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
                    if checkpoint_seen(page):
                        print_checkpoint_help(page, start_url)
                    if not confirm("    Сохранить всё равно? [y/N]: "):
                        continue
                elif verify and not confirmed:
                    print("  ! Вход не подтверждён (меню аккаунта не найдено).")
                    if not confirm("    Сохранить всё равно? [Y/n]: "):
                        continue
                elif not added:
                    # При подтверждённом входе это лишь замечание: часть сессионных
                    # куки могла появиться ещё до нажатия Enter.
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
                if owned:
                    try:
                        context.close()
                    except Exception:
                        pass

            if session.persistent is not None:
                print("\nПрофиль переиспользуется одним аккаунтом за запуск — завершаю.")
                break

            try:
                answer = input("\nEnter - следующий аккаунт, q - выход: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if answer in ("q", "quit", "exit", "й"):
                break
    finally:
        try:
            if session.browser is not None:
                session.browser.close()
            elif session.persistent is not None:
                session.persistent.close()
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
    parser.add_argument("--channel", choices=tuple(CHANNEL_CANDIDATES), default="auto",
                        help="какой браузер запускать (auto: Chrome -> Edge -> Chromium)")
    parser.add_argument("--profile", nargs="?", const="__default__", default=None, metavar="DIR",
                        help="постоянный профиль (по умолчанию auth_profile/), один аккаунт за запуск")
    parser.add_argument("--import", dest="import_path", metavar="FILE",
                        help="перенести куки из экспорта Cookie-Editor/EditThisCookie (без браузера)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    cookies_dir = Path(args.dir).expanduser()

    if args.check:
        return do_check(args.channel)

    # Импорт куки — без браузера и без Playwright.
    if args.import_path:
        try:
            cookies_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            print_failure(f"Не удалось создать папку {cookies_dir}", exc)
            return 1
        return 0 if import_file(args.import_path, cookies_dir.resolve(), args.all) else 1

    sync_playwright = load_playwright()
    if sync_playwright is None:
        return 1
    if args.install:
        install_chromium(with_deps=(os.name != "nt"))
    if not ensure_browser(args):
        print("\nПодсказка: запусти 'python auth.py --check' и покажи вывод целиком.")
        return 2

    try:
        cookies_dir.mkdir(parents=True, exist_ok=True)     # storage_state() папку НЕ создаёт
    except Exception as exc:
        print_failure(f"Не удалось создать папку {cookies_dir}", exc)
        return 1

    try:
        with sync_playwright() as playwright:
            saved = collect(playwright, cookies_dir.resolve(), args.url, args.all, not args.no_verify, args)
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
        print("Проверить, что Arena видит вход: python diagnose_arena.py (если есть в проекте)")
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
