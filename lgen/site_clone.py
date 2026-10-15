"""Режим «Клон сайта» для бота LGen: безопасная подготовка промпта.

Модуль отвечает только за подготовку. Генерацию выполняет обычный генератор
лендингов (`ArenaGenerator.generate_landing`), то есть ключ и бэкенд LGen.

Что проверяется кодом (а не только предупреждением в интерфейсе):
  * только http/https, порты 80/443, без логина и пароля в URL;
  * запрет IP-литералов, localhost и доменов во внутренних зонах;
  * запрет адресов, которые резолвятся во внутреннюю сеть (защита от SSRF);
  * каждый редирект и каждый CSS-файл проверяются заново;
  * отказ для страниц входа, оплаты, банков и почты (по домену и пути);
  * отказ, если в HTML есть поля пароля, карты или одноразового кода;
  * отказ для страниц, которые собираются только JavaScript (их форм не видно);
  * изображения, видео и шрифты сторонних сайтов не скачиваются;
  * страница не хостится: пользователь получает архив результата генерации;
  * лимиты: cooldown между запросами и суточная квота на пользователя.

Эти проверки снижают риск, но не гарантируют, что их нельзя обойти.
Ответственность за использование результата несёт пользователь.
"""

import html as _html
import ipaddress
import os
import re
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_URL_LEN = 2048
MAX_HTML_BYTES = 2_000_000
MAX_CSS_BYTES_TOTAL = 300_000
MAX_CSS_FILES = 8
MAX_REDIRECTS = 3
MAX_PROMPT_CHARS = 12_000
ALLOWED_PORTS = (80, 443)
FETCH_TIMEOUT_SECONDS = 20
USER_AGENT = "Mozilla/5.0 (compatible; LGenCloneBot/1.0; design-reference)"

CLONE_COOLDOWN_SECONDS = int(os.getenv("CLONE_COOLDOWN_SECONDS", "120"))
CLONE_DAILY_LIMIT = int(os.getenv("CLONE_DAILY_LIMIT", "5"))

DENY_TEXT = (
    "Этот адрес похож на страницу входа, оплаты, банка или почты. "
    "Клонирование таких страниц запрещено."
)

# Префиксы меток домена (кроме последней, TLD): банки, платежи, вход, почта
_DENY_LABEL_PREFIXES = (
    "bank", "sber", "tinkoff", "alfa", "vtb", "gazprom", "raiffeisen", "gosuslug",
    "paypal", "stripe", "pay", "wallet", "crypto", "binance",
    "login", "signin", "sign-in", "auth", "oauth", "sso", "passport", "webmail",
)
_DENY_LABEL_EXACT = ("id", "mail", "account", "accounts", "signin")
_DENY_PATH_KEYWORDS = (
    "/login", "/signin", "/sign-in", "/sign_in", "/log-in", "/auth", "/oauth",
    "/account", "/password", "/reset", "/verify", "/2fa", "/sso", "/checkout",
    "/payment", "/pay/", "/wallet", "/otp",
)
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".corp")

_SENSITIVE_RES = (
    (re.compile(r"<input[^>]*type\s*=\s*[\"']?password", re.I), "поле пароля"),
    (re.compile(
        r"autocomplete\s*=\s*[\"']?(cc-number|cc-csc|cc-exp|cc-name|one-time-code|"
        r"current-password|new-password)", re.I),
     "поля карты, пароля или одноразового кода"),
    (re.compile(
        r"(?:name|id|placeholder)\s*=\s*[\"'][^\"']*(?:card[-_ ]?number|cvv|cvc|iban|"
        r"\botp\b|pin[-_ ]?code)", re.I),
     "поля платёжных данных или кода"),
)
_LOGIN_TITLE_RE = re.compile(r"sign[ -]?in|log[ -]?in|войти|вход|авториз", re.I)
_EMAIL_INPUT_RE = re.compile(r"<input[^>]*type\s*=\s*[\"']?(?:email|tel)", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.I | re.S)
_SHELL_RE = re.compile(r"<div[^>]+id=[\"'](?:root|app|__next|q-app)[\"']", re.I)
_HEAD_RE = re.compile(r"<(h1|h2|h3)[^>]*>(.*?)</\1>", re.I | re.S)
_ANCHOR_RE = re.compile(r"<a[^>]*>(.*?)</a>", re.I | re.S)
_META_DESC_RE = re.compile(
    r"<meta[^>]+name=[\"']description[\"'][^>]*content=[\"']([^\"']{0,400})", re.I)
_STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.I | re.S)
_LINK_STYLESHEET_RE = re.compile(r"<link[^>]+rel=[\"']?stylesheet[^>]*>", re.I)
_HREF_RE = re.compile(r"href=[\"']([^\"']+)", re.I)
_COLOR_RE = re.compile(
    r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgba?\([^)]{5,60}\)|hsla?\([^)]{5,60}\)")
_FONT_RE = re.compile(r"font-family\s*:\s*([^;}{]{3,120})", re.I)


class CloneRefused(Exception):
    """Отказ с текстом, который можно показать пользователю."""


def _default_resolver(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise CloneRefused("Не удалось найти такой сайт (домен не резолвится).")
    return sorted({info[4][0] for info in infos})


def _looks_like_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_public_ip(ip_text):
    ip = ipaddress.ip_address(ip_text.split("%")[0])
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def host_of(url):
    return urlsplit(url).hostname or ""


def validate_url(raw, resolver=_default_resolver):
    """Проверяет адрес и возвращает нормализованный URL или бросает CloneRefused."""
    text = (raw or "").strip()
    if not text or len(text) > MAX_URL_LEN:
        raise CloneRefused(f"Пришлите ссылку на сайт (не длиннее {MAX_URL_LEN} символов).")
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        raise CloneRefused("Разрешены только адреса http:// и https://.")
    if parts.username or parts.password:
        raise CloneRefused("Адрес не должен содержать логин и пароль.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise CloneRefused("В ссылке не найден домен.")
    try:
        port = parts.port
    except ValueError:
        raise CloneRefused("Некорректный порт в ссылке.")
    if port is not None and port not in ALLOWED_PORTS:
        raise CloneRefused("Разрешены только стандартные порты 80 и 443.")
    if "." not in host or host.endswith(_LOCAL_SUFFIXES):
        raise CloneRefused("Локальные адреса не поддерживаются — укажите публичный домен.")
    if _looks_like_ip(host):
        raise CloneRefused("Адрес по IP не поддерживается — укажите домен.")

    labels = host.split(".")[:-1]  # последнюю метку (TLD) не проверяем
    if any(label in _DENY_LABEL_EXACT or label.startswith(_DENY_LABEL_PREFIXES)
           for label in labels):
        raise CloneRefused(DENY_TEXT)
    path = (parts.path or "/").lower()
    if any(keyword in path for keyword in _DENY_PATH_KEYWORDS):
        raise CloneRefused(DENY_TEXT)

    addresses = resolver(host)
    if not addresses:
        raise CloneRefused("Не удалось определить адрес сайта.")
    if not all(_is_public_ip(a) for a in addresses):
        raise CloneRefused("Адрес ведёт во внутреннюю сеть — клонирование отключено.")

    scheme = parts.scheme.lower()
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def _extract_title(html_text):
    m = _TITLE_RE.search(html_text or "")
    return _clean(m.group(1)) if m else ""


def detect_sensitive(html_text, title=""):
    """Возвращает причину отказа, если страница похожа на вход или оплату."""
    for rx, what in _SENSITIVE_RES:
        if rx.search(html_text):
            return f"Страница содержит {what}. Это похоже на вход или оплату — клонирование запрещено."
    if _LOGIN_TITLE_RE.search(title or "") and _EMAIL_INPUT_RE.search(html_text):
        return "Страница похожа на форму входа — клонирование запрещено."
    return None


def _visible_text(html_text):
    body = _SCRIPT_STYLE_RE.sub(" ", html_text or "")
    body = _TAG_RE.sub(" ", body)
    return " ".join(_html.unescape(body).split())


def looks_like_js_shell(html_text):
    """Страница — пустая оболочка SPA: формы и текст появятся только в браузере."""
    return len(_visible_text(html_text)) < 200 and _SHELL_RE.search(html_text or "") is not None


def _clean(fragment, limit=200):
    return " ".join(_html.unescape(_TAG_RE.sub(" ", fragment or "")).split())[:limit]


def _unique(items, limit):
    seen, out = set(), []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
        if len(out) >= limit:
            break
    return out


def build_snapshot(url, html_text, css_texts):
    """Собирает краткое описание дизайна страницы (без картинок и медиа)."""
    css = "\n".join(css_texts)
    meta = _META_DESC_RE.search(html_text or "")
    links = [t for t in (_clean(a, 40) for a in _ANCHOR_RE.findall(html_text or "")) if t]
    fonts = [f.strip().strip("'\"") for f in _FONT_RE.findall(css)]
    return {
        "url": url,
        "title": _extract_title(html_text),
        "description": _html.unescape(meta.group(1)).strip() if meta else "",
        "headings": _unique((_clean(h[1], 120) for h in _HEAD_RE.findall(html_text or "")), 15),
        "links": _unique(links, 15),
        "colors": _unique(_COLOR_RE.findall(css), 20),
        "fonts": _unique(fonts, 5),
        "sections": len(re.findall(r"<section\b", html_text or "", re.I)),
        "images": len(re.findall(r"<img\b", html_text or "", re.I)),
        "text": _visible_text(html_text)[:800],
        "css": css[:6000],
    }


CLONE_RULES = (
    "Создай один адаптивный лендинг (HTML, CSS и при необходимости небольшой JS), "
    "который повторяет компоновку, цветовую схему, типографику и отступы описанного сайта. "
    "Правила: не копируй логотипы, товарные знаки, фирменные тексты и изображения, "
    "используй нейтральные заглушки и собственный текст; не добавляй формы для паролей, "
    "платёжных данных и одноразовых кодов; не добавляй скрипты отслеживания и сторонние трекеры; "
    "верни результат архивом с файлами."
)


def build_clone_prompt(snap, limit=MAX_PROMPT_CHARS):
    """Собирает промпт для generate_landing из снимка дизайна."""
    lines = [
        CLONE_RULES,
        "",
        f"Ориентир по дизайну: {snap.get('url', '')}",
        f"Заголовок страницы: {snap.get('title', '')}",
    ]
    if snap.get("description"):
        lines.append(f"Описание: {snap['description']}")
    if snap.get("headings"):
        lines.append("Заголовки разделов: " + " | ".join(snap["headings"]))
    if snap.get("links"):
        lines.append("Пункты навигации: " + " | ".join(snap["links"]))
    lines.append(f"Секций: {snap.get('sections', 0)}, изображений на оригинале: {snap.get('images', 0)} "
                 "(изображения не копируй, используй плейсхолдеры той же формы)")
    if snap.get("colors"):
        lines.append("Цвета: " + ", ".join(snap["colors"]))
    if snap.get("fonts"):
        lines.append("Шрифты: " + ", ".join(snap["fonts"]))
    if snap.get("text"):
        lines.append("Пример текста с оригинала (для стиля, не для копирования): " + snap["text"][:600])
    if snap.get("css"):
        lines.append("Фрагмент CSS для ориентира:\n" + snap["css"][:4000])
    return "\n".join(lines)[:limit]


async def _get_following_redirects(session, url, resolver, limit):
    """GET без cookies, редиректы вручную с проверкой каждого адреса."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        async with session.get(current, allow_redirects=False) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if not location:
                    raise CloneRefused("Сайт вернул редирект без адреса.")
                current = validate_url(urljoin(current, location), resolver=resolver)
                continue
            if resp.status != 200:
                raise CloneRefused(f"Сайт ответил HTTP {resp.status}.")
            ctype = resp.headers.get("Content-Type", "")
            raw = await resp.content.read(limit)
            return raw.decode(resp.charset or "utf-8", errors="replace"), ctype, current
    raise CloneRefused("Слишком много перенаправлений.")


async def fetch_snapshot(raw_url, resolver=_default_resolver):
    """Загружает страницу и CSS, проверяет и возвращает снимок дизайна."""
    import aiohttp  # импорт внутри функции: модуль тестируется без сети и aiohttp

    url = validate_url(raw_url, resolver=resolver)
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,text/css,*/*;q=0.5"}
    async with aiohttp.ClientSession(
        timeout=timeout, headers=headers, cookie_jar=aiohttp.DummyCookieJar()
    ) as session:
        html_text, ctype, final_url = await _get_following_redirects(
            session, url, resolver, MAX_HTML_BYTES)
        if "text/html" not in ctype.lower():
            raise CloneRefused("По ссылке не HTML-страница.")

        css_parts = list(_STYLE_BLOCK_RE.findall(html_text))
        hrefs = []
        for tag in _LINK_STYLESHEET_RE.findall(html_text):
            m = _HREF_RE.search(tag)
            if m:
                hrefs.append(urljoin(final_url, _html.unescape(m.group(1))))
        total = 0
        for href in hrefs[:MAX_CSS_FILES]:
            if total >= MAX_CSS_BYTES_TOTAL:
                break
            try:
                css, css_ctype, _ = await _get_following_redirects(
                    session, validate_url(href, resolver=resolver), resolver,
                    MAX_CSS_BYTES_TOTAL - total)
            except Exception:
                continue  # недоступный или запрещённый CSS пропускаем
            if "css" not in css_ctype.lower():
                continue
            css_parts.append(css)
            total += len(css.encode("utf-8"))

    if looks_like_js_shell(html_text):
        raise CloneRefused(
            "Страница собирается JavaScript, и её формы не видны на сервере. "
            "Такие сайты пока не поддерживаются.")
    reason = detect_sensitive(html_text, _extract_title(html_text))
    if reason:
        raise CloneRefused(reason)
    return build_snapshot(final_url, html_text, css_parts)


class CloneRateLimiter:
    """Cooldown и суточная квота на пользователя (в памяти процесса)."""

    def __init__(self, cooldown=CLONE_COOLDOWN_SECONDS, daily_limit=CLONE_DAILY_LIMIT,
                 clock=time.time):
        self.cooldown = cooldown
        self.daily_limit = daily_limit
        self.clock = clock
        self._history = {}

    def _recent(self, user_id):
        now = self.clock()
        hist = [t for t in self._history.get(user_id, []) if now - t < 86400]
        self._history[user_id] = hist
        return now, hist

    def check(self, user_id):
        """None, если можно начать; иначе текст причины для пользователя."""
        now, hist = self._recent(user_id)
        if hist and now - hist[-1] < self.cooldown:
            wait = int(self.cooldown - (now - hist[-1])) + 1
            return f"Подождите {wait} с перед следующим клонированием."
        if len(hist) >= self.daily_limit:
            return f"Лимит клонирований на сутки исчерпан ({self.daily_limit}). Попробуйте завтра."
        return None

    def record(self, user_id):
        self._recent(user_id)
        self._history[user_id].append(self.clock())


clone_limiter = CloneRateLimiter()
