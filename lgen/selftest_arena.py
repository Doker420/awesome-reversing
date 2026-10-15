#!/usr/bin/env python3
"""
selftest_arena.py — офлайн-проверка всего конвейера генерации БЕЗ arena.ai.

Поднимает локальный «мок» arena.ai (та же разметка, что у настоящего:
модалка Terms of Use, textarea с placeholder, кнопка Send message с aria-label,
кнопка Stop generation, карточка результата с иконкой скачивания и кнопкой Edit)
и прогоняет через него реальный ArenaGenerator с настоящим Chromium.

Зачем: убедиться, что механика (согласие → ввод → отправка → перехват результата →
скачивание) работает, не тратя токены и не упираясь в капчу.

Запуск (в контейнере или локально, где стоит Playwright):

    python selftest_arena.py            # все режимы
    python selftest_arena.py image      # только картинка
    python selftest_arena.py video      # только видео
    python selftest_arena.py edit       # редактирование картинки
    python selftest_arena.py animate    # оживление картинки

Код выхода 0 — всё ок, 1 — есть падения.
"""
import asyncio
import http.server
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


# ─────────────────────────────────────────────────────────────
# Генерируем настоящий PNG (512x512), чтобы сработал фильтр «>200px»
# ─────────────────────────────────────────────────────────────
def make_png(size: int = 512, rgb=(90, 40, 200)) -> bytes:
    raw = b""
    for y in range(size):
        raw += b"\x00" + bytes(rgb) * size
    compressed = zlib.compress(raw, 6)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", compressed) + chunk(b"IEND", b""))


PNG = make_png()
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200_000  # «достаточно большое видео»
# Каждый результат — свои байты (иначе дедупликация по содержимому отсеет второй вариант)
def mp4_for(name: str) -> bytes:
    return MP4 + name.encode()
# Промо-ролик самой Arena, который лежит на странице /video ДО отправки.
# Бот не должен принять его за результат (регрессия из боевого лога).
CTA_MP4 = MP4 + b"promo-cta"

# ─────────────────────────────────────────────────────────────
# HTML-мок: повторяет боевые особенности arena.ai
# ─────────────────────────────────────────────────────────────
PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Arena mock</title>
<style>
  body {{ font-family: sans-serif; margin: 0; padding: 40px; }}
  #composer {{ max-width: 640px; margin: 0 auto; }}
  textarea {{ width: 100%; height: 90px; font-size: 15px; padding: 10px; }}
  #result {{ max-width: 640px; margin: 24px auto; }}
  .card {{ display: flex; gap: 8px; align-items: center; }}
  button {{ cursor: pointer; }}
  video, img.result {{ width: 320px; height: 320px; object-fit: cover; }}
  ol {{ list-style: none; padding: 0; }}
  .justify-end {{ color: #666; }}
</style></head>
<body data-mode="{mode}">
<video class="promo" src="/videos/cta/agents-cta.mp4" muted autoplay loop></video>
<ol id="chat"></ol>

<div id="composer">
  <form onsubmit="return false;">
    <textarea placeholder="{placeholder}" id="ta"></textarea>
    <div style="display:flex; gap:8px; margin-top:8px;">
      <button type="button" id="addfiles" aria-label="Add files">Add files</button>
      <input type="file" id="file" style="display:none">
      <button type="button" id="send" aria-label="Send message" disabled>&#8594;</button>
    </div>
  </form>
</div>

<div id="result"></div>

<div id="terms" role="dialog" aria-modal="true" style="position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;">
  <div style="background:#fff;max-width:520px;margin:12vh auto;padding:24px;border-radius:12px;">
    <h3>Terms of Use &amp; Privacy Policy</h3>
    <p>By clicking &ldquo;Agree&rdquo;, you agree to be bound by our Terms of Use and acknowledge
       receipt and understanding of our Privacy Policy.</p>
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <small>Or hit <b>Enter</b> on your keyboard to agree</small>
      <button type="button" id="agree">Agree</button>
    </div>
  </div>
</div>

<script>
const MODE = document.body.dataset.mode;
const ta = document.getElementById('ta');
const send = document.getElementById('send');
const terms = document.getElementById('terms');
const chat = document.getElementById('chat');
const result = document.getElementById('result');
let gate = !document.cookie.includes('cookie-preferences');
let generating = false;

function showGate(v) {{
  gate = v;
  terms.style.display = v ? 'block' : 'none';
}}
showGate(gate);

function agree() {{
  document.cookie = 'cookie-preferences=1; path=/';
  showGate(false);
  setEnabled();   // после согласия composer снова активен
}}
document.getElementById('agree').onclick = agree;

function setEnabled() {{ send.disabled = !ta.value.trim() || gate; }}
ta.addEventListener('input', setEnabled);
setEnabled();

ta.addEventListener('keydown', (e) => {{
  if (e.key !== 'Enter') return;
  e.preventDefault();
  if (gate) {{ agree(); return; }}   // боевое поведение: модалка «съедает» Enter
  submit();
}});

send.onclick = () => {{ if (!gate) submit(); }};

document.getElementById('addfiles').onclick = () => document.getElementById('file').click();
document.getElementById('file').onchange = () => {{
  const name = document.getElementById('file').files[0]?.name || 'file';
  const li = document.createElement('li');
  li.className = 'justify-end';
  li.textContent = 'attachment: ' + name;
  chat.appendChild(li);
}};

function submit() {{
  const text = ta.value.trim();
  if (!text || generating || gate) return;
  ta.value = '';
  setEnabled();

  const userMsg = document.createElement('li');
  userMsg.className = 'justify-end';
  userMsg.innerHTML = '<div data-role="user">' + text + '</div>';
  chat.appendChild(userMsg);

  generating = true;
  const li = document.createElement('li');
  li.innerHTML = '<div><span class="animate-spin">◌</span> Generating… ' +
                 '<button aria-label="Stop generation">Stop</button></div>';
  chat.appendChild(li);

  setTimeout(() => done(li), 1200);
}}

function downloadButton(name) {{
  const b = document.createElement('button');
  b.setAttribute('aria-label', 'Download');
  b.textContent = '⬇';
  b.onclick = () => {{
    const a = document.createElement('a');
    a.href = '/download/' + name;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }};
  return b;
}}

function done(stopHolder) {{
  generating = false;
  const src = stopHolder.querySelector('button')?.textContent === 'Stop';
  stopHolder.remove();

  const li = document.createElement('li');
  if (MODE === 'video') {{
    for (const n of ['video_1.mp4', 'video_2.mp4']) {{
      const card = document.createElement('div');
      card.className = 'card';
      const v = document.createElement('video');
      v.src = '/assets/' + n;
      v.muted = true;
      card.appendChild(downloadButton(n));
      card.appendChild(v);
      const edit = document.createElement('button');
      edit.textContent = 'Edit';
      card.appendChild(edit);
      li.appendChild(card);
    }}
  }} else {{
    const card = document.createElement('div');
    card.className = 'card';
    const img = document.createElement('img');
    img.className = 'result';
    img.src = '/assets/result.png';
    card.appendChild(downloadButton('result.png'));
    card.appendChild(img);
    const edit = document.createElement('button');
    edit.textContent = 'Edit';
    card.appendChild(edit);
    li.appendChild(card);
  }}
  chat.appendChild(li);
}}
</script>
</body></html>
"""

PLACEHOLDERS = {
    "image": "Describe the image you want to generate...",
    "video": "Describe your video...",
    "text": "Ask anything...",
    "code": "Describe the app you want to build...",
}


class MockHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # тихий режим

    def _send(self, body: bytes, ctype: str, extra: dict = None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/image/direct", "/image", "/video", "/text/direct", "/text", "/code/direct", "/code"):
            mode = "video" if "video" in path else ("image" if "image" in path else ("code" if "code" in path else "text"))
            html = PAGE.format(mode=mode, placeholder=PLACEHOLDERS[mode]).encode("utf-8")
            return self._send(html, "text/html; charset=utf-8")
        if path == "/assets/result.png":
            return self._send(PNG, "image/png")
        if path == "/videos/cta/agents-cta.mp4":
            return self._send(CTA_MP4, "video/mp4")
        if path in ("/assets/video_1.mp4", "/assets/video_2.mp4"):
            return self._send(mp4_for(os.path.basename(path)), "video/mp4")
        if path.startswith("/download/"):
            name = os.path.basename(path)
            body = mp4_for(name) if name.endswith(".mp4") else PNG
            ctype = "video/mp4" if name.endswith(".mp4") else "image/png"
            return self._send(body, ctype, {"Content-Disposition": f'attachment; filename="{name}"'})
        return self._send(b"not found", "text/plain")


def start_mock() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port


# ─────────────────────────────────────────────────────────────
# Прогон настоящего генератора против мока
# ─────────────────────────────────────────────────────────────
async def run_checks(port: int, only: list) -> int:
    import arena_generator as ag

    base = f"http://127.0.0.1:{port}"
    ag.ARENA_URLS.update({
        "image": f"{base}/image/direct",
        "video": f"{base}/video",
        "text": f"{base}/text/direct",
        "code": f"{base}/code/direct",
        "prompt": f"{base}/text/direct",
    })

    workdir = tempfile.mkdtemp(prefix="arena_selftest_")
    src_img = os.path.join(workdir, "source.png")
    with open(src_img, "wb") as f:
        f.write(PNG)

    gen = ag.ArenaGenerator()
    # быстрые тайминги, чтобы тест шёл секунды
    gen.submit_check_seconds = 3
    gen.image_poll_seconds = 0.5
    gen.video_poll_seconds = 0.5
    gen.download_wait_seconds = 6
    gen.download_click_interval = 0.5
    gen.consent_check_interval = 0.5

    results = []

    async def check(name, coro):
        try:
            out = await coro
            files = out if isinstance(out, list) else [out]
            ok = bool(out) and all(os.path.exists(p) and os.path.getsize(p) > 0 for p in files)
            # регрессия: промо-ролик самой Arena не должен попасть в результаты
            promo = [p for p in files if os.path.exists(p) and b"promo-cta" in open(p, "rb").read()]
            if promo:
                ok = False
                print("  ❌ в результатах промо-ролик Arena (agents-cta) — регрессия")
            results.append((name, ok, str(out) if not promo else f"промо-ролик в результатах: {promo}"))
            print(f"  {'✅' if ok else '❌'} {name}: {out}")
        except Exception as e:
            results.append((name, False, repr(e)))
            print(f"  ❌ {name}: {e!r}")

    print(f"\n▶ Мок arena.ai: {base}   (выход: {workdir})\n")
    try:
        await gen.init_browser()
        if not only or "image" in only:
            await check("generate_image", gen.generate_image("neon city at night", output_dir=workdir, timeout=60))
        if not only or "edit" in only:
            await check("edit_image", gen.edit_image(src_img, "add sunglasses", output_dir=workdir, timeout=60))
        if not only or "video" in only:
            await check("generate_video", gen.generate_video("drone over lake", output_dir=workdir, timeout=60))
        if not only or "animate" in only:
            await check("animate_image", gen.animate_image(src_img, "gentle motion", output_dir=workdir, timeout=60))
        if not only or "text" in only:
            await check("generate_text", gen.generate_text("say hello", timeout=30))
    finally:
        try:
            await gen.close_browser()
        except Exception:
            pass
        shutil.rmtree(workdir, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print(f"\nИтог: {len(results) - len(failed)}/{len(results)} проверок пройдено")
    if failed:
        print("Провалились:")
        for name, _, err in failed:
            print(f"  - {name}: {err}")
    return 1 if failed else 0


def main() -> int:
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    port = start_mock()
    return asyncio.run(run_checks(port, only))


if __name__ == "__main__":
    sys.exit(main())
