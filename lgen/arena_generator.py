"""
arena_generator.py — надёжная обёртка над arena.ai (Playwright).

Что изменилось по сравнению с версией 1.x (почему падали фото/видео/анимация):

1.  Согласие (Terms of Use / cookies) закрывается ДО ввода промпта и, при
    необходимости, повторно перед отправкой. На arena.ai модалка «Terms of Use &
    Privacy Policy» и баннер «…hit Enter on your keyboard to agree» полностью
    блокируют composer: кнопка отправки остаётся disabled и генерация не стартует
    вообще. Раньше в этом случае код молча ждал таймаут.
2.  Отправка промпта переписана: сначала Enter (он работает всегда), затем кнопка
    «Send message», затем — закрытие согласия и повтор. Раньше клик по кнопке мог
    попасть в «промежуточный» экран безопасности, а Enter-next-клик по «Agree»
    просто закрывал модалку, и отправки не происходило. Успешная отправка теперь
    ПРОВЕРЯЕТСЯ (пустой textarea / индикатор Generating / кнопка Stop / /c/ в URL),
    и если промпт не ушёл — выбрасывается исключение (а не тихий таймаут).
3.  Результат забирается несколькими стратегиями:
      1) перехват сетевых ответов (картинки/видео с CDN) + SSE-поток
         `/nextjs-api/stream` (строки `a2:` содержат URL результата, `a3:`/`ae:` —
         ошибки модерации/провайдера);
      2) клик по кнопкам скачивания (в т.ч. иконка слева от кнопки «Edit» —
         именно так устроена карточка результата Arena) с ловлей download-событий;
      3) скачивание найденного URL через контекст браузера (`page.request.get`) —
         наследует cookies, поэтому работает и для приватных/подписанных CDN-ссылок;
      4) для blob:/data: — fetch внутри страницы;
      5) последний резерв — скриншот элемента изображения.
4.  Доменные селекторы берутся точечно: `textarea[placeholder^='Describe the image']`
    для картинок, `textarea[placeholder*='Describe your video']` для видео,
    `button[aria-label='Send message']` для отправки, `button[aria-label='Stop generation']`
    как индикатор генерации.
5.  Исправлен `generate_image`, который при ошибке возвращал `None` вместо raise —
    из-за этого бот не отправлял сообщение об ошибке и не возвращал токены.
"""

import os
import re
import json
import time
import base64
import random
import asyncio
import logging
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

try:  # playwright-stealth >= 1.0.6
    from playwright_stealth import Stealth
except Exception:  # pragma: no cover
    Stealth = None  # type: ignore

from config import COOKIES_PATH, ARENA_TIMEOUT

try:  # новые лимиты из config.py (могут отсутствовать в старых .env)
    from config import IMAGE_TIMEOUT, VIDEO_TIMEOUT, TEXT_TIMEOUT  # type: ignore
except Exception:  # pragma: no cover
    IMAGE_TIMEOUT, VIDEO_TIMEOUT, TEXT_TIMEOUT = 600, 1800, 300

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# URL-маппинг по типу генерации
# ─────────────────────────────────────────────────────────────
ARENA_URLS = {
    "code":  "https://arena.ai/code/direct?model_a=max",
    "text":  "https://arena.ai/text/direct?model_a=max",
    "image": "https://arena.ai/image/direct?model_a=max",
    "video": "https://arena.ai/video",            # видео всегда 2 варианта
    "prompt": "https://arena.ai/text/direct?model_a=max",  # для генерации промтов
}

# Допустимые «direct»-страницы Arena (если редиректит на battle/root — надо вернуться)
ARENA_DIRECT_PATHS = ("/text/direct", "/direct", "/image/direct", "/code/direct", "/search/direct", "/video")

# Сколько вариантов видео отдаёт Arena
VIDEO_VARIANTS = int(os.getenv("ARENA_VIDEO_VARIANTS", "2"))

# Расширения / mime-типы медиа
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".bmp")
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".m4v")

# Отсекаем иконки/аватарки/логотипы — их нельзя путать с результатом генерации
NEGATIVE_MEDIA_RE = re.compile(
    r"(favicon|sprite|logo|avatar|placeholder|spinner|loader|loading|blurhash|1x1\.|pixel\.gif"
    r"|icons?[-_.]|apple-touch|og-image|emoji|flag|gravatar|profile[-_]?(pic|photo)|badge|watermark"
    r"|/static/|/favicon|sentry|analytics|/icons/)",
    re.I,
)

# Признаки того, что модалка согласия блокирует страницу
CONSENT_TEXT_RE = re.compile(
    r"hit enter on your keyboard to agree|terms of use|privacy policy|by clicking .?agree"
    r"|manage cookie preferences|accept cookies|i understand|do not submit to our services",
    re.I,
)

# Кнопки, которыми закрывается согласие/уведомления
CONSENT_BUTTON_LABELS = [
    "Agree", "I Agree", "I agree", "I understand", "Accept Cookies", "Accept All Cookies",
    "Accept all", "Accept", "Got it", "Save Preferences", "Close", "Continue", "OK",
]
CONSENT_SAFE_RE = re.compile(r"^(agree|i agree|i understand|accept|accept all|accept cookies|accept all cookies|got it|save preferences|close|continue|ok)$", re.I)

# Кнопки, которые НЕЛЬЗЯ нажимать при поиске кнопки скачивания
DANGEROUS_BUTTON_RE = re.compile(r"(delete|remove|trash|share|report|publish|copy|regenerate|retry|hide|discard)", re.I)

# Ключи, в которых Arena отдаёт ссылки на результат внутри SSE/JSON
MEDIA_JSON_KEYS = ("image", "image_url", "imageUrl", "images", "video", "video_url", "videoUrl",
                   "videos", "url", "downloadUrl", "download_url", "src", "output")

URL_RE = re.compile(r"https?://[^\s\"'<>\\\\)\],}]+", re.I)


def _ext_from_content_type(content_type: str, default: str = ".png") -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/avif": ".avif",
        "image/gif": ".gif",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
    }.get(ct, default)


def _ext_from_url(url: str, default: str = ".png") -> str:
    try:
        path = urlparse(url).path.lower()
    except Exception:
        return default
    for ext in VIDEO_EXTS + IMAGE_EXTS:
        if path.endswith(ext):
            return ext
    return default


class ArenaGenerator:
    def __init__(self):
        self.playwright = None
        self.browser: Browser = None
        self.context: BrowserContext = None
        self.page: Page = None
        self.stealth = Stealth() if Stealth else None
        self._page_handlers_attached = False

        # Перехват медиа из сети во время генерации
        self._capture_enabled = False
        self._captured = {"image": [], "video": []}
        self._stream_urls = []
        self._stream_errors = []
        self._stream_tasks = set()
        self._response_tasks = set()

        # Загрузки (download-события) — ловим постоянно
        self._downloads = []
        self._download_dir = None

        # Снимок изображений страницы ДО генерации (чтобы не отдать старое)
        self._baseline_images = set()

        # Тайминги (секунды) — вынесены в атрибуты, чтобы их можно было
        # настраивать через .env или переопределять в тестах
        self.submit_check_seconds = float(os.getenv("ARENA_SUBMIT_CHECK_SECONDS", "10"))
        self.consent_check_interval = float(os.getenv("ARENA_CONSENT_CHECK_SECONDS", "15"))
        self.image_poll_seconds = float(os.getenv("ARENA_IMAGE_POLL_SECONDS", "4"))
        self.video_poll_seconds = float(os.getenv("ARENA_VIDEO_POLL_SECONDS", "8"))
        self.download_wait_seconds = float(os.getenv("ARENA_DOWNLOAD_WAIT_SECONDS", "25"))
        self.download_click_interval = float(os.getenv("ARENA_DOWNLOAD_CLICK_SECONDS", "20"))

    # ═════════════════════════════════════════════════════════
    # Инициализация браузера
    # ═════════════════════════════════════════════════════════
    async def init_browser(self):
        self.playwright = await async_playwright().start()

        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-blink-features=AutomationControlled',
                '--disable-infobars',
                '--window-size=1920,1080',
                '--autoplay-policy=no-user-gesture-required',
            ]
        )

        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/126.0.0.0 Safari/537.36'
            ),
            locale='en-US',
            timezone_id='America/New_York',
            ignore_https_errors=True,
            accept_downloads=True,
        )

        if os.path.exists(COOKIES_PATH):
            try:
                with open(COOKIES_PATH, 'r', encoding='utf-8') as f:
                    cookies = json.load(f)
                if isinstance(cookies, dict):  # storageState-подобный файл
                    cookies = cookies.get("cookies", [])
                for cookie in cookies:
                    if 'sameSite' not in cookie or cookie['sameSite'] not in ('Strict', 'Lax', 'None'):
                        cookie['sameSite'] = 'Lax'
                if cookies:
                    await self.context.add_cookies(cookies)
                    logger.info(f"Cookies loaded from {COOKIES_PATH} ({len(cookies)} шт.)")
            except Exception as e:
                logger.warning(f"Failed to load cookies: {e}")
        else:
            logger.warning(f"Cookies file not found at {COOKIES_PATH}")

        self.page = await self.context.new_page()
        if self.stealth is not None:
            try:
                await self.stealth.apply_stealth_async(self.page)
            except Exception as e:
                logger.debug(f"stealth apply failed: {e}")

        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
            const _origQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (p) =>
                p.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : _origQuery(p);
        """)

        self._attach_page_handlers()
        logger.info("Browser initialized with Stealth mode")

    def _attach_page_handlers(self):
        """Вешаем обработчики сети/загрузок один раз на текущий page/context."""
        if self._page_handlers_attached:
            return
        try:
            self.page.on("response", self._on_response)
            self.page.on("download", self._on_download)
            self.context.on("page", lambda p: self._attach_to_page(p))
        except Exception as e:
            logger.debug(f"handler attach failed: {e}")
        self._page_handlers_attached = True

    async def _attach_to_page(self, page: Page):
        """Новые вкладки (иногда Arena открывает результат в новой вкладке)."""
        try:
            page.on("response", self._on_response)
            page.on("download", self._on_download)
        except Exception:
            pass

    async def close_browser(self):
        for task in list(self._stream_tasks) + list(self._response_tasks):
            task.cancel()
        self._stream_tasks.clear()
        self._response_tasks.clear()
        if self.browser:
            await self.browser.close()
            logger.info("Browser closed")
        if self.playwright:
            await self.playwright.stop()
            logger.info("Playwright stopped")
        self._page_handlers_attached = False

    # ═════════════════════════════════════════════════════════
    # Перехват медиа из сети и download-событий
    # ═════════════════════════════════════════════════════════
    def start_capture(self, download_dir: str = "generated/.downloads"):
        """Начинаем собирать медиа-ответы только на время генерации."""
        self._capture_enabled = True
        self._captured = {"image": [], "video": []}
        self._stream_urls = []
        self._stream_errors = []
        self._downloads = []
        self._download_dir = download_dir
        os.makedirs(download_dir, exist_ok=True)

    def stop_capture(self):
        self._capture_enabled = False

    def _media_kind(self, url: str, content_type: str) -> str:
        if not url or url.startswith("data:"):
            return ""
        if NEGATIVE_MEDIA_RE.search(url):
            return ""
        ct = (content_type or "").split(";")[0].strip().lower()
        if "svg" in ct or urlparse(url).path.lower().endswith(".svg"):
            return ""
        path = urlparse(url).path.lower()
        if ct.startswith("image/") or path.endswith(IMAGE_EXTS):
            return "image"
        if ct.startswith("video/") or path.endswith(VIDEO_EXTS):
            return "video"
        return ""

    def _on_response(self, response):
        """Синхронный колбэк Playwright: кладём URL в очередь на разбор."""
        try:
            task = asyncio.ensure_future(self._handle_response(response))
            self._response_tasks.add(task)
            task.add_done_callback(self._response_tasks.discard)
        except Exception:
            pass

    async def _handle_response(self, response):
        try:
            url = response.url
            if not self._capture_enabled:
                return

            headers = {}
            try:
                headers = await response.all_headers()
            except Exception:
                pass
            content_type = headers.get("content-type", "")

            kind = self._media_kind(url, content_type)
            if kind:
                # отсекаем явно мелкие файлы (иконки), если размер известен
                try:
                    cl = int(headers.get("content-length") or 0)
                except Exception:
                    cl = 0
                min_size = 8_000 if kind == "image" else 60_000
                if cl and cl < min_size:
                    return
                self._captured[kind].append({"url": url, "content_type": content_type, "ts": time.time()})

            # SSE-поток генерации: /nextjs-api/stream содержит прямые URL результата
            if "/nextjs-api/stream" in url or "text/event-stream" in content_type:
                task = asyncio.ensure_future(self._read_stream(response))
                self._stream_tasks.add(task)
                task.add_done_callback(self._stream_tasks.discard)
        except Exception as e:
            logger.debug(f"response handler error: {e}")

    async def _read_stream(self, response):
        """Читаем SSE-поток и вытаскиваем ссылки на результат + ошибки."""
        try:
            text = await asyncio.wait_for(response.text(), timeout=1800)
        except Exception:
            return
        try:
            self._parse_stream_text(text)
        except Exception as e:
            logger.debug(f"stream parse error: {e}")

    def _parse_stream_text(self, text: str):
        if not text:
            return
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            # ошибки Arena: a3: (провайдер) / ae: (платформа/модерация)
            if line.startswith("a3:") or line.startswith("ae:"):
                msg = self._extract_error(line[3:])
                if msg:
                    self._stream_errors.append(msg)
            # полезная нагрузка: a2: {...}
            if line.startswith("a2:"):
                self._collect_urls_from_payload(line[3:], self._stream_urls)

        # на всякий случай — прямые медиа-ссылки в любом месте потока
        for m in URL_RE.finditer(text):
            url = m.group(0).rstrip('.,"')
            if self._media_kind(url, ""):
                self._stream_urls.append(url)
        if self._stream_urls:
            # дедуп, сохраняя порядок
            seen = set()
            self._stream_urls = [u for u in self._stream_urls if not (u in seen or seen.add(u))]

    def _extract_error(self, payload: str) -> str:
        payload = payload.strip()
        try:
            data = json.loads(payload)
            if isinstance(data, str):
                return data[:400]
            if isinstance(data, dict):
                err = data.get("error") or data.get("message")
                if isinstance(err, dict):
                    return str(err.get("message") or err)[:400]
                if err:
                    return str(err)[:400]
        except Exception:
            pass
        return payload[:400] if payload else ""

    def _collect_urls_from_payload(self, payload: str, bucket: list):
        """Достаём URL медиа из JSON-строки SSE (структура может меняться)."""
        payload = payload.strip()
        try:
            data = json.loads(payload)
        except Exception:
            data = None

        if isinstance(data, (dict, list)):
            stack = [data]
            while stack:
                item = stack.pop()
                if isinstance(item, dict):
                    for k, v in item.items():
                        if isinstance(v, (dict, list)):
                            stack.append(v)
                        elif isinstance(v, str) and v.startswith("http"):
                            if k in MEDIA_JSON_KEYS or self._media_kind(v, ""):
                                bucket.append(v)
                elif isinstance(item, list):
                    stack.extend([i for i in item if isinstance(i, (dict, list, str))])
                elif isinstance(item, str) and item.startswith("http"):
                    if self._media_kind(item, ""):
                        bucket.append(item)
        else:
            for m in URL_RE.finditer(payload):
                bucket.append(m.group(0).rstrip('.,"'))

    def _on_download(self, download):
        """Скачивание, инициированное страницей (клик по «Download»)."""
        async def _save():
            try:
                name = (download.suggested_filename or "").strip() or f"arena_{int(time.time())}"
                safe = re.sub(r"[^\w.\-]+", "_", name)
                dest = os.path.join(self._download_dir or "generated", f"{int(time.time())}_{random.randint(0, 9999)}_{safe}")
                await download.save_as(dest)
                self._downloads.append(dest)
                logger.info(f"Download captured → {dest}")
            except Exception as e:
                logger.debug(f"download save failed: {e}")
        try:
            task = asyncio.ensure_future(_save())
            self._response_tasks.add(task)
            task.add_done_callback(self._response_tasks.discard)
        except Exception:
            pass

    # ═════════════════════════════════════════════════════════
    # Вспомогательные модалки: cookies / terms / captcha
    # ═════════════════════════════════════════════════════════
    async def accept_cookies(self):
        """Старое имя оставлено для совместимости."""
        return await self.ensure_consent()

    async def accept_terms(self):
        """Старое имя оставлено для совместимости."""
        return await self.ensure_consent()

    async def _click_buttons_by_labels(self, labels: list) -> bool:
        """JS-клик по видимой кнопке с подходящим текстом/aria-label/title."""
        try:
            clicked = await self.page.evaluate(
                """(labels) => {
                    const wanted = labels.map(l => l.toLowerCase());
                    const vis = (e) => {
                        if (!e) return false;
                        const cs = getComputedStyle(e);
                        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) return false;
                        const r = e.getBoundingClientRect();
                        return r.width > 1 && r.height > 1;
                    };
                    const btns = Array.from(document.querySelectorAll('button, [role="button"], a'));
                    for (const b of btns) {
                        if (!vis(b) || b.disabled) continue;
                        const text = ((b.innerText || b.textContent || '') + ' ' +
                                      (b.getAttribute('aria-label') || '') + ' ' +
                                      (b.getAttribute('title') || '')).trim().toLowerCase();
                        if (!text) continue;
                        if (wanted.some(w => text === w || text.startsWith(w))) {
                            try { b.click(); return text.slice(0, 40); } catch (e) { }
                        }
                    }
                    return '';
                }""",
                labels,
            )
            if clicked:
                logger.info(f"Consent button clicked: '{clicked}' ✅")
                return True
        except Exception as e:
            logger.debug(f"click by labels failed: {e}")
        return False

    async def consent_gate_open(self) -> bool:
        """Открыт ли диалог согласия / блокирующий слой."""
        try:
            return bool(await self.page.evaluate(
                """() => {
                    const vis = (e) => {
                        if (!e) return false;
                        const cs = getComputedStyle(e);
                        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) return false;
                        const r = e.getBoundingClientRect();
                        return r.width > 1 && r.height > 1;
                    };
                    const re = /hit enter on your keyboard to agree|terms of use|privacy policy|by clicking .?agree|manage cookie preferences|accept cookies|i understand/i;
                    const dialogs = Array.from(document.querySelectorAll(
                        "[role='dialog'],[role='alertdialog'],[aria-modal='true'],[data-state='open']"));
                    if (dialogs.filter(vis).some(d => re.test((d.innerText || '').slice(0, 800)))) return true;
                    const body = document.body ? document.body.innerText || '' : '';
                    if (/hit enter on your keyboard to agree/i.test(body)) return true;
                    return false;
                }"""
            ))
        except Exception:
            return False

    async def dismiss_got_it(self) -> bool:
        """Закрываем всплывающие баннеры «Got it» (они перекрывают composer)."""
        return await self._click_buttons_by_labels(["Got it"])

    async def ensure_consent(self, reload_after: bool = False) -> bool:
        """
        Гарантируем, что модалки согласия закрыты.

        На arena.ai модалка Terms of Use держит composer выключенным, поэтому её
        закрываем ПЕРЕД вводом промпта и повторно перед отправкой.
        """
        try:
            if not await self.consent_gate_open():
                await self.dismiss_got_it()
                return True
        except Exception:
            return True

        logger.info("⚠️ Найдена модалка согласия Arena — закрываем...")
        for attempt in range(1, 4):
            clicked = await self._click_buttons_by_labels(CONSENT_BUTTON_LABELS)
            if not clicked:
                # «Or hit Enter on your keyboard to agree» — Enter так же подтверждает
                try:
                    await self.page.keyboard.press("Enter")
                except Exception:
                    pass
            await asyncio.sleep(1.2)

            # cookie-preferences: после Agree появляется кнопка сохранения настроек
            await self._click_buttons_by_labels(["Save Preferences", "Accept All Cookies", "Accept all"])
            await asyncio.sleep(0.8)

            if not await self.consent_gate_open():
                logger.info(f"✅ Модалка согласия закрыта (попытка {attempt})")
                await self.save_cookies()
                if reload_after:
                    try:
                        await self.page.reload(wait_until="domcontentloaded", timeout=60000)
                        await asyncio.sleep(2)
                    except Exception:
                        pass
                await self.dismiss_got_it()
                return True

            # последняя попытка: клик в тело страницы + Enter
            try:
                await self.page.mouse.click(700, 400)
                await asyncio.sleep(0.3)
                await self.page.keyboard.press("Enter")
            except Exception:
                pass
            await asyncio.sleep(1)

        still_open = await self.consent_gate_open()
        if still_open:
            logger.warning("⚠️ Модалку согласия не удалось закрыть — генерация может не стартовать")
        return not still_open

    async def handle_captcha(self, wait_seconds: int = 5):
        """reCAPTCHA / Cloudflare — как раньше, но с защитой от зависания."""
        try:
            if wait_seconds > 0:
                try:
                    await self.page.wait_for_selector(
                        'text=/security verification/i', timeout=wait_seconds * 1000
                    )
                except Exception:
                    return False
            else:
                if not await self.page.query_selector('text=/security verification/i'):
                    # Cloudflare-заглушки тоже считаем капчей
                    cf = await self.page.query_selector(
                        "iframe[src*='challenges.cloudflare.com'], .cf-turnstile, #cf-wrapper"
                    )
                    if not cf:
                        return False
        except Exception:
            return False

        logger.info("🔒 Security Verification (reCAPTCHA/Cloudflare) detected")
        await asyncio.sleep(1.5)

        frame = None
        for _ in range(5):
            for f in self.page.frames:
                if 'recaptcha' in (f.url or '') or 'challenges.cloudflare.com' in (f.url or ''):
                    frame = f
                    break
            if frame:
                break
            await asyncio.sleep(1)

        try:
            if frame:
                anchor = await frame.wait_for_selector('#recaptcha-anchor, input[type="checkbox"], body', timeout=10000)
                if anchor:
                    box = await anchor.bounding_box()
                    if box:
                        x = box['x'] + box['width'] / 2 + random.uniform(-2, 2)
                        y = box['y'] + box['height'] / 2 + random.uniform(-2, 2)
                        await self.page.mouse.move(x, y, steps=12)
                        await asyncio.sleep(random.uniform(0.3, 0.7))
                        await self.page.mouse.click(x, y, delay=120)
                        logger.info("Captcha checkbox clicked (human-like) ✅")
                    else:
                        await anchor.click(delay=120)
            else:
                cb = await self.page.query_selector('[role="checkbox"], input[type="checkbox"]')
                if cb:
                    await cb.click(delay=120)
                    logger.info("Captcha checkbox clicked (page-level) ✅")
        except Exception as e:
            logger.warning(f"Captcha click failed: {e}")
            return False

        try:
            await self.page.wait_for_selector(
                'text=/security verification/i', state='detached', timeout=30000
            )
            logger.info("Captcha modal closed ✅")
        except Exception:
            logger.warning("Captcha modal still visible (possibly image challenge)")

        await self.save_cookies()
        return True

    # ═════════════════════════════════════════════════════════
    # Навигация и ожидание UI
    # ═════════════════════════════════════════════════════════
    async def _navigate_and_wait(self, url: str, ui_selector: str = None, mode: str = None):
        logger.info(f"Navigating to {url}")
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        # Arena иногда редиректит direct-страницы на battle/root — возвращаемся
        try:
            current = self.page.url or ""
            if "arena.ai" in current:
                path = urlparse(current).path.rstrip("/")
                if path in ("", "/"):
                    logger.warning(f"⚠️ Arena редиректнула на battle-страницу ({current}) — повторный переход")
                    await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(2)
        except Exception:
            pass

        logger.info(f"Current URL: {self.page.url}")

        # Модалки / капча / согласие
        await self.ensure_consent(reload_after=True)
        await self.handle_captcha(wait_seconds=3)

        selector = ui_selector or self._prompt_selector_hint(mode)
        try:
            await self.page.wait_for_selector(selector, timeout=40000)
            logger.info("Main UI rendered ✅")
        except Exception:
            logger.warning("Main UI did not render within 40s — continuing anyway")

        await asyncio.sleep(1.5)
        await self._remember_baseline_images()

    def _prompt_selector_hint(self, mode: str = None) -> str:
        if mode == "image":
            return "textarea[placeholder^='Describe the image'], textarea, [contenteditable='true'], [role='textbox']"
        if mode == "video":
            return "textarea[placeholder*='Describe your video'], textarea, [role='textbox']"
        return '#chat-area, form textarea, textarea, [role="textbox"]'

    async def _remember_baseline_images(self):
        """Запоминаем картинки, которые уже были на странице (логотипы, старые результаты)."""
        try:
            srcs = await self.page.evaluate(
                """() => Array.from(document.querySelectorAll('img')).map(i => i.currentSrc || i.src || '')"""
            )
            self._baseline_images = {s for s in srcs if s}
        except Exception:
            self._baseline_images = set()

    # ═════════════════════════════════════════════════════════
    # Ввод промпта и вложения
    # ═════════════════════════════════════════════════════════
    def _prompt_selectors(self, mode: str = None) -> list:
        selectors = []
        if mode == "image":
            selectors += [
                "textarea[placeholder^='Describe the image']",
                "textarea[placeholder*='Describe the image']",
                "textarea[placeholder*='image' i]",
            ]
        elif mode == "video":
            selectors += [
                "textarea[placeholder*='Describe your video']",
                "textarea[placeholder*='video' i]",
            ]
        selectors += [
            "form textarea",
            "textarea",
            "[contenteditable='true']",
            "[role='textbox']",
        ]
        # дедуп без потери порядка
        seen = set()
        return [s for s in selectors if not (s in seen or seen.add(s))]

    async def _find_editor(self, mode: str = None):
        for sel in self._prompt_selectors(mode):
            try:
                el = await self.page.wait_for_selector(sel, timeout=2500)
                if el and await el.is_visible():
                    logger.info(f"Prompt editor found: {sel} ✅")
                    return el
            except Exception:
                continue
        # поиск во фреймах
        for frame in self.page.frames:
            for sel in self._prompt_selectors(mode):
                try:
                    el = await frame.query_selector(sel)
                    if el and await el.is_visible():
                        logger.info(f"Prompt editor found in iframe ({frame.url}): {sel} ✅")
                        return el
                except Exception:
                    continue
        return None

    async def enter_prompt(self, prompt: str, mode: str = None) -> bool:
        logger.info(f"Entering prompt ({mode or 'text'}): {prompt[:80]}...")
        editor = await self._find_editor(mode)
        if not editor:
            try:
                await self.page.screenshot(path="debug_no_input.png")
            except Exception:
                pass
            raise Exception("Could not find prompt input field")

        # 1) fill() — самый надёжный способ, не зависит от фокуса/оверлеев
        try:
            await editor.fill(prompt, timeout=10000)
            value = ""
            try:
                value = await editor.input_value()
            except Exception:
                value = (await editor.text_content() or "")
            if (value or "").strip() == prompt.strip():
                logger.info("Prompt entered via fill() ✅")
                return True
        except Exception as e:
            logger.debug(f"fill() failed: {e}")

        # 2) клик + печать
        try:
            await editor.click(timeout=5000, force=True)
            await asyncio.sleep(0.3)
            await self.page.keyboard.press('Control+A')
            await self.page.keyboard.press('Delete')
            await asyncio.sleep(0.2)
            if len(prompt) <= 400:
                await self.page.keyboard.type(prompt, delay=12)
            else:
                await self.page.keyboard.insert_text(prompt)
            await asyncio.sleep(0.4)
            logger.info("Prompt entered via keyboard ✅")
            return True
        except Exception as e:
            logger.warning(f"keyboard entry failed: {e}")

        # 3) js-вставка в textarea
        try:
            ok = await self.page.evaluate(
                """(text) => {
                    const ta = document.querySelector("textarea");
                    if (!ta) return false;
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                    setter.call(ta, text);
                    ta.dispatchEvent(new Event('input', { bubbles: true }));
                    ta.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }""",
                prompt,
            )
            if ok:
                logger.info("Prompt entered via JS setter ✅")
                await asyncio.sleep(0.5)
                return True
        except Exception as e:
            logger.warning(f"JS prompt entry failed: {e}")

        raise Exception("Could not enter prompt")

    async def upload_image(self, image_path: str) -> bool:
        """Прикрепляем картинку к composer (для image-edit и image→video)."""
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Image file not found: {image_path}")

        logger.info(f"Uploading image: {image_path}...")

        # 1) кнопка «Add files» / «Add file» рядом с composer (актуальный UI Arena)
        for name in ["Add files", "Add file", "Attach", "Upload"]:
            try:
                btn = self.page.get_by_role("button", name=re.compile(rf"^{name}$", re.I)).first
                if await btn.count() and await btn.is_visible():
                    async with self.page.expect_file_chooser(timeout=8000) as fc_info:
                        await btn.click()
                    chooser = await fc_info.value
                    await chooser.set_files(image_path)
                    logger.info(f"Image uploaded via '{name}' button ✅")
                    await asyncio.sleep(2.5)
                    return True
            except Exception as e:
                logger.debug(f"attach via '{name}' failed: {e}")

        # 2) скрытый input[type=file]
        try:
            file_input = await self.page.query_selector('input[type="file"]')
            if file_input:
                await file_input.set_input_files(image_path, timeout=10000)
                logger.info("Image uploaded via input[type=file] ✅")
                await asyncio.sleep(2.5)
                return True
        except Exception as e:
            logger.debug(f"input[type=file] failed: {e}")

        # 3) делаем input видимым и грузим
        try:
            await self.page.evaluate(
                """() => {
                    document.querySelectorAll('input[type="file"]').forEach(i => {
                        i.style.display = 'block';
                        i.style.visibility = 'visible';
                        i.removeAttribute('hidden');
                    });
                }"""
            )
            fi = await self.page.query_selector('input[type="file"]')
            if fi:
                await fi.set_input_files(image_path, force=True)
                logger.info("Image uploaded via forced file input ✅")
                await asyncio.sleep(2.5)
                return True
        except Exception as e:
            logger.debug(f"forced file input failed: {e}")

        raise Exception("Failed to attach image: file input not found (UI Arena мог измениться)")

    # ═════════════════════════════════════════════════════════
    # Отправка промпта
    # ═════════════════════════════════════════════════════════
    async def _generation_state(self, prompt: str = "") -> dict:
        """Состояние страницы: идёт ли генерация и ушёл ли промпт."""
        try:
            return await self.page.evaluate(
                """(prompt) => {
                    const vis = (e) => {
                        if (!e) return false;
                        const cs = getComputedStyle(e);
                        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) return false;
                        const r = e.getBoundingClientRect();
                        return r.width > 1 && r.height > 1;
                    };
                    const ta = document.querySelector("textarea");
                    const editorEmpty = ta ? (ta.value || '').trim().length === 0 : true;
                    const body = document.body ? (document.body.innerText || '') : '';
                    const head = body.slice(0, 6000);

                    const stopBtn = document.querySelector("button[aria-label='Stop generation']");
                    const stopText = Array.from(document.querySelectorAll('button'))
                        .some(b => vis(b) && /^(stop|stop generating)$/i.test((b.innerText || '').trim()));
                    const generating = /(generating|creating|rendering|processing your|queued|in queue)/i.test(head);

                    const part = (prompt || '').trim().slice(0, 40);
                    const promptInPage = part.length >= 8 ? body.includes(part) : false;
                    const hasDownloadOrEdit = Array.from(document.querySelectorAll('button'))
                        .some(b => vis(b) && /^(edit)$/i.test((b.innerText || '').trim()));

                    return {
                        editorEmpty, generating,
                        stop: !!(stopBtn && vis(stopBtn)) || stopText,
                        promptInPage, hasDownloadOrEdit,
                        url: location.href,
                    };
                }""",
                prompt,
            )
        except Exception as e:
            logger.debug(f"generation state eval failed: {e}")
            return {}

    async def _wait_submission(self, prompt: str, seconds: float = None) -> bool:
        """Ждём подтверждения, что промпт реально ушёл."""
        deadline = time.time() + float(seconds if seconds is not None else self.submit_check_seconds)
        while time.time() < deadline:
            st = await self._generation_state(prompt)
            # 1) идёт генерация: кнопка Stop / индикатор «Generating»
            if st.get("stop") or st.get("generating"):
                return True
            # 2) промпт отрисован в чате как сообщение. Значение <textarea> в
            #    innerText не попадает, поэтому совпадение = сообщение реально ушло.
            if st.get("promptInPage"):
                return True
            # 3) ушли на страницу чата /c/… и composer опустел
            if "/c/" in (st.get("url") or "") and st.get("editorEmpty"):
                return True
            await asyncio.sleep(0.7)
        return False

    async def composer_blocker(self) -> str:
        """
        Что перекрывает поле ввода (если перекрывает).

        Частая причина «промпт не ушёл»: поверх composer лежит модалка/баннер
        (position:fixed портал), клик и Enter доходят до него, а не до поля.
        Возвращает описание верхнего элемента или пустую строку.
        """
        try:
            return await self.page.evaluate(
                """() => {
                    const ta = document.querySelector("textarea");
                    if (!ta) return 'textarea не найден';
                    const r = ta.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) return 'textarea не отрисован (0x0)';
                    const el = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
                    if (!el) return 'elementFromPoint вернул null';
                    if (el === ta || ta.contains(el) || el.contains(ta)) return '';
                    const host = ta.closest('form') || ta.parentElement;
                    if (host && (host.contains(el) || el.contains(host))) return '';
                    const cls = (typeof el.className === 'string' && el.className)
                        ? '.' + el.className.split(/\s+/).slice(0, 3).join('.') : '';
                    const role = el.getAttribute('role');
                    const aria = el.getAttribute('aria-label');
                    const text = ((el.innerText || el.textContent || '') + '').replace(/\s+/g, ' ').trim().slice(0, 140);
                    return el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
                           (role ? '[role=' + role + ']' : '') + (aria ? '[aria-label=' + aria + ']' : '') +
                           cls + (text ? ' :: ' + text : '');
                }"""
            )
        except Exception as e:
            logger.debug(f"composer_blocker failed: {e}")
            return ''

    async def send_state(self) -> dict:
        """Состояние composer: активна ли кнопка отправки, не идёт ли загрузка файла."""
        try:
            state = await self.page.evaluate(
                """() => {
                    const ta = document.querySelector("textarea");
                    if (!ta) return { found: false };
                    const host = ta.closest('form') || ta.closest('div') || document;
                    const send = host.querySelector("button[aria-label='Send message']")
                        || host.querySelector("button[type='submit']")
                        || Array.from(host.querySelectorAll('button')).filter(b => !!b.querySelector('svg')).pop();
                    return {
                        found: true,
                        sendFound: !!send,
                        sendDisabled: send ? (!!send.disabled || send.getAttribute('aria-disabled') === 'true') : null,
                        textareaDisabled: !!ta.disabled || !!ta.readOnly,
                        uploading: !!document.querySelector("[aria-busy='true'], [class*='upload' i][class*='progress' i], [class*='uploading' i]"),
                    };
                }"""
            )
            return state if isinstance(state, dict) else {}
        except Exception as e:
            logger.debug(f"send_state failed: {e}")
            return {}

    async def wait_composer_ready(self, timeout: float = 30) -> bool:
        """
        Ждём, пока composer станет рабочим.

        После прикрепления файла Arena держит кнопку «Send message» выключенной,
        пока вложение не обработается: Enter в этот момент просто ничего не делает.
        """
        deadline = time.time() + timeout
        warned = False
        while time.time() < deadline:
            st = await self.send_state()
            if not st or not st.get("found"):
                return True  # состояние неизвестно — не блокируем
            bad = st.get("textareaDisabled") or st.get("uploading") or (st.get("sendFound") and st.get("sendDisabled"))
            if not bad:
                return True
            if not warned:
                reason = ("идёт загрузка вложения" if st.get("uploading") else
                          "кнопка Send неактивна" if st.get("sendDisabled") else
                          "поле ввода заблокировано (disabled/readonly)")
                logger.warning(f"⏳ Composer не готов: {reason} — ждём...")
                warned = True
            await asyncio.sleep(1)
        logger.warning("⚠️ Composer так и не стал активным за отведённое время")
        return False

    async def _click_send_button(self, mode: str = None) -> bool:
        """Клик по кнопке отправки (только когда composer включён)."""
        selectors = [
            "button[aria-label='Send message']:not([disabled])",
            "form:has(textarea) button[aria-label='Send message']",
            "button[aria-label*='send' i]:not([disabled])",
            "button[type='submit']:not([disabled])",
            "form button:has(svg):not([disabled])",
        ]
        for sel in selectors:
            try:
                btns = await self.page.query_selector_all(sel)
                for b in btns:
                    if await b.is_visible() and await b.is_enabled():
                        await b.click()
                        logger.info(f"Clicked send button via: {sel} ✅")
                        return True
            except Exception:
                continue
        # JS-резерв: последняя кнопка с svg внутри формы composer
        try:
            clicked = await self.page.evaluate(
                """() => {
                    const ta = document.querySelector("textarea");
                    if (!ta) return false;
                    const host = ta.closest('form') || ta.closest('div') || document;
                    const btns = Array.from(host.querySelectorAll('button')).filter(b => {
                        if (b.disabled) return false;
                        const cs = getComputedStyle(b);
                        const r = b.getBoundingClientRect();
                        return cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 1 && r.height > 1;
                    });
                    const blocked = /(add file|upload|got it|edit|video|image|attach)/i;
                    const cand = btns.filter(b => {
                        const t = ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '') + ' ' +
                                   (b.getAttribute('title') || '')).trim();
                        return !blocked.test(t) && !!b.querySelector('svg');
                    });
                    if (!cand.length) return false;
                    cand[cand.length - 1].click();
                    return true;
                }"""
            )
            if clicked:
                logger.info("Clicked send button via JS ✅")
                return True
        except Exception:
            pass
        return False

    async def submit_prompt(self, prompt: str = "", mode: str = None) -> bool:
        """
        Отправка промпта. Порядок попыток:
          1) Enter (работает всегда; клик по кнопке может тронуть экран безопасности)
          2) клик по кнопке «Send message»
          3) закрыть согласие/баннер и повторить
        Возвращает True только если генерация действительно началась.
        """
        logger.info("Submitting prompt...")

        await self.ensure_consent()

        # Проверяем, не перекрыт ли composer модалкой/баннером
        blocker = await self.composer_blocker()
        if blocker:
            logger.warning(f"⚠️ Поле ввода перекрыто: {blocker}")
            try:
                await self.page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
            except Exception:
                pass
            await self.ensure_consent(reload_after=True)
            blocker2 = await self.composer_blocker()
            if blocker2:
                logger.warning(f"⚠️ Перекрытие осталось: {blocker2}")

        # Ждём, пока кнопка отправки станет активной (после загрузки фото)
        await self.wait_composer_ready(timeout=max(20.0, self.submit_check_seconds))

        if await self._wait_submission(prompt, seconds=3):
            logger.info("Generation already running ✅")
            return True

        enter_retry_done = False
        for attempt in range(1, 4):
            # (1) Enter
            try:
                editor = await self._find_editor(mode)
                if editor:
                    await editor.focus()
                    await asyncio.sleep(0.2)
                    await self.page.keyboard.press("Enter")
                    if await self._wait_submission(prompt, seconds=self.submit_check_seconds):
                        logger.info(f"Generation started via Enter ✅ (попытка {attempt})")
                        return True
            except Exception as e:
                logger.debug(f"Enter submit failed: {e}")

            # текст мог остаться в поле — Enter ушёл в перенос строки
            try:
                await self.enter_prompt(prompt, mode) if prompt else None
            except Exception:
                pass

            # (2) кнопка отправки
            if await self._click_send_button(mode):
                if await self._wait_submission(prompt, seconds=self.submit_check_seconds):
                    logger.info(f"Generation started via button ✅ (попытка {attempt})")
                    return True

            # (3) закрываем согласие (оно «съедает» Enter) и повторяем
            if await self.ensure_consent():
                try:
                    await self.enter_prompt(prompt, mode) if prompt else None
                except Exception:
                    pass
                try:
                    await self.page.keyboard.press("Enter")
                except Exception:
                    pass
                if await self._wait_submission(prompt, seconds=self.submit_check_seconds):
                    logger.info(f"Generation started after consent retry ✅ (попытка {attempt})")
                    return True
            enter_retry_done = True
            await asyncio.sleep(1.5)

        st = await self._generation_state(prompt)
        blocker = await self.composer_blocker()
        send = await self.send_state()
        logger.warning(f"Промпт не отправлен. Состояние: {st} | перекрытие: {blocker or 'нет'} | composer: {send}")
        reason = []
        if blocker:
            reason.append(f"поле ввода перекрыто «{blocker}»")
        if send.get("sendDisabled"):
            reason.append("кнопка Send неактивна")
        if send.get("uploading"):
            reason.append("не завершилась загрузка вложения")
        if st.get("editorEmpty"):
            reason.append("поле ввода пустое (текст не записался)")
        raise Exception(
            "Не удалось отправить промпт на arena.ai. Причины: "
            + ("; ".join(reason) if reason else "модалка согласия или протухшая сессия Arena")
            + ". Проверьте cookies/сессию Arena и запустите diagnose_arena.py."
        )

    # ═════════════════════════════════════════════════════════
    # Сохранение результата
    # ═════════════════════════════════════════════════════════
    async def _fetch_bytes(self, url: str, timeout_ms: int = 120000) -> tuple:
        """Скачиваем через контекст браузера (наследует cookies). Вернёт (bytes, ext)."""
        for client_name, client in (("context", self.context.request), ("page", self.page.request)):
            if client is None:
                continue
            try:
                resp = await client.get(url, timeout=timeout_ms, headers={"Referer": "https://arena.ai/"})
                if resp.ok:
                    body = await resp.body()
                    if body:
                        ext = _ext_from_content_type(resp.headers.get("content-type", ""), _ext_from_url(url))
                        logger.info(f"Downloaded {len(body) // 1024} KB via {client_name}.request ({ext}) ✅")
                        return body, ext
                else:
                    logger.debug(f"{client_name}.request HTTP {resp.status} for {url[:120]}")
            except Exception as e:
                logger.debug(f"{client_name}.request failed: {e}")

        # резерв: fetch внутри страницы (умеет blob: и подписанные ссылки)
        try:
            data = await self.page.evaluate(
                """async (u) => {
                    try {
                        const r = await fetch(u, {credentials: 'include'});
                        if (!r.ok) return null;
                        const b = await r.arrayBuffer();
                        const bytes = new Uint8Array(b);
                        let bin = '';
                        const chunk = 0x8000;
                        for (let i = 0; i < bytes.length; i += chunk) {
                            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                        }
                        return {b64: btoa(bin), type: r.headers.get('content-type') || ''};
                    } catch (e) { return null; }
                }""",
                url,
            )
            if data and data.get("b64"):
                body = base64.b64decode(data["b64"])
                ext = _ext_from_content_type(data.get("type", ""), _ext_from_url(url))
                logger.info(f"Downloaded {len(body) // 1024} KB via page fetch ({ext}) ✅")
                return body, ext
        except Exception as e:
            logger.debug(f"page fetch failed: {e}")
        return None, None

    async def _save_media_url(self, url: str, output_dir: str, prefix: str, index: int = 0,
                              fallback_ext: str = ".png", min_bytes: int = 2000):
        """Скачиваем медиа по URL и сохраняем в output_dir."""
        if not url or url.startswith("data:"):
            return None
        body, ext = await self._fetch_bytes(url)
        if not body:
            return None
        # отсекаем «почти пустые» ответы (заглушки, иконки, страницы ошибок)
        if len(body) < min_bytes:
            logger.debug(f"Слишком маленький ответ ({len(body)} байт) — пропускаем {url[:100]}")
            return None
        url_ext = _ext_from_url(url, "")
        if fallback_ext in (".mp4", ".webm") and url_ext in VIDEO_EXTS:
            ext = url_ext  # это видео — ссылка надёжнее content-type
        elif not ext or ext in (".bin",):
            ext = url_ext or fallback_ext
        os.makedirs(output_dir, exist_ok=True)
        suffix = f"_{index}" if index else ""
        filepath = os.path.join(output_dir, f"{prefix}_{int(time.time())}{suffix}{ext}")
        with open(filepath, "wb") as f:
            f.write(body)
        logger.info(f"✅ Saved {filepath} ({len(body) // 1024} KB)")
        return filepath

    async def _result_images_dom(self) -> dict:
        """Картинки последнего ответа ассистента + признаки завершения."""
        try:
            return await self.page.evaluate(
                """() => {
                    const vis = (e) => {
                        if (!e) return false;
                        const cs = getComputedStyle(e);
                        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) return false;
                        const r = e.getBoundingClientRect();
                        return r.width > 20 && r.height > 1;
                    };
                    const isJunk = (src) => /(favicon|logo|avatar|sprite|icon|emoji|placeholder|blur|spinner)/i.test(src || '');
                    const out = [];

                    const push = (img, scopeName) => {
                        const w = img.naturalWidth || img.width || 0;
                        const h = img.naturalHeight || img.height || 0;
                        const src = img.currentSrc || img.src || '';
                        if (w >= 200 && h >= 200 && src && !isJunk(src)) out.push({ src, w, h, scope: scopeName });
                    };

                    let scope = null;
                    const ol = document.querySelector('ol.flex-col-reverse') || document.querySelector('ol');
                    if (ol) {
                        const kids = Array.from(ol.children).filter(vis);
                        for (let i = kids.length - 1; i >= 0; i--) {
                            const c = kids[i];
                            const isUser = c.classList.contains('justify-end') || !!c.querySelector('[data-role="user"]');
                            if (!isUser) { scope = c; break; }
                        }
                    }
                    if (scope) scope.querySelectorAll('img').forEach(i => push(i, 'assistant'));
                    if (!out.length) {
                        document.querySelectorAll('main img, figure img, [data-testid*="result" i] img').forEach(i => push(i, 'main'));
                    }

                    const stop = !!document.querySelector("button[aria-label='Stop generation']");
                    const spinning = !!document.querySelector('.animate-spin, [class*="animate-spin"], .lucide-loader');
                    const body = document.body ? (document.body.innerText || '').slice(0, 6000) : '';
                    const generating = /(generating|creating|rendering)/i.test(body);
                    const actionBar = !!(scope && (
                        scope.querySelector('button[aria-label*="like" i], button[aria-label*="dislike" i]') ||
                        Array.from(scope.querySelectorAll('button')).some(b => (b.innerText || '').trim() === 'Edit')
                    ));
                    const errorText = (() => {
                        const m = body.match(/(blocked|moderation|violat|failed|error|couldn't|could not|unable to)/i);
                        return m ? body.slice(Math.max(0, m.index - 80), m.index + 160) : '';
                    })();

                    return { images: out, stop, spinning, generating, actionBar, errorText,
                             assistantFound: !!scope, url: location.href };
                }"""
            )
        except Exception as e:
            logger.debug(f"result dom eval failed: {e}")
            return {"images": [], "stop": False, "spinning": False, "generating": False, "actionBar": False}

    async def _click_download_candidates(self) -> int:
        """Кликаем по всем кнопкам скачивания результата (в т.ч. иконка рядом с Edit)."""
        try:
            count = await self.page.evaluate(
                """() => {
                    const vis = (e) => {
                        if (!e) return false;
                        const cs = getComputedStyle(e);
                        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) return false;
                        const r = e.getBoundingClientRect();
                        return r.width > 1 && r.height > 1;
                    };
                    const danger = /(delete|remove|trash|share|report|publish|copy|regenerate|retry|hide|discard|like|dislike|edit|zoom|fullscreen|download app)/i;
                    const seen = new Set();
                    const targets = [];
                    const add = (el) => {
                        if (!el || seen.has(el) || el.disabled || !vis(el)) return;
                        seen.add(el);
                        targets.push(el);
                    };

                    // (a) явные кнопки/ссылки скачивания
                    Array.from(document.querySelectorAll('button, a')).forEach(el => {
                        const t = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' +
                                   (el.getAttribute('title') || '') + ' ' + (el.getAttribute('data-testid') || '')).toLowerCase();
                        if (t.includes('download') && !danger.test(t)) add(el);
                    });

                    // (b) кнопка-иконка слева от кнопки «Edit» — так устроена карточка результата Arena
                    Array.from(document.querySelectorAll('button')).forEach(el => {
                        if ((el.innerText || '').trim() !== 'Edit') return;
                        const prev = el.previousElementSibling;
                        if (prev && prev.tagName === 'BUTTON') {
                            const t = ((prev.getAttribute('aria-label') || '') + ' ' + (prev.getAttribute('title') || '')).toLowerCase();
                            if (!danger.test(t)) add(prev);
                        }
                        const parent = el.parentElement;
                        if (parent) {
                            const btns = Array.from(parent.querySelectorAll('button'));
                            const idx = btns.indexOf(el);
                            if (idx > 0) {
                                const cand = btns[idx - 1];
                                const t = ((cand.getAttribute('aria-label') || '') + ' ' + (cand.getAttribute('title') || '')).toLowerCase();
                                if (!danger.test(t)) add(cand);
                            }
                        }
                    });

                    targets.forEach(el => { try { el.click(); } catch (e) { } });
                    return targets.length;
                }"""
            )
            if count:
                logger.info(f"Нажато кнопок скачивания: {count}")
            return int(count or 0)
        except Exception as e:
            logger.debug(f"click download candidates failed: {e}")
            return 0

    async def _collect_downloads(self, expected: int, wait_seconds: int) -> list:
        """Ждём download-события после клика по кнопкам скачивания."""
        deadline = time.time() + wait_seconds
        while time.time() < deadline and len(self._downloads) < expected:
            await asyncio.sleep(0.5)
        return list(self._downloads)

    # ═════════════════════════════════════════════════════════
    # Ожидание результата: картинки
    # ═════════════════════════════════════════════════════════
    async def wait_for_image(self, timeout: int = None, output_dir: str = "generated") -> str:
        """Ждём картинку и сохраняем её. Возвращает путь к файлу."""
        timeout = int(timeout or IMAGE_TIMEOUT)
        logger.info(f"Waiting for image generation (до {timeout // 60} мин)...")
        start = time.time()
        os.makedirs(output_dir, exist_ok=True)
        last_consent_check = 0.0
        last_download_click = 0.0
        failed_urls = set()

        while (time.time() - start) < timeout:
            await self.handle_captcha(wait_seconds=0)
            if time.time() - last_consent_check > self.consent_check_interval:
                await self.dismiss_got_it()
                await self.ensure_consent()
                last_consent_check = time.time()

            state = await self._result_images_dom()
            images = [i for i in state.get("images", []) if i.get("src") not in self._baseline_images]

            # 1) кнопка Download на карточке результата (отдаёт полноразмерный файл)
            if images and (time.time() - last_download_click) > self.download_click_interval:
                last_download_click = time.time()
                clicked = await self._click_download_candidates()
                if clicked:
                    files = await self._collect_downloads(expected=1, wait_seconds=self.download_wait_seconds)
                    files = [f for f in files if os.path.exists(f) and os.path.getsize(f) >= 2000]
                    if files:
                        logger.info("✅ Изображение получено кнопкой Download")
                        return files[0]

            # 2) скачиваем найденный URL (сеть/SSE/страница)
            candidates = [i["src"] for i in images if i["src"] not in failed_urls]
            for url in candidates + [u for u in self._stream_urls if u not in failed_urls]:
                saved = await self._save_media_url(url, output_dir, "image", fallback_ext=".png")
                if saved:
                    return saved
                failed_urls.add(url)

            # 3) сетевые кандидаты (перехваченные ответы)
            for item in list(self._captured["image"]):
                if item["url"] in failed_urls:
                    continue
                saved = await self._save_media_url(item["url"], output_dir, "image", fallback_ext=".png")
                if saved:
                    return saved
                failed_urls.add(item["url"])

            # 4) скриншот элемента (последний резерв)
            if images:
                try:
                    el = await self.page.query_selector('img[src="%s"]' % images[0]["src"].replace('"', '\\"'))
                    if el:
                        filepath = os.path.join(output_dir, f"image_{int(time.time())}.png")
                        await el.screenshot(path=filepath)
                        logger.info(f"✅ Изображение сохранено скриншотом элемента: {filepath}")
                        return filepath
                except Exception as e:
                    logger.debug(f"element screenshot failed: {e}")

            # генерация закончилась, но результата нет → ждём немного и падаем
            finished = (not state.get("stop")) and (not state.get("spinning")) and (not state.get("generating"))
            if finished and state.get("actionBar"):
                await asyncio.sleep(3)
                if not images and not self._captured["image"]:
                    err = state.get("errorText") or ""
                    raise Exception(f"Arena вернула пустой результат генерации изображения. {err}".strip())

            await asyncio.sleep(self.image_poll_seconds)

        err = f"; ошибка Arena: {self._stream_errors[0]}" if self._stream_errors else ""
        raise Exception(f"Image generation timed out or result not found{err}")

    # ═════════════════════════════════════════════════════════
    # Ожидание результата: видео
    # ═════════════════════════════════════════════════════════
    async def _video_state(self) -> dict:
        try:
            return await self.page.evaluate(
                """() => {
                    const vis = (e) => {
                        if (!e) return false;
                        const cs = getComputedStyle(e);
                        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) return false;
                        const r = e.getBoundingClientRect();
                        return r.width > 60 && r.height > 1;
                    };
                    const vids = Array.from(document.querySelectorAll('video')).filter(vis)
                        .map(v => v.currentSrc || v.src || (v.querySelector('source') ? v.querySelector('source').src : ''))
                        .filter(Boolean);
                    const dlButtons = Array.from(document.querySelectorAll('button, a')).filter(el => {
                        if (!vis(el) || el.disabled) return false;
                        const t = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' +
                                   (el.getAttribute('title') || '')).toLowerCase();
                        return t.includes('download');
                    }).length;
                    let editSiblings = 0;
                    Array.from(document.querySelectorAll('button')).forEach(el => {
                        if ((el.innerText || '').trim() !== 'Edit') return;
                        const prev = el.previousElementSibling;
                        if (prev && prev.tagName === 'BUTTON' && !prev.disabled) editSiblings++;
                    });
                    const stop = !!document.querySelector("button[aria-label='Stop generation']");
                    const spinning = !!document.querySelector('.animate-spin, [class*="animate-spin"]');
                    const body = document.body ? (document.body.innerText || '').slice(0, 8000) : '';
                    const generating = /(generating|creating|rendering|queued)/i.test(body);
                    const errorText = (() => {
                        const m = body.match(/(blocked|moderation|violat|failed|error|couldn't|could not|unable to|not available)/i);
                        return m ? body.slice(Math.max(0, m.index - 80), m.index + 200) : '';
                    })();
                    const done = !!body.match(/(download|share|edit)/i);
                    return { videos: vids, dlButtons, editSiblings, stop, spinning, generating, errorText, done,
                             url: location.href };
                }"""
            )
        except Exception as e:
            logger.debug(f"video state eval failed: {e}")
            return {"videos": [], "dlButtons": 0, "editSiblings": 0}

    async def wait_for_video(self, timeout: int = None, output_dir: str = "generated",
                             expected: int = None) -> list:
        """Ждём видео (по умолчанию 2 варианта) и сохраняем файлы."""
        timeout = int(timeout or VIDEO_TIMEOUT)
        expected = expected or VIDEO_VARIANTS
        logger.info(f"Waiting for video generation ({expected} варианта, до {timeout // 60} мин)...")
        start = time.time()
        os.makedirs(output_dir, exist_ok=True)

        saved: list = []
        last_consent_check = 0.0
        clicked_at = 0.0
        failed_urls = set()

        while (time.time() - start) < timeout:
            await self.handle_captcha(wait_seconds=0)
            if time.time() - last_consent_check > self.consent_check_interval:
                await self.dismiss_got_it()
                await self.ensure_consent()
                last_consent_check = time.time()

            # забрали готовые download-события
            for path in self._downloads:
                if path not in saved and os.path.exists(path):
                    saved.append(path)
            if len(saved) >= expected:
                break

            state = await self._video_state()

            # кликаем по кнопкам скачивания (не чаще заданного интервала)
            ready = state.get("dlButtons", 0) or state.get("editSiblings", 0)
            if ready and (time.time() - clicked_at) > self.download_click_interval:
                clicked_at = time.time()
                clicked = await self._click_download_candidates()
                if clicked:
                    await self._collect_downloads(expected=expected, wait_seconds=self.download_wait_seconds)
                    for path in self._downloads:
                        if path not in saved and os.path.exists(path):
                            saved.append(path)
                    if len(saved) >= expected:
                        break

            # прямые ссылки на mp4 из сети/SSE
            urls = []
            urls += list(self._stream_urls)
            urls += [v for v in state.get("videos", []) if v and not v.startswith("blob:")]
            urls += [v["url"] for v in self._captured["video"]]
            for url in urls:
                if len(saved) >= expected:
                    break
                if url in failed_urls or any(url in s for s in saved):
                    continue
                path = await self._save_media_url(url, output_dir, "video", index=len(saved),
                                                   fallback_ext=".mp4", min_bytes=20000)
                if path:
                    saved.append(path)
                else:
                    failed_urls.add(url)

            # blob:-видео — тянем через fetch внутри страницы
            if len(saved) < expected:
                for v in state.get("videos", []):
                    if not v.startswith(("blob:", "data:")):
                        continue
                    if len(saved) >= expected:
                        break
                    if v in failed_urls:
                        continue
                    path = await self._save_media_url(v, output_dir, "video", index=len(saved),
                                                       fallback_ext=".mp4", min_bytes=20000)
                    if path:
                        saved.append(path)
                    else:
                        failed_urls.add(v)

            if len(saved) >= expected:
                break

            finished = (not state.get("stop")) and (not state.get("spinning")) and not state.get("generating")
            if finished and state.get("errorText") and not saved:
                raise Exception(f"Arena вернула ошибку при генерации видео: {state['errorText'][:300]}")

            await asyncio.sleep(self.video_poll_seconds)

        if not saved:
            err = f"; ошибка Arena: {self._stream_errors[0]}" if self._stream_errors else ""
            raise Exception(f"Video generation timed out or result not found{err}")
        if len(saved) < expected:
            logger.warning(f"Получено {len(saved)} из {expected} видео-вариантов")
        return saved

    # ═════════════════════════════════════════════════════════
    # ПУБЛИЧНЫЕ МЕТОДЫ ГЕНЕРАЦИИ
    # ═════════════════════════════════════════════════════════
    async def generate_landing(self, prompt: str, output_dir: str = "generated") -> str:
        """Генерация лендинга (code mode). Возвращает путь к файлу."""
        return await self._generate_code(prompt, output_dir)

    async def generate_code(self, prompt: str, output_dir: str = "generated") -> str:
        """Генерация кода (code mode). Возвращает путь к файлу."""
        return await self._generate_code(prompt, output_dir)

    async def _generate_code(self, prompt: str, output_dir: str = "generated") -> str:
        try:
            if not self.browser:
                await self.init_browser()

            url = ARENA_URLS["code"]
            await self._navigate_and_wait(url, mode="code")
            self.start_capture()
            await self.page.screenshot(path="debug_01_loaded.png")

            await self.enter_prompt(prompt, mode="code")
            await self.page.screenshot(path="debug_02_prompt.png")

            await self.submit_prompt(prompt, mode="code")
            await self.page.screenshot(path="debug_03_submitted.png")

            timeout = min(max(int(ARENA_TIMEOUT), 1800), 2400)
            download_button = await self.wait_for_download_button(timeout)

            if not download_button:
                await self.page.screenshot(path="debug_04_no_download.png")
                raise Exception("Download button not found/not active after timeout")

            await self.page.screenshot(path="debug_05_download_ready.png")
            os.makedirs(output_dir, exist_ok=True)

            try:
                async with self.page.expect_download(timeout=120000) as download_info:
                    await download_button.click()
                download = await download_info.value
                ts = int(time.time())
                filename = f"landing_{ts}.zip"
                filepath = os.path.join(output_dir, filename)
                await download.save_as(filepath)
                logger.info(f"✅ Landing saved to {filepath}")
                return filepath

            except Exception as e:
                logger.warning(f"Download event failed, extracting code from DOM: {e}")
                code_blocks = await self.page.query_selector_all('pre code, .cm-content, pre')
                full_code = ""
                for block in code_blocks:
                    text = await block.text_content()
                    full_code += (text or "") + "\n"

                if full_code.strip():
                    ts = int(time.time())
                    filepath = os.path.join(output_dir, f"landing_{ts}.html")
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(full_code)
                    logger.info(f"✅ Code extracted from DOM and saved to {filepath}")
                    return filepath

                await self.page.screenshot(path="debug_06_extract_failed.png")
                raise Exception("Could not download or extract the landing code.")

        except Exception as e:
            logger.error(f"Error generating code: {e}")
            try:
                await self.page.screenshot(path="debug_error.png")
            except Exception:
                pass
            raise
        finally:
            self.stop_capture()

    # ─────────────────────────────────────────────────────────
    # Ожидание кнопки Download (code/landing)
    # ─────────────────────────────────────────────────────────
    async def wait_for_download_button(self, timeout: int):
        logger.info(f"Waiting for active Download button (up to {timeout // 60} min)...")
        start = asyncio.get_event_loop().time()
        last_log = 0
        last_consent_check = 0.0

        while (asyncio.get_event_loop().time() - start) < timeout:
            await self.handle_captcha(wait_seconds=0)
            if time.time() - last_consent_check > self.consent_check_interval:
                await self.dismiss_got_it()
                await self.ensure_consent()
                last_consent_check = time.time()
            try:
                buttons = await self.page.query_selector_all(
                    'button:has-text("Download"), a:has-text("Download"), '
                    'a[download], button[aria-label*="download" i]'
                )
                for b in buttons:
                    if await b.is_visible() and await b.is_enabled():
                        logger.info("Download button is ACTIVE! ✅")
                        return b
            except Exception:
                pass

            elapsed = int(asyncio.get_event_loop().time() - start)
            if elapsed - last_log >= 60:
                logger.info(f"Still generating... {elapsed // 60} min elapsed")
                last_log = elapsed
            await asyncio.sleep(10)

        return None

    async def wait_for_text_done(self, timeout: int = None) -> str:
        """Ждём окончания генерации текста и забираем ответ ассистента."""
        timeout = int(timeout or TEXT_TIMEOUT)
        logger.info("Waiting for text generation to finish...")
        start = asyncio.get_event_loop().time()

        UI_JUNK_PATTERNS = [
            "Follow us for the latest",
            "Experience the frontier",
            "Battle 2 anonymous models",
            "Built for complex tasks",
            "Compare 2 models of your choice",
            "Chat with 1 model at a time",
            "Side by Side",
            "DirectChat",
        ]

        await asyncio.sleep(2)
        last_consent_check = 0.0
        while (asyncio.get_event_loop().time() - start) < timeout:
            await self.handle_captcha(wait_seconds=0)
            if time.time() - last_consent_check > 20:
                await self.dismiss_got_it()
                last_consent_check = time.time()
            try:
                st = await self._generation_state()
                if not st.get("stop") and not st.get("generating"):
                    break
            except Exception:
                break
            await asyncio.sleep(3)

        await asyncio.sleep(2)

        result = ""
        selectors = [
            '[data-message-author-role="assistant"] .prose',
            '[data-message-author-role="assistant"]',
            'div[class*="assistant"] div[class*="prose"]',
            '.prose',
            '.markdown',
            'article',
        ]

        for sel in selectors:
            try:
                els = await self.page.query_selector_all(sel)
                for el in els:
                    if not await el.is_visible():
                        continue
                    t = (await el.text_content() or "").strip()
                    is_junk = any(junk in t for junk in UI_JUNK_PATTERNS)
                    if t and not is_junk and len(t) > len(result):
                        result = t
            except Exception:
                continue

        if not result:
            try:
                blocks = await self.page.evaluate('''() => {
                    const nodes = Array.from(document.querySelectorAll('div, section, p'));
                    return nodes.map(m => m.innerText ? m.innerText.trim() : '').filter(t => t.length > 50);
                }''')
                for b in blocks:
                    if not any(junk in b for junk in UI_JUNK_PATTERNS):
                        if len(b) > len(result):
                            result = b
            except Exception:
                pass

        if result:
            for junk in UI_JUNK_PATTERNS:
                if junk in result:
                    lines = [line for line in result.splitlines() if not any(j in line for j in UI_JUNK_PATTERNS)]
                    result = "\n".join(lines).strip()

        logger.info(f"Text generation done, got {len(result)} chars")
        return result

    async def generate_text(self, prompt: str, timeout: int = None) -> str:
        """Генерация текста (text mode). Возвращает строку с ответом."""
        timeout = int(timeout or TEXT_TIMEOUT)
        try:
            if not self.browser:
                await self.init_browser()

            url = ARENA_URLS["text"]
            await self._navigate_and_wait(url, mode="text")
            self.start_capture()

            await self.enter_prompt(prompt, mode="text")
            await self.submit_prompt(prompt, mode="text")

            result = await self.wait_for_text_done(timeout=timeout)
            if not result:
                raise Exception("Empty response from text generation")
            return result

        except Exception as e:
            logger.error(f"Error generating text: {e}")
            try:
                await self.page.screenshot(path="debug_text_error.png")
            except Exception:
                pass
            raise
        finally:
            self.stop_capture()

    async def generate_prompt(self, topic: str, timeout: int = None) -> str:
        """Генерация промта по теме."""
        meta_prompt = (
            f"Write a detailed, creative and professional AI prompt for the following topic: {topic}. "
            f"Output only the prompt text, nothing else."
        )
        return await self.generate_text(meta_prompt, timeout=timeout or TEXT_TIMEOUT)

    async def generate_image(self, prompt: str, output_dir: str = "generated", timeout: int = None) -> str:
        """Генерация картинки (image mode). Возвращает путь к файлу."""
        timeout = int(timeout or IMAGE_TIMEOUT)
        try:
            if not self.browser:
                await self.init_browser()

            url = ARENA_URLS["image"]
            await self._navigate_and_wait(url, mode="image")
            self.start_capture(os.path.join(output_dir, ".downloads"))

            await self.enter_prompt(prompt, mode="image")
            await self.submit_prompt(prompt, mode="image")

            filepath = await self.wait_for_image(timeout=timeout, output_dir=output_dir)
            return filepath

        except Exception as e:
            logger.error(f"Error generating image: {e}")
            try:
                await self.page.screenshot(path="debug_image_error.png")
            except Exception:
                pass
            raise
        finally:
            self.stop_capture()

    async def edit_image(self, image_path: str, prompt: str, output_dir: str = "generated",
                         timeout: int = None) -> str:
        """Редактирование картинки (image edit): загрузка фото + инструкция."""
        timeout = int(timeout or IMAGE_TIMEOUT)
        try:
            if not self.browser:
                await self.init_browser()

            url = ARENA_URLS["image"]
            await self._navigate_and_wait(url, mode="image")
            self.start_capture(os.path.join(output_dir, ".downloads"))

            # 1) прикрепляем картинку
            await self.upload_image(image_path)

            # 2) инструкция по редактированию
            await self.enter_prompt(prompt, mode="image")

            # 3) отправляем
            await self.submit_prompt(prompt, mode="image")

            # 4) ждём результат
            filepath = await self.wait_for_image(timeout=timeout, output_dir=output_dir)
            return filepath

        except Exception as e:
            logger.error(f"Error editing image: {e}")
            try:
                await self.page.screenshot(path="debug_image_edit_error.png")
            except Exception:
                pass
            raise
        finally:
            self.stop_capture()

    async def generate_video(self, prompt: str, output_dir: str = "generated", timeout: int = None) -> list:
        """Генерация видео (video mode — 2 варианта). Возвращает список файлов."""
        timeout = int(timeout or VIDEO_TIMEOUT)
        try:
            if not self.browser:
                await self.init_browser()

            url = ARENA_URLS["video"]
            await self._navigate_and_wait(url, mode="video")
            self.start_capture(os.path.join(output_dir, ".downloads"))

            await self.enter_prompt(prompt, mode="video")
            await self.submit_prompt(prompt, mode="video")

            paths = await self.wait_for_video(timeout=timeout, output_dir=output_dir)
            return paths

        except Exception as e:
            logger.error(f"Error generating video: {e}")
            try:
                await self.page.screenshot(path="debug_video_error.png")
            except Exception:
                pass
            raise
        finally:
            self.stop_capture()

    async def animate_image(self, image_path: str, prompt: str = "", output_dir: str = "generated",
                            timeout: int = None) -> list:
        """Оживление картинки (image → video): загружаем фото и генерируем 2 видео."""
        timeout = int(timeout or VIDEO_TIMEOUT)
        try:
            if not self.browser:
                await self.init_browser()

            url = ARENA_URLS["video"]
            await self._navigate_and_wait(url, mode="video")
            self.start_capture(os.path.join(output_dir, ".downloads"))

            animation_prompt = (prompt or "").strip() or "Animate this image, natural realistic motion, high quality"

            # 1) промпт, 2) вложение (порядок как в рабочей автоматизации Arena)
            await self.enter_prompt(animation_prompt, mode="video")
            await self.upload_image(image_path)

            # 3) отправляем
            await self.submit_prompt(animation_prompt, mode="video")

            # 4) ждём 2 видео
            paths = await self.wait_for_video(timeout=timeout, output_dir=output_dir)
            return paths

        except Exception as e:
            logger.error(f"Error animating image: {e}")
            try:
                await self.page.screenshot(path="debug_animate_error.png")
            except Exception:
                pass
            raise
        finally:
            self.stop_capture()

    # ═════════════════════════════════════════════════════════
    # Cookies
    # ═════════════════════════════════════════════════════════
    async def save_cookies(self):
        if self.context:
            try:
                cookies = await self.context.cookies()
                dir_name = os.path.dirname(COOKIES_PATH)
                if dir_name:
                    os.makedirs(dir_name, exist_ok=True)
                with open(COOKIES_PATH, 'w', encoding='utf-8') as f:
                    json.dump(cookies, f, ensure_ascii=False, indent=2)
                logger.debug("Cookies saved")
            except Exception as e:
                logger.debug(f"save_cookies failed: {e}")
