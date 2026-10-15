"""
Тесты для arena_generator.py.

Реальный arena.ai из песочницы недоступен, поэтому проверяем:
  1) «чистую» логику (разбор SSE, классификация медиа, расширения);
  2) управляющий поток на фейковом Playwright-API, который воспроизводит
     боевые проблемы Arena:
        • модалка Terms of Use держит composer и «съедает» Enter;
        • кнопка Send нажимается, но генерация не стартует;
        • в карточке результата нет текстовой кнопки «Download» (только иконка);
        • есть, куда скачивать: сетевой URL вместо download-события.

Запуск:  pytest -q tests/
"""
import asyncio
import base64
import os
import sys

import pathlib
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arena_generator as ag  # noqa: E402

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


# ═══════════════════════════════════════════════════════════════════
# Фейковый Playwright
# ═══════════════════════════════════════════════════════════════════
class FakeResponse:
    def __init__(self, url, body: bytes = PNG_BYTES, ct: str = "image/png", status: int = 200):
        self.url = url
        self._body = body
        self._ct = ct
        self.status = status

    def ok(self):
        return 200 <= self.status < 300

    async def body(self):
        return self._body

    @property
    def headers(self):
        return {"content-type": self._ct}


class FakeNetRequest:
    """Событие запроса для page.on('request')."""

    def __init__(self, method, url):
        self.method = method
        self.url = url


class FakeRequest:
    def __init__(self, page):
        self.page = page
        self.calls = []

    async def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        if self.page.network_result_available:
            is_video = url.lower().endswith((".mp4", ".webm"))
            # объём правдоподобный: видео должно проходить фильтр размера
            body = (b"\x00" * 300_000) if is_video else (PNG_BYTES * 100)
            return FakeResponse(url, body, "video/mp4" if is_video else "image/png")
        return FakeResponse(url, b"", "text/html", status=404)


class FakeDownload:
    def __init__(self, name="result.png"):
        self.suggested_filename = name
        self.saved_to = None

    async def save_as(self, path):
        self.saved_to = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(PNG_BYTES)


class FakeElement:
    def __init__(self, page, sel="", text="", visible=True, enabled=True):
        self.page = page
        self.sel = sel
        self.text = text
        self.visible = visible
        self.enabled = enabled

    async def is_visible(self):
        return self.visible

    async def is_enabled(self):
        return self.enabled

    async def click(self, timeout=None, force=False, delay=None):
        await self.page._click(self.sel, self.text)

    async def fill(self, text, timeout=None):
        self.page.prompt_value = text

    async def input_value(self):
        return self.page.prompt_value

    async def text_content(self):
        return self.text

    async def focus(self):
        self.page.focused = True

    async def screenshot(self, path=None, **kw):
        self.page.write_png(path)

    async def wait_for(self, state=None, timeout=None):
        return None

    async def get_attribute(self, name):
        return None

    async def bounding_box(self):
        return {"x": 10, "y": 10, "width": 20, "height": 20}

    async def evaluate(self, js, arg=None):
        return await self.page.evaluate(js, arg)

    async def query_selector(self, sel):
        return None

    async def query_selector_all(self, sel):
        return []

    async def count(self):
        return 1


class FakeLocator:
    def __init__(self, page, role, name):
        self.page = page
        self.role = role
        self.name = name
        self.first = FakeElement(page, f"role={role}")

    async def count(self):
        # Кнопка «Add files» есть всегда (нам нужен только один путь вложения)
        return 1 if self.role == "button" else 0

    async def is_visible(self):
        return True

    async def click(self, **kw):
        self.page.file_chooser_opened = True


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        await self.page._press(key)

    async def type(self, text, delay=None):
        self.page.prompt_value += text

    async def insert_text(self, text):
        self.page.prompt_value += text

    async def down(self, key):
        pass

    async def up(self, key):
        pass


class FakeMouse:
    def __init__(self, page):
        self.page = page

    async def move(self, x, y, steps=None):
        pass

    async def click(self, x=None, y=None, delay=None):
        pass


class _ExpectDownload:
    def __init__(self, page, download):
        self.page = page
        self.download = download

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    async def value(self):  # pragma: no cover - используется как await info.value
        return self.download


class _FileChooser:
    def __init__(self, page):
        self.page = page

    async def set_files(self, path):
        self.page.uploaded_file = path


class _ExpectFileChooser:
    def __init__(self, page):
        self.page = page

    async def __aenter__(self):
        self.page.file_chooser_opened = True
        return self

    async def __aexit__(self, *a):
        return False

    @property
    async def value(self):
        return _FileChooser(self.page)


class FakePage:
    """Мини-эмуляция arena.ai с боевыми «подлянками»."""

    def __init__(self, gate_open=False, modal_on_submit=False,
                 download_button=True, result_ready=True, existing_images=None):
        self._url = "https://arena.ai/image/direct"
        self.gate_open = gate_open
        self.modal_on_submit = modal_on_submit
        self.download_button = download_button
        self.result_ready = result_ready
        self.existing_images = existing_images or []
        self.network_result_available = True
        self.prompt_value = ""
        self.submitted = False
        self.generating = False
        self.gate_was_closed = False
        self._modal_shown = False
        self.file_chooser_opened = False
        self.uploaded_file = None
        self.clicked_downloads = 0
        self._handlers = {}
        self.keyboard = FakeKeyboard(self)
        self.mouse = FakeMouse(self)
        self.request = FakeRequest(self)
        self.screenshots = []
        self.goto_calls = []

    # ── служебное ──────────────────────────────────────────────
    @property
    def url(self):
        return self._url

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append(url)
        self._url = url

    async def wait_for_load_state(self, *a, **kw):
        return None

    async def reload(self, **kw):
        return None

    @property
    def frames(self):
        return []

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def off(self, event, handler):
        if handler in self._handlers.get(event, []):
            self._handlers[event].remove(handler)

    async def add_init_script(self, *a, **kw):
        return None

    def write_png(self, path=None):
        if path:
            self.screenshots.append(path)
            with open(path, "wb") as f:
                f.write(PNG_BYTES)

    async def screenshot(self, path=None, **kw):
        self.write_png(path)

    def _emit(self, event, obj):
        for h in self._handlers.get(event, []):
            h(obj)

    def _emit_download(self, name="arena_result.png"):
        dl = FakeDownload(name)
        for h in self._handlers.get("download", []):
            h(dl)
        return dl

    async def _click(self, sel, text=""):
        low = (sel or "").lower() + " " + (text or "").lower()
        if "send message" in low or "submit" in low or "svg" in low:
            await self._submit()
        elif "add file" in low or "attach" in low or "upload" in low:
            self.file_chooser_opened = True

    async def _press(self, key):
        if key == "Enter":
            if self.gate_open:
                # Модалка согласия «съедает» Enter — генерация не стартует
                self.gate_open = False
                self.gate_was_closed = True
                return
            if self.modal_on_submit and not self.submitted and not self._modal_shown:
                # Модалка вылезает в момент отправки и блокирует composer
                self._modal_shown = True
                self.gate_open = True
                return
            if self.prompt_value.strip():
                await self._submit()
        elif key in ("Control+A", "Delete"):
            self.prompt_value = "Ctrl" if key == "Control+A" else ""

    async def _submit(self):
        if self.gate_open:
            return
        self.submitted = True
        self.generating = True
        # реальная отправка уходит запросом на stream — это и видит бот
        self._emit("request", FakeNetRequest("POST", "https://arena.ai/nextjs-api/stream/create-evaluation"))
        # ответ на отправку: 200 — принят, 429 и т. п. — отказ Arena
        self._emit("response", FakeResponse("https://arena.ai/nextjs-api/stream/create-evaluation",
                                            body=b"", ct="application/json",
                                            status=getattr(self, "submit_status", 200)))
        if self.result_ready:
            self.generating = False

    # ── селекторы/локаторы ─────────────────────────────────────
    async def wait_for_selector(self, sel, timeout=None, state=None):
        if "security verification" in sel:
            raise TimeoutError("no captcha")
        if "textarea" in sel or "textbox" in sel or "input" in sel:
            if "Describe the image" in sel or sel.strip() == "textarea":
                el = FakeElement(self, sel)
                # имитируем отсутствие специфичного placeholder-селектора
                if "Describe the image" in sel and "image" not in self._url:
                    raise TimeoutError("no match")
                return el
            raise TimeoutError("no match")
        return FakeElement(self, sel)

    async def query_selector(self, sel):
        if "security verification" in sel:
            return None
        if "recaptcha" in sel or "challenges.cloudflare" in sel:
            return None
        if 'button[aria-label="Stop generation"]' in sel:
            return FakeElement(self, sel) if self.generating else None
        if "Stop" in sel:
            return FakeElement(self, sel) if self.generating else None
        if sel.startswith("img[src="):
            if self.result_ready:
                return FakeElement(self, sel)
            return None
        if 'input[type="file"]' in sel:
            return FakeElement(self, sel)
        if 'input[type="checkbox"]' in sel:
            return None
        return None

    async def query_selector_all(self, sel):
        if "img" in sel and "src" not in sel:
            if self.result_ready:
                return [FakeElement(self, "img")]
            return []
        if "Download" in sel or "download" in sel or "a[download]" in sel:
            if self.result_ready and self.download_button:
                return [FakeElement(self, sel, "Download")]
            return []
        return []

    def get_by_role(self, role, name=None):
        return FakeLocator(self, role, name)

    def expect_download(self, timeout=None):
        return _ExpectDownload(self, FakeDownload())

    def expect_file_chooser(self, timeout=None):
        return _ExpectFileChooser(self)

    # ── JS-диспетчер (эмулируем сниппеты arena_generator) ──────
    async def evaluate(self, js, arg=None):
        js = js or ""
        if "labels.map(l => l.toLowerCase())" in js:
            labels = [l.lower() for l in (arg or [])]
            if self.gate_open and any(l in labels for l in ("agree", "i agree", "accept cookies", "got it")):
                self.gate_open = False
                return "agree"
            return ""
        if "dialogs.filter" in js and "hit enter on your keyboard to agree" in js:
            return self.gate_open
        if "editorEmpty" in js:
            body = "Generating your image" if self.generating else ""
            return {
                "editorEmpty": not self.prompt_value.strip(),
                "generating": self.generating,
                "stop": self.generating,
                "promptInPage": (bool(self.prompt_value) and self.submitted) or getattr(self, "echo_prompt", False),
                "hasDownloadOrEdit": self.result_ready,
                "url": self._url,
            }
        if "isJunk" in js:
            imgs = []
            if self.result_ready and self.submitted:
                imgs = [{"src": "https://cdn.arena.ai/result.png", "w": 1024, "h": 1024, "scope": "assistant"}]
            return {
                "images": imgs,
                "stop": self.generating,
                "spinning": self.generating,
                "generating": self.generating,
                "actionBar": self.result_ready and self.submitted,
                "errorText": "",
                "assistantFound": self.submitted,
                "url": self._url,
            }
        if "targets.forEach" in js:
            # кнопки скачивания есть не всегда (иконка появляется с задержкой)
            if not (self.result_ready and self.submitted and self.download_button):
                return 0
            self.clicked_downloads += 1
            self._emit_download()
            return 1
        if "editSiblings" in js:
            vids = []
            if self.result_ready and self.submitted:
                vids = ["https://cdn.arena.ai/v1.mp4", "https://cdn.arena.ai/v2.mp4"]
            return {"videos": vids, "dlButtons": 1 if (self.result_ready and self.download_button) else 0,
                    "editSiblings": 1 if self.result_ready else 0, "stop": self.generating,
                    "spinning": self.generating, "generating": self.generating,
                    "errorText": "", "done": self.result_ready, "url": self._url}
        if "currentSrc || i.src" in js and "Array.from" in js:
            return list(self.existing_images)
        if "cand[cand.length - 1].click()" in js:
            await self._submit()
            return True
        if "querySelectorAll('input[type=\"file\"]')" in js:
            return True
        if "HTMLTextAreaElement.prototype" in js:
            self.prompt_value = arg or ""
            return True
        if "await fetch(u" in js:
            if self.network_result_available:
                return {"b64": base64.b64encode(PNG_BYTES).decode(), "type": "image/png"}
            return None
        if "nodes.map(m => m.innerText" in js:
            return []
        return None


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.request = page.request

    async def cookies(self):
        return [{"name": "cookie-preferences", "value": "1", "domain": ".arena.ai", "path": "/"}]

    async def add_cookies(self, cookies):
        return None

    def on(self, event, handler):
        return None

    async def new_page(self):
        return self.page


def make_generator(page: FakePage, tmp_path=None):
    gen = ag.ArenaGenerator()
    gen.page = page
    gen.context = FakeContext(page)
    gen.browser = object()  # «браузер уже запущен»

    # download-события страницы должен ловить обработчик генератора
    page.on("download", gen._on_download)
    # запросы страницы — по ним бот подтверждает, что промпт ушёл (как в _attach_page_handlers)
    page.on("request", gen._on_request)
    page.on("response", gen._on_response)
    gen._page_handlers_attached = True

    # ускоряем тайминги, чтобы тесты шли секунды, а не минуты
    gen.submit_check_seconds = 0.6
    gen.consent_check_interval = 0.1
    gen.image_poll_seconds = 0.2
    gen.video_poll_seconds = 0.2
    gen.download_wait_seconds = 2
    gen.download_click_interval = 0.1

    async def _save_cookies():
        return None

    gen.save_cookies = _save_cookies  # не пишем реальный файл cookies
    return gen


# ═══════════════════════════════════════════════════════════════════
# 1. Чистая логика
# ═══════════════════════════════════════════════════════════════════
def test_media_kind_filters_icons_and_detects_media():
    gen = ag.ArenaGenerator()
    assert gen._media_kind("https://cdn.arena.ai/result.png", "image/png") == "image"
    assert gen._media_kind("https://cdn.arena.ai/clip.mp4", "video/mp4") == "video"
    assert gen._media_kind("https://arena.ai/logo.svg", "image/svg+xml") == ""
    assert gen._media_kind("https://arena.ai/static/favicon.ico", "image/x-icon") == ""
    assert gen._media_kind("https://arena.ai/avatar.png", "image/png") == ""
    assert gen._media_kind("https://arena.ai/app.js", "application/javascript") == ""


def test_ext_helpers():
    assert ag._ext_from_content_type("image/png; charset=binary") == ".png"
    assert ag._ext_from_content_type("video/mp4") == ".mp4"
    assert ag._ext_from_url("https://cdn.arena.ai/a/b/result.webp?x=1") == ".webp"
    assert ag._ext_from_url("https://cdn.arena.ai/download") == ".png"


def test_parse_stream_text_extracts_image_and_errors():
    gen = ag.ArenaGenerator()
    stream = (
        'a2:{"id":"x","c":[{"image":"https://cdn.arena.ai/out/result.png?sig=1"}]}\n'
        'a3:{"error":{"message":"moderation_blocked","code":"x"}}\n'
        'ae:{"message":"Arena platform error"}\n'
    )
    gen._parse_stream_text(stream)
    assert any("result.png" in u for u in gen._stream_urls)
    assert any("moderation_blocked" in e for e in gen._stream_errors)
    assert any("Arena platform error" in e for e in gen._stream_errors)


def test_parse_stream_text_handles_video_urls():
    gen = ag.ArenaGenerator()
    gen._parse_stream_text('a2:{"videos":["https://cdn.arena.ai/v/a.mp4","https://cdn.arena.ai/v/b.mp4"]}')
    assert len([u for u in gen._stream_urls if u.endswith(".mp4")]) == 2


def test_collect_urls_from_payload_is_recursive():
    gen = ag.ArenaGenerator()
    bucket = []
    gen._collect_urls_from_payload(
        '{"a":[{"deep":{"image":"https://cdn.arena.ai/x/y.png"}}],"b":"https://cdn.arena.ai/z.mp4"}', bucket
    )
    assert "https://cdn.arena.ai/x/y.png" in bucket
    assert "https://cdn.arena.ai/z.mp4" in bucket


# ═══════════════════════════════════════════════════════════════════
# 2. Управляющий поток
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_submit_works_when_consent_modal_swallows_enter():
    """Боевой кейс: модалка Terms of Use вылезает на отправке и съедает Enter."""
    page = FakePage(gate_open=True, modal_on_submit=True)
    gen = make_generator(page)

    await gen.ensure_consent()
    assert page.gate_open is False

    page.modal_on_submit = True  # модалка снова вылезает при отправке
    ok = await gen.submit_prompt("neon city", mode="image")
    assert ok is True
    assert page.submitted is True


@pytest.mark.asyncio
async def test_submit_raises_when_nothing_happens():
    """Если промпт так и не ушёл — должно быть исключение (а не тихий таймаут)."""
    page = FakePage(gate_open=False, modal_on_submit=False)

    # Enter и клик не дают результата: composer «мёртвый»
    page._submit = lambda: asyncio.sleep(0)  # type: ignore
    gen = make_generator(page)

    with pytest.raises(Exception) as exc:
        await gen.submit_prompt("test prompt", mode="image")
    assert "отправить промпт" in str(exc.value).lower() or "соглас" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_wait_for_image_uses_download_button(tmp_path):
    page = FakePage(download_button=True)
    gen = make_generator(page)
    gen.start_capture(str(tmp_path / ".downloads"))
    gen._download_dir = str(tmp_path / ".downloads")

    page._submit = page._submit  # noqa
    await page._submit()
    path = await gen.wait_for_image(timeout=20, output_dir=str(tmp_path))
    assert path and os.path.exists(path)
    assert page.clicked_downloads == 1


@pytest.mark.asyncio
async def test_wait_for_image_falls_back_to_network_fetch(tmp_path):
    """Нет текстовой кнопки «Download» — забираем результат из сети."""
    page = FakePage(download_button=False, result_ready=True)
    gen = make_generator(page)
    gen.start_capture(str(tmp_path / ".downloads"))
    await page._submit()

    path = await gen.wait_for_image(timeout=20, output_dir=str(tmp_path))
    assert path and os.path.exists(path)
    assert os.path.getsize(path) > 0
    assert any("result.png" in u or "cdn.arena.ai" in u for u in page.request.calls)


@pytest.mark.asyncio
async def test_wait_for_image_ignores_baseline_images(tmp_path):
    """Старые картинки (логотипы/прошлые результаты) не должны отдаваться."""
    page = FakePage(existing_images=["https://arena.ai/logo.png"], result_ready=False)
    gen = make_generator(page)
    gen.start_capture(str(tmp_path / ".downloads"))
    await page._submit()

    with pytest.raises(Exception):
        await gen.wait_for_image(timeout=5, output_dir=str(tmp_path))


@pytest.mark.asyncio
async def test_generate_video_saves_two_variants(tmp_path):
    page = FakePage(download_button=True)
    gen = make_generator(page)

    async def fake_navigate(url, ui_selector=None, mode=None):
        gen._baseline_images = set()

    gen._navigate_and_wait = fake_navigate  # type: ignore
    page.network_result_available = True

    paths = await gen.generate_video("drone over lake", output_dir=str(tmp_path), timeout=30)
    assert len(paths) >= 1
    for p in paths:
        assert os.path.exists(p) and os.path.getsize(p) > 0


@pytest.mark.asyncio
async def test_animate_image_uploads_file(tmp_path):
    page = FakePage(download_button=False)
    gen = make_generator(page)

    async def fake_navigate(url, ui_selector=None, mode=None):
        gen._baseline_images = set()

    gen._navigate_and_wait = fake_navigate  # type: ignore
    img = tmp_path / "src.png"
    img.write_bytes(PNG_BYTES)

    paths = await gen.animate_image(str(img), "gentle motion", output_dir=str(tmp_path), timeout=30)
    assert page.uploaded_file == str(img)
    assert len(paths) >= 1


@pytest.mark.asyncio
async def test_generate_image_raises_on_empty_result(tmp_path, monkeypatch):
    """Ключевой баг старой версии: при ошибке возвращался None вместо исключения."""
    page = FakePage(result_ready=False)
    gen = make_generator(page)

    async def fake_navigate(url, ui_selector=None, mode=None):
        gen._baseline_images = set()

    gen._navigate_and_wait = fake_navigate  # type: ignore
    monkeypatch.setattr(ag, "IMAGE_TIMEOUT", 5)

    with pytest.raises(Exception):
        await gen.generate_image("a cat", output_dir=str(tmp_path))


# ═══════════════════════════════════════════════════════════════════
# 3. Регрессии после разбора «промпт не ушёл»
# ═══════════════════════════════════════════════════════════════════
def test_no_offsetparent_visibility_checks():
    """
    offsetParent === null у position:fixed → модалка Terms of Use (fixed-портал)
    считалась невидимой, Agree не нажимался, composer оставался disabled,
    и промпт не уходил. Проверка должна идти через getComputedStyle.
    """
    src = pathlib.Path(ag.__file__).read_text(encoding="utf-8")
    assert "offsetParent" not in src, "вернулась проверка видимости, ломающаяся на position:fixed"
    assert "getComputedStyle" in src


@pytest.mark.asyncio
async def test_wait_composer_ready_does_not_block_on_unknown_state():
    """Если состояние composer неизвестно — не блокируем отправку."""
    page = FakePage()
    gen = make_generator(page)
    assert await gen.wait_composer_ready(timeout=1) is True


@pytest.mark.asyncio
async def test_composer_blocker_returns_empty_without_info():
    page = FakePage()
    gen = make_generator(page)
    assert not await gen.composer_blocker()


@pytest.mark.asyncio
async def test_submit_reports_reasons_in_error():
    """При провале отправки в тексте ошибки должны быть конкретные причины."""
    page = FakePage()
    page._submit = lambda: asyncio.sleep(0)  # composer «мёртвый»
    gen = make_generator(page)
    with pytest.raises(Exception) as exc:
        await gen.submit_prompt("некоторый промпт", mode="video")
    text = str(exc.value)
    assert "arena.ai" in text
    assert "diagnose_arena.py" in text  # подсказка, что запустить для разбора


# ═══════════════════════════════════════════════════════════════════
# Регрессия: промо-ролик Arena принимался за результат видео
#   В боевом логе «Downloaded 2568 KB» ×2 — это agents-cta.mp4
#   (2 630 448 байт = 2568.8 KiB). Бот отправлял его пользователю.
# ═══════════════════════════════════════════════════════════════════
CTA_URL = "https://arena.ai/videos/cta/agents-cta.mp4"
REAL_URL = "https://cdn.example.net/results/abc123.mp4"
BIG_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 60_000


def test_cta_promo_video_is_not_a_media_result():
    gen = ag.ArenaGenerator()
    assert gen._media_kind(CTA_URL, "video/mp4") == ""
    assert gen._media_kind(REAL_URL, "video/mp4") == "video"


async def test_save_media_refuses_static_and_pre_send_urls(monkeypatch, tmp_path):
    gen = ag.ArenaGenerator()
    fetched = []

    async def fake_fetch(url, timeout_ms=120000):
        fetched.append(url)
        return BIG_MP4, ".mp4"

    monkeypatch.setattr(gen, "_fetch_bytes", fake_fetch)
    input_preview = "https://cdn.example.net/input/preview.mp4"
    gen._pre_send_media = {input_preview}

    kw = dict(fallback_ext=".mp4", min_bytes=20000)
    assert await gen._save_media_url(CTA_URL, str(tmp_path), "video", **kw) is None
    assert await gen._save_media_url(input_preview, str(tmp_path), "video", **kw) is None
    assert fetched == []  # статику и вложение даже не скачиваем

    path = await gen._save_media_url(REAL_URL, str(tmp_path), "video", **kw)
    assert path and path.endswith(".mp4")


async def test_identical_files_are_saved_only_once(monkeypatch, tmp_path):
    gen = ag.ArenaGenerator()

    async def fake_fetch(url, timeout_ms=120000):
        return BIG_MP4, ".mp4"  # оба «варианта» одинаковые — это ошибка выбора

    monkeypatch.setattr(gen, "_fetch_bytes", fake_fetch)
    kw = dict(fallback_ext=".mp4", min_bytes=20000)
    first = await gen._save_media_url(REAL_URL, str(tmp_path), "video", index=0, **kw)
    second = await gen._save_media_url(
        "https://cdn.example.net/results/other.mp4", str(tmp_path), "video", index=1, **kw
    )
    assert first is not None
    assert second is None


async def test_dom_promo_video_without_result_card_is_ignored(monkeypatch, tmp_path):
    """<video> промо-ролика на странице без карточки результата не должен попасть в выдачу."""
    gen = ag.ArenaGenerator()
    gen.start_capture(str(tmp_path / ".downloads"))
    fetched = []

    async def fake_state():
        return {"videos": [CTA_URL], "dlButtons": 0, "editSiblings": 0,
                "stop": False, "spinning": False, "generating": False, "errorText": ""}

    async def fake_fetch(url, timeout_ms=120000):
        fetched.append(url)
        return BIG_MP4, ".mp4"

    monkeypatch.setattr(gen, "_video_state", fake_state)
    monkeypatch.setattr(gen, "_fetch_bytes", fake_fetch)
    monkeypatch.setattr(gen, "consent_check_interval", 999)
    gen.video_poll_seconds = 0.05

    async def no_consent():
        return False

    monkeypatch.setattr(gen, "dismiss_got_it", no_consent)
    monkeypatch.setattr(gen, "ensure_consent", no_consent)
    monkeypatch.setattr(gen, "handle_captcha", no_consent)

    with pytest.raises(Exception):
        await gen.wait_for_video(timeout=1, output_dir=str(tmp_path))
    assert CTA_URL not in fetched


# ═══════════════════════════════════════════════════════════════════
# Капча: клик по чекбоксу reCAPTCHA/Turnstile внутри iframe
#   Боевой лог: «Captcha click failed: Timeout 10000ms exceeded» — старый код
#   ждал видимый body/чекбокс во фрейме, выбранном по URL, и зависал.
# ═══════════════════════════════════════════════════════════════════
class _CaptchaMouse:
    def __init__(self, clicks):
        self.clicks = clicks

    async def move(self, x, y, steps=None):
        pass

    async def click(self, x, y, delay=None):
        self.clicks.append((x, y))


class _CaptchaIframe:
    async def bounding_box(self):
        return {"x": 100, "y": 200, "width": 304, "height": 78}


class _CaptchaPage:
    def __init__(self, clicks, widget=True, modal=False):
        self.mouse = _CaptchaMouse(clicks)
        self.frames = []
        self._widget = widget
        self._modal = modal

    async def query_selector(self, sel):
        if "recaptcha/api2/anchor" in sel and self._widget:
            return _CaptchaIframe()
        if "security verification" in sel and self._modal:
            return object()
        return None


async def test_captcha_click_targets_checkbox_inside_iframe():
    clicks = []
    gen = ag.ArenaGenerator()
    gen.page = _CaptchaPage(clicks)
    assert await gen._click_captcha_checkbox() is True
    x, y = clicks[0]
    # левый край виджета (x + 28), по вертикали по центру
    assert 120 <= x <= 140
    assert 230 <= y <= 270


async def test_captcha_click_without_widget_does_not_click():
    clicks = []
    gen = ag.ArenaGenerator()
    gen.page = _CaptchaPage(clicks, widget=False)
    assert await gen._click_captcha_checkbox() is False
    assert clicks == []


async def test_captcha_done_when_modal_gone():
    gen = ag.ArenaGenerator()
    gen.page = _CaptchaPage([], modal=False)
    assert await gen._wait_captcha_done(timeout=3) is True


# ═══════════════════════════════════════════════════════════════════
# Ложная ошибка «Arena вернула ошибку»: в меню режимов есть подпись
#   «Not available for Video» — это не ошибка генерации.
# ═══════════════════════════════════════════════════════════════════
def _noop_video_handlers(gen):
    async def _false(*a, **kw):
        return False

    gen.dismiss_got_it = _false
    gen.ensure_consent = _false
    gen.handle_captcha = _false


async def test_static_ui_text_is_not_reported_as_error(monkeypatch, tmp_path):
    gen = ag.ArenaGenerator()
    gen.video_poll_seconds = 0.05
    gen.video_start_timeout = 0.3
    _noop_video_handlers(gen)

    async def fake_state():
        return {"videos": [], "dlButtons": 0, "editSiblings": 0, "stop": False, "spinning": False,
                "generating": False, "errorText": "Side by Side Not available for Video", "url": ""}

    monkeypatch.setattr(gen, "_video_state", fake_state)
    with pytest.raises(Exception) as exc:
        await gen.wait_for_video(timeout=2, output_dir=str(tmp_path))
    assert "вернула ошибку" not in str(exc.value)
    assert "не стартовала" in str(exc.value)


async def test_real_moderation_error_after_start_is_reported(monkeypatch, tmp_path):
    gen = ag.ArenaGenerator()
    gen.video_poll_seconds = 0.05
    _noop_video_handlers(gen)
    states = iter([
        {"videos": [], "dlButtons": 0, "editSiblings": 0, "stop": True, "spinning": True,
         "generating": True, "errorText": "", "url": ""},
        {"videos": [], "dlButtons": 0, "editSiblings": 0, "stop": False, "spinning": False,
         "generating": False, "errorText": "Your prompt was blocked by moderation", "url": ""},
    ])

    async def fake_state():
        return next(states)

    monkeypatch.setattr(gen, "_video_state", fake_state)
    with pytest.raises(Exception) as exc:
        await gen.wait_for_video(timeout=5, output_dir=str(tmp_path))
    assert "вернула ошибку" in str(exc.value)
    assert "moderation" in str(exc.value)


# ═══════════════════════════════════════════════════════════════════
# «Промт не ушёл» для видео: бот считал отправку успешной по тексту на странице
#   Теперь успех — только по POST /nextjs-api/stream, Stop или сообщению пользователя.
# ═══════════════════════════════════════════════════════════════════
async def test_echoed_prompt_text_is_not_proof_of_sending():
    page = FakePage()
    page.echo_prompt = True  # текст промпта «виден» на странице, но запроса нет
    page._submit = lambda: asyncio.sleep(0)  # type: ignore
    gen = make_generator(page)
    with pytest.raises(Exception) as exc:
        await gen.submit_prompt("некоторый промпт", mode="video")
    assert "не ушёл" in str(exc.value)


async def test_success_is_backed_by_stream_request():
    page = FakePage()
    gen = make_generator(page)
    assert await gen.submit_prompt("neon city", mode="image") is True
    assert gen._stream_posts, "успех должен подтверждаться сетевым запросом"


# ═══════════════════════════════════════════════════════════════════
# Отказ Arena (HTTP 429) на отправку: раньше бот «ждал» и повторял Enter
# ═══════════════════════════════════════════════════════════════════
async def test_rate_limited_submit_is_reported_without_retries():
    page = FakePage()
    page.submit_status = 429
    calls = []
    orig = page._submit

    async def counting():
        calls.append(1)
        await orig()

    page._submit = counting
    gen = make_generator(page)
    gen.submit_check_seconds = 2.0
    with pytest.raises(ag.ArenaRejectedError) as exc:
        await gen.submit_prompt("некоторый промпт", mode="video")
    assert "429" in str(exc.value)
    assert len(calls) == 1  # без повторных Enter, которые усугубляют лимит
