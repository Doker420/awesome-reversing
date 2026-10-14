"""Патч arena_generator.py: не отдавать исходное фото как «результат редактирования».

Проблема
--------
В режиме редактирования фото (`edit_image`) генератор мог вернуть то же самое фото,
которое вы отправили, не дожидаясь генерации. Причин в коде было сразу несколько:

  1) `edit_image()` включает перехват сети (`start_capture`) ДО загрузки файла,
     поэтому ответ на загрузку вашего фото на CDN Arena попадал в `_captured["image"]`
     и позже сохранялся как «результат»;
  2) эхо вложения в ленте (blob:-превью / тот же файл по CDN-ссылке) — это «новый»
     относительно `_baseline_images` URL, поэтому DOM-путь тоже его отдавал;
  3) проверки в `wait_for_image` не смотрели на кнопку «Stop generation»: картинка
     сохранялась в момент, когда генерация ещё шла;
  4) скриншот элемента (шаг 4) вообще не проходил ни одну проверку исключений;
  5) `_click_download_candidates` кликал «Download» в любой карточке на странице —
     включая старую (предыдущую) карточку результата.

Что делает патч
---------------
* помечает вложение как референс: считает SHA-256 отправленного файла, а также
  запоминает URL превью и ответы загрузки (они больше не считаются результатом);
* `_is_excluded_media()` учитывает эти референс-URL;
* `_save_media_url()` пропускает файл, содержимое которого совпадает с отправленным
  фото (побайтово) — это и есть «вернулось то же фото»;
* `wait_for_image()` не сохраняет ничего, пока на странице есть «Stop generation»;
  приоритет у ссылок из SSE-потока генерации, затем карточка результата, затем сеть,
  затем скриншот (с той же проверкой на эхо);
* `_result_images_dom()` ищет ответ ассистента только ПОСЛЕ последнего сообщения
  пользователя (не берёт старую карточку) и игнорирует картинки внутри сообщений
  пользователя;
* `_click_download_candidates()` кликает только внутри актуальной карточки результата;
* `edit_image()` в конце сверяет хэши и падает с понятной ошибкой, если вернулось
  исходное фото (лучше честная ошибка, чем отправить пользователю его же файл).

Использование
-------------
    python fix_edit_image_echo.py                # патчит arena_generator.py рядом со скриптом
    python fix_edit_image_echo.py --dir PROJECT  # указать папку проекта
    python fix_edit_image_echo.py --check        # только показать, что будет изменено
    python fix_edit_image_echo.py --restore      # вернуть arena_generator.py из .bak

Скрипт идемпотентен: повторный запуск ничего не делает. Перед правкой создаётся
arena_generator.py.bak. Рядом (если есть папка tests/) кладётся регресс-тест
tests/test_edit_echo_fix.py. Запуск тестов:  python -m pytest -q tests/
"""

import argparse
import py_compile
import shutil
import sys
import tempfile
from pathlib import Path

# ════════════════════════════════════════════════════════════════════
#  Шаги патча: (имя, что ищем, на что меняем)
# ════════════════════════════════════════════════════════════════════
STEPS = []


def step(name, old, new):
    STEPS.append((name, old, new))


step(
    "1. Память о вложении-референсе",
    """        # SHA-256 уже сохранённых файлов текущей задачи (два одинаковых варианта = баг)
        self._saved_hashes = set()
""",
    """        # SHA-256 уже сохранённых файлов текущей задачи (два одинаковых варианта = баг)
        self._saved_hashes = set()
        # SHA-256 отправленных вложений: их эхо — не результат генерации
        self._upload_digests = set()
        # Медиа-URL вложения (превью, эхо в ленте, ответы загрузки на CDN)
        self._reference_media = set()
""",
)

step(
    "2. Сброс памяти вложения на каждую задачу",
    """    def _begin_generation(self):
        self._last_upload_path = None
""",
    """    def _begin_generation(self):
        self._last_upload_path = None
        self._upload_digests = set()
        self._reference_media = set()
""",
)

step(
    "3a. После загрузки через кнопку — пометить референс",
    """                    chooser = await fc_info.value
                    await chooser.set_files(image_path)
                    logger.info(f"Image uploaded via '{name}' button ✅")
                    await asyncio.sleep(2.5)
                    return True
""",
    """                    chooser = await fc_info.value
                    await chooser.set_files(image_path)
                    logger.info(f"Image uploaded via '{name}' button ✅")
                    await asyncio.sleep(2.5)
                    await self._mark_upload_reference()
                    return True
""",
)

step(
    "3b. После загрузки через input[type=file] — пометить референс",
    """                await file_input.set_input_files(image_path, timeout=10000)
                logger.info("Image uploaded via input[type=file] ✅")
                await asyncio.sleep(2.5)
                return True
""",
    """                await file_input.set_input_files(image_path, timeout=10000)
                logger.info("Image uploaded via input[type=file] ✅")
                await asyncio.sleep(2.5)
                await self._mark_upload_reference()
                return True
""",
)

step(
    "3c. После загрузки через принудительный input — пометить референс",
    """                await fi.set_input_files(image_path, force=True)
                logger.info("Image uploaded via forced file input ✅")
                await asyncio.sleep(2.5)
                return True
""",
    """                await fi.set_input_files(image_path, force=True)
                logger.info("Image uploaded via forced file input ✅")
                await asyncio.sleep(2.5)
                await self._mark_upload_reference()
                return True
""",
)

step(
    "4. Метод _mark_upload_reference() + helper для проверки файла",
    """    # ═════════════════════════════════════════════════════════
    # Отправка промпта
    # ═════════════════════════════════════════════════════════
    async def _generation_state(self, prompt: str = "") -> dict:
""",
    '''    async def _mark_upload_reference(self):
        """Пометить отправленный файл как референс: его эхо — не результат генерации.

        Пока ничего не генерируется, любые медиа-ответы сети — это загрузка нашего же
        вложения на CDN Arena. Убираем их из кандидатов и запоминаем SHA-256 файла.
        """
        path = self._last_upload_path
        if path and os.path.isfile(path):
            try:
                with open(path, "rb") as stream:
                    self._upload_digests.add(hashlib.sha256(stream.read()).hexdigest())
            except OSError as e:
                logger.debug(f"upload digest failed: {e}")
        # 1) ответы сети за время загрузки — это наша же картинка на CDN
        captured = [item for item in self._captured.get("image", []) if item.get("url")]
        reference = {item["url"] for item in captured}
        if reference:
            self._captured["image"] = [item for item in self._captured["image"]
                                       if item.get("url") not in reference]
            self._reference_media |= reference
        # 2) превью вложения (blob:/data:) — тоже референс, а не результат
        try:
            urls = await self.page.evaluate(
                """() => Array.from(document.querySelectorAll('img, video, source'))
                        .map(e => e.currentSrc || e.src || '')
                        .filter(Boolean)"""
            )
            self._reference_media |= {u for u in (urls or []) if u.startswith("blob:") or u.startswith("data:")}
        except Exception as e:
            logger.debug(f"reference media scan failed: {e}")
        if reference or self._upload_digests:
            logger.info("Вложение помечено как референс: исключено URL=%s, хэшей=%s",
                        len(reference), len(self._upload_digests))

    def _is_upload_echo_file(self, path: str) -> bool:
        """True, если файл побайтово совпадает с отправленным фото (эхо вложения)."""
        if not self._upload_digests:
            return False
        try:
            with open(path, "rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
        except OSError:
            return False
        return digest in self._upload_digests

    # ═════════════════════════════════════════════════════════
    # Отправка промпта
    # ═════════════════════════════════════════════════════════
    async def _generation_state(self, prompt: str = "") -> dict:
''',
)

step(
    "5. _is_excluded_media() знает про референс",
    """    def _is_excluded_media(self, url: str) -> bool:
        \"\"\"True для URL, которые ни при каких условиях не являются результатом генерации.\"\"\"
        if not url or url.startswith("data:"):
            return True
        if ARENA_STATIC_MEDIA_RE.search(url):
            return True
        return url in self._pre_send_media
""",
    """    def _is_excluded_media(self, url: str) -> bool:
        \"\"\"True для URL, которые ни при каких условиях не являются результатом генерации.\"\"\"
        if not url or url.startswith("data:"):
            return True
        if ARENA_STATIC_MEDIA_RE.search(url):
            return True
        if url in self._reference_media:
            return True
        return url in self._pre_send_media
""",
)

step(
    "6. _save_media_url() пропускает файл, совпавший с отправленным фото",
    """        # два одинаковых файла в одной задаче — это не два результата, а ошибка выбора
        digest = hashlib.sha256(body).hexdigest()
        if digest in self._saved_hashes:
""",
    """        # два одинаковых файла в одной задаче — это не два результата, а ошибка выбора
        digest = hashlib.sha256(body).hexdigest()
        if digest in self._upload_digests:
            logger.warning("Пропуск: содержимое совпадает с отправленным фото (эхо вложения) — это не результат")
            return None
        if digest in self._saved_hashes:
""",
)

step(
    "7. Поиск карточки результата: только ответ ассистента после последнего сообщения",
    """                    let scope = null;
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
""",
    """                    const isUserNode = (c) => c.classList.contains('justify-end')
                        || !!c.querySelector('[data-role="user"]');
                    let scope = null;
                    const ol = document.querySelector('ol.flex-col-reverse') || document.querySelector('ol');
                    if (ol) {
                        const kids = Array.from(ol.children).filter(vis);
                        // Ответ ассистента ищем ТОЛЬКО после последнего сообщения пользователя:
                        // предыдущая готовая карточка результатом текущей задачи не является.
                        let lastUser = -1;
                        kids.forEach((c, i) => { if (isUserNode(c)) lastUser = i; });
                        for (let i = lastUser + 1; i < kids.length; i++) {
                            if (!isUserNode(kids[i])) { scope = kids[i]; break; }
                        }
                    }
                    const inUserBubble = (img) => !!img.closest('.justify-end, [data-role="user"]');
                    if (scope) scope.querySelectorAll('img').forEach(i => { if (!inUserBubble(i)) push(i, 'assistant'); });
                    if (!out.length) {
                        // резерв — но только вне сообщений пользователя, где лежит вложение
                        document.querySelectorAll('main img, figure img, [data-testid*="result" i] img')
                            .forEach(i => { if (!inUserBubble(i)) push(i, 'main'); });
                    }
""",
)

step(
    "8. Кнопки скачивания — только в актуальной карточке результата",
    """                    const add = (el) => {
                        if (!el || seen.has(el) || el.disabled || !vis(el)) return;
                        seen.add(el);
                        targets.push(el);
                    };
""",
    """                    const scope = (() => {
                        const ol = document.querySelector('ol.flex-col-reverse') || document.querySelector('ol');
                        if (!ol) return null;
                        const kids = Array.from(ol.children).filter(vis);
                        const isUser = (c) => c.classList.contains('justify-end') || !!c.querySelector('[data-role="user"]');
                        let lastUser = -1;
                        kids.forEach((c, i) => { if (isUser(c)) lastUser = i; });
                        for (let i = lastUser + 1; i < kids.length; i++) if (!isUser(kids[i])) return kids[i];
                        return null;
                    })();
                    const add = (el) => {
                        if (!el || seen.has(el) || el.disabled || !vis(el)) return;
                        if (scope && !scope.contains(el)) return;   // кнопка из старой карточки — не наша
                        seen.add(el);
                        targets.push(el);
                    };
""",
)

step(
    "9. wait_for_image(): не отдавать эхо, ждать окончания генерации",
    """            state = await self._result_images_dom()
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
                    el = await self.page.query_selector('img[src="%s"]' % images[0]["src"].replace('"', '\\\\"'))
                    if el:
                        filepath = os.path.join(output_dir, f"image_{int(time.time())}.png")
                        await el.screenshot(path=filepath)
                        logger.info(f"✅ Изображение сохранено скриншотом элемента: {filepath}")
                        return filepath
                except Exception as e:
                    logger.debug(f"element screenshot failed: {e}")
""",
    """            state = await self._result_images_dom()
            images = [i for i in state.get("images", [])
                      if i.get("src") not in self._baseline_images
                      and not self._is_excluded_media(i.get("src"))]
            # Пока видна кнопка «Stop generation», Arena ещё рисует: всё, что появилось
            # в ленте в этот момент (в том числе эхо нашего вложения) — не результат.
            generating_now = bool(state.get("stop"))

            # 1) кнопка Download в актуальной карточке результата (полноразмерный файл)
            if images and not generating_now and (time.time() - last_download_click) > self.download_click_interval:
                last_download_click = time.time()
                clicked = await self._click_download_candidates()
                if clicked:
                    files = await self._collect_downloads(expected=1, wait_seconds=self.download_wait_seconds)
                    files = [f for f in files if os.path.exists(f) and os.path.getsize(f) >= 2000]
                    files = [f for f in files if not self._is_upload_echo_file(f)]
                    if files:
                        logger.info("✅ Изображение получено кнопкой Download")
                        return files[0]

            # 2) прямые ссылки из SSE-потока генерации — появляются, когда результат готов
            for url in [u for u in self._stream_urls if u not in failed_urls]:
                saved = await self._save_media_url(url, output_dir, "image", fallback_ext=".png")
                if saved:
                    return saved
                failed_urls.add(url)

            if generating_now:
                logger.debug("Генерация ещё идёт (на странице Stop) — найденное изображение не сохраняю")
            else:
                # 3) URL из карточки результата
                for url in [i["src"] for i in images if i["src"] not in failed_urls]:
                    saved = await self._save_media_url(url, output_dir, "image", fallback_ext=".png")
                    if saved:
                        return saved
                    failed_urls.add(url)

                # 4) сетевые кандидаты (перехваченные ответы)
                for item in list(self._captured["image"]):
                    if item["url"] in failed_urls:
                        continue
                    saved = await self._save_media_url(item["url"], output_dir, "image", fallback_ext=".png")
                    if saved:
                        return saved
                    failed_urls.add(item["url"])

                # 5) скриншот элемента (последний резерв) — с теми же проверками
                if images:
                    try:
                        el = await self.page.query_selector('img[src="%s"]' % images[0]["src"].replace('"', '\\\\"'))
                        if el:
                            filepath = os.path.join(output_dir, f"image_{int(time.time())}.png")
                            await el.screenshot(path=filepath)
                            if self._is_upload_echo_file(filepath):
                                logger.warning("Скриншот совпал с отправленным фото — пропускаю")
                                try:
                                    os.remove(filepath)
                                except OSError:
                                    pass
                            else:
                                logger.info(f"✅ Изображение сохранено скриншотом элемента: {filepath}")
                                return filepath
                    except Exception as e:
                        logger.debug(f"element screenshot failed: {e}")
""",
)

step(
    "10. edit_image(): честная ошибка вместо возврата исходного фото",
    """            # 4) ждём результат
            filepath = await self.wait_for_image(timeout=timeout, output_dir=output_dir)
            return filepath
""",
    """            # 4) ждём результат
            filepath = await self.wait_for_image(timeout=timeout, output_dir=output_dir)

            # 5) страховка: исходное фото не должно уйти пользователю как «результат»
            try:
                with open(image_path, "rb") as source, open(filepath, "rb") as result:
                    if hashlib.sha256(source.read()).hexdigest() == hashlib.sha256(result.read()).hexdigest():
                        raise Exception("Arena вернула исходное фото, а не результат редактирования "
                                        "(генерация не стартовала или результат ещё не готов)")
            except OSError:
                pass
            return filepath
""",
)


# ════════════════════════════════════════════════════════════════════
#  Регресс-тест, который кладём рядом
# ════════════════════════════════════════════════════════════════════
TEST_FILE = '''"""Регресс-тесты: в режиме редактирования Arena не должна отдавать исходное фото.

Запуск:  python -m pytest -q tests/test_edit_echo_fix.py
"""
import asyncio
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arena_generator as ag  # noqa: E402
from tests.test_arena_generator import (  # noqa: E402
    FakePage, FakeRequest, FakeResponse, PNG_BYTES, make_generator,
)

ECHO_URL = "blob:https://arena.ai/attached-preview"
RESULT_URL = "https://cdn.arena.ai/edited-result.png"


def _dom(images, stop=False, action_bar=True):
    async def fake() -> dict:
        return {"images": images, "stop": stop, "spinning": stop, "generating": stop,
                "actionBar": action_bar, "errorText": "", "assistantFound": True,
                "url": "https://arena.ai/image/direct"}
    return fake


@pytest.mark.asyncio
async def test_no_result_while_generating(tmp_path):
    """Пока видна кнопка Stop, ничего не сохраняем — даже если в ленте уже что-то есть."""
    page = FakePage(download_button=False)
    gen = make_generator(page)
    gen.start_capture(str(tmp_path / ".downloads"))
    gen._result_images_dom = _dom([{"src": ECHO_URL, "w": 1024, "h": 1024, "scope": "assistant"}], stop=True)

    with pytest.raises(Exception):
        await gen.wait_for_image(timeout=2, output_dir=str(tmp_path))
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".png")]


@pytest.mark.asyncio
async def test_attachment_echo_is_not_saved(tmp_path):
    """URL вложения (превью/эхо) исключён и не может стать «результатом»."""
    page = FakePage(download_button=False)
    gen = make_generator(page)
    gen.start_capture(str(tmp_path / ".downloads"))
    gen._reference_media = {ECHO_URL}
    gen._result_images_dom = _dom([{"src": ECHO_URL, "w": 1024, "h": 1024, "scope": "assistant"}])

    with pytest.raises(Exception):
        await gen.wait_for_image(timeout=2, output_dir=str(tmp_path))
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".png")]


@pytest.mark.asyncio
async def test_same_bytes_as_upload_are_skipped(tmp_path):
    """Файл, побайтово равный отправленному фото, — эхо вложения, а не результат."""
    # result_ready=False: у страницы нет карточки, поэтому резервный скриншот тоже недоступен
    page = FakePage(download_button=False, result_ready=False)
    gen = make_generator(page)
    gen.start_capture(str(tmp_path / ".downloads"))
    gen._upload_digests = {hashlib.sha256(PNG_BYTES * 100).hexdigest()}
    gen._result_images_dom = _dom([{"src": RESULT_URL, "w": 1024, "h": 1024, "scope": "assistant"}])

    with pytest.raises(Exception):
        await gen.wait_for_image(timeout=2, output_dir=str(tmp_path))
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".png")]

    # тот же путь, но содержимое другое — настоящий результат принимается
    gen._upload_digests = {hashlib.sha256(b"other-photo").hexdigest()}
    path = await gen.wait_for_image(timeout=5, output_dir=str(tmp_path))
    assert path and os.path.exists(path)


@pytest.mark.asyncio
async def test_upload_marks_reference_and_hash(tmp_path):
    """После загрузки файла его URL и SHA-256 попадают в список исключений."""
    page = FakePage(download_button=False)
    gen = make_generator(page)
    gen.start_capture(str(tmp_path / ".downloads"))
    gen._captured["image"].append({"url": "https://cdn.arena.ai/uploaded-echo.png", "ts": 0})

    image = tmp_path / "source.png"
    image.write_bytes(PNG_BYTES)

    assert await gen.upload_image(str(image)) is True
    assert hashlib.sha256(PNG_BYTES).hexdigest() in gen._upload_digests
    assert "https://cdn.arena.ai/uploaded-echo.png" in gen._reference_media
    assert all(item["url"] != "https://cdn.arena.ai/uploaded-echo.png" for item in gen._captured["image"])
    assert gen._is_excluded_media("https://cdn.arena.ai/uploaded-echo.png") is True


class EchoPage(FakePage):
    """Как ведёт себя Arena: пока идёт генерация, в ленте висит эхо вложения."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.echo_url = "https://cdn.arena.ai/echo-unknown-url.png"
        self.real_url = "https://cdn.arena.ai/real-edit.png"

    async def evaluate(self, js, arg=None):
        if "isJunk" in js:
            images = []
            if self.submitted:
                # вложение пользователя остаётся в ленте и после генерации
                images.append({"src": self.echo_url, "w": 1024, "h": 1024, "scope": "main"})
            if self.submitted and not self.generating:
                images.append({"src": self.real_url, "w": 1024, "h": 1024, "scope": "assistant"})
            return {"images": images, "stop": self.generating, "spinning": self.generating,
                    "generating": self.generating, "actionBar": self.submitted and not self.generating,
                    "errorText": "", "assistantFound": True, "url": self._url}
        return await super().evaluate(js, arg)


class EchoRequest(FakeRequest):
    """Эхо отдаётся байт-в-байт как исходное фото, настоящий результат — другим файлом."""

    async def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        body = PNG_BYTES * 100 if "echo" in url else PNG_BYTES * 100 + b"\\x89PNG\\r\\n\\x1a\\n"
        return FakeResponse(url, body, "image/png")


@pytest.mark.asyncio
async def test_full_edit_flow_returns_real_result_not_echo(tmp_path):
    """Сквозной сценарий: эхо вложения в ленте + «Stop generation» -> отдаём настоящий результат."""
    page = EchoPage(download_button=False)
    page.request = EchoRequest(page)          # до make_generator: context берёт page.request
    gen = make_generator(page)
    gen.start_capture(str(tmp_path / ".downloads"))

    source = tmp_path / "in" / "source.png"   # исходник отдельно от папки результатов
    source.parent.mkdir(exist_ok=True)
    source.write_bytes(PNG_BYTES * 100)       # ровно те же байты, что отдаёт эхо-ссылка
    gen._last_upload_path = str(source)
    await gen._mark_upload_reference()        # то, что делает upload_image() после загрузки
    assert hashlib.sha256(PNG_BYTES * 100).hexdigest() in gen._upload_digests

    page.submitted = True
    page.generating = True
    task = asyncio.ensure_future(gen.wait_for_image(timeout=15, output_dir=str(tmp_path)))
    await asyncio.sleep(1.2)                  # несколько поллов, пока на странице Stop

    saved = [f for f in os.listdir(tmp_path) if f.endswith(".png")]
    assert saved == [], f"эхо вложения не должно сохраняться во время генерации: {saved}"

    page.generating = False                   # генерация завершилась, появился результат
    path = await task
    assert path and os.path.exists(path)
    with open(path, "rb") as stream:
        assert hashlib.sha256(stream.read()).hexdigest() != hashlib.sha256(PNG_BYTES * 100).hexdigest()


@pytest.mark.asyncio
async def test_edit_image_rejects_identical_file(tmp_path):
    """Если вернулось то же фото — edit_image обязан сообщить об ошибке, а не отдать его."""
    page = FakePage(download_button=False)
    gen = make_generator(page)

    async def fake_navigate(url, ui_selector=None, mode=None):
        gen._baseline_images = set()

    async def fake_enter(prompt, mode=None):
        return True

    async def fake_submit(prompt="", mode=None):
        return True

    async def fake_wait(timeout=None, output_dir="generated"):
        return str(image)

    gen._navigate_and_wait = fake_navigate
    gen.enter_prompt = fake_enter
    gen.submit_prompt = fake_submit
    gen.wait_for_image = fake_wait

    image = tmp_path / "source.png"
    image.write_bytes(PNG_BYTES)

    with pytest.raises(Exception) as exc:
        await gen.edit_image(str(image), "make it a figurine", output_dir=str(tmp_path))
    assert "исходное фото" in str(exc.value)
'''


def find_target(directory: Path) -> Path:
    path = directory / "arena_generator.py"
    if not path.is_file():
        sys.exit(f"Не найден {path}. Запусти скрипт в папке проекта или укажи --dir.")
    return path


def apply_patch(text: str) -> tuple:
    """Возвращает (новый_текст, применённые, уже_применённые)."""
    applied, skipped = [], []
    for name, old, new in STEPS:
        if new in text:
            skipped.append(name)
            continue
        count = text.count(old)
        if count != 1:
            sys.exit(f"Не найден ожидаемый фрагмент (шаг «{name}», совпадений: {count}).\n"
                     f"Похоже, файл уже правился вручную. Ничего не изменено.")
        text = text.replace(old, new, 1)
        applied.append(name)
    return text, applied, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default=None, help="папка проекта (по умолчанию — рядом со скриптом)")
    parser.add_argument("--check", action="store_true", help="только показать, что будет изменено")
    parser.add_argument("--restore", action="store_true", help="вернуть arena_generator.py из .bak")
    parser.add_argument("--no-tests", action="store_true", help="не класть регресс-тест в tests/")
    args = parser.parse_args()

    directory = Path(args.dir).expanduser().resolve() if args.dir else Path(__file__).resolve().parent
    target = find_target(directory)
    backup = target.with_suffix(".py.bak")

    if args.restore:
        if not backup.is_file():
            sys.exit(f"Резервная копия не найдена: {backup}")
        shutil.copy2(backup, target)
        print(f"Восстановлено из {backup.name}")
        return 0

    original = target.read_text(encoding="utf-8")
    patched, applied, skipped = apply_patch(original)

    print(f"Файл: {target}")
    for name in applied:
        print(f"  + {name}")
    for name in skipped:
        print(f"  = уже применено: {name}")

    if args.check:
        print("\n--check: файл не изменён")
        return 0

    if not applied:
        print("\nНечего делать: все правки уже на месте.")
    else:
        # сначала проверяем, что результат компилируется
        with tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8", delete=False) as tmp:
            tmp.write(patched)
            tmp_name = tmp.name
        try:
            py_compile.compile(tmp_name, doraise=True)
        except py_compile.PyCompileError as exc:
            Path(tmp_name).unlink(missing_ok=True)
            sys.exit(f"Патч не применён: результат не компилируется.\n{exc}")
        Path(tmp_name).unlink(missing_ok=True)

        if not backup.is_file():
            shutil.copy2(target, backup)
            print(f"\nРезервная копия: {backup.name}")
        target.write_text(patched, encoding="utf-8")
        print(f"Готово: правок применено {len(applied)}.")

    if not args.no_tests and (directory / "tests").is_dir():
        test_path = directory / "tests" / "test_edit_echo_fix.py"
        if test_path.is_file() and "Регресс-тесты: в режиме редактирования" not in test_path.read_text(encoding="utf-8"):
            print(f"  ! {test_path.name} уже существует и создан не этим скриптом — не перезаписываю")
        else:
            test_path.write_text(TEST_FILE, encoding="utf-8")
            print(f"Регресс-тест: {test_path}")
            print("Проверка:  python -m pytest -q tests/test_edit_echo_fix.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
