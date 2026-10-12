#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diagnose_arena.py — пошаговая диагностика отправки промпта на arena.ai.

ЗАЧЕМ
    Бот (bot.py + arena_generator.py) использует aiogram и playwright-stealth.
    Этот скрипт — «голый» прогон: только Playwright, БЕЗ бота и БЕЗ stealth,
    с подробным выводом состояния страницы после каждого шага. Запустите его,
    когда «промпт не ушёл», и пришлите REPORT + папку diag_out/.

ЧТО ПРОВЕРЯЕТ
    0. окружение: python/playwright/браузер, файл cookies и срок их жизни
    1. открытие страницы режима: URL, редиректы, логин-стена, капча
    2. все модалки (Terms of Use / cookies) с текстом и признаком position:fixed
    3. все <textarea> и все кнопки composer: aria-label, disabled, svg
    4. что ПЕРЕКРЫВАЕТ поле ввода (elementFromPoint) — частая причина
    5. ввод промпта: что реально оказалось в поле (fill / type / js)
    6. Enter → состояние через 1/3/6/10 с; не ушло → клик по кнопке Send
    7. сеть: POST /nextjs-api/stream (статус + первые байты ответа), медиа-ответы
    8. скриншоты + HTML + дампы в diag_out/, итоговый REPORT

ЗАПУСК (на сервере, где стоит бот)
    python diagnose_arena.py --mode video   --prompt "drone over a mountain lake"
    python diagnose_arena.py --mode image   --prompt "neon city at night"
    python diagnose_arena.py --mode animate --image photo.jpg --prompt "gentle motion"
    python diagnose_arena.py --mode edit    --image photo.jpg --prompt "add sunglasses"
    python diagnose_arena.py --mode text    --prompt "say hello"
    python diagnose_arena.py --url https://arena.ai/video --prompt "..." --headed

    --headed        показать окно браузера (нужен X/рабочий стол)
    --no-cookies    не подгружать cookies/arena_cookies.json
    --wait 180      сколько секунд слушать сеть после отправки (видео — 300+)
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime

try:
    import playwright
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    print("❌ Playwright не установлен. Выполните:\n"
          "   pip install playwright && playwright install chromium")
    sys.exit(2)

MODES = {
    "image":   "https://arena.ai/image/direct?model_a=max",
    "video":   "https://arena.ai/video",
    "text":    "https://arena.ai/text/direct?model_a=max",
    "prompt":  "https://arena.ai/text/direct?model_a=max",
    "code":    "https://arena.ai/code/direct?model_a=max",
    "animate": "https://arena.ai/video",
    "edit":    "https://arena.ai/image/direct?model_a=max",
}

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

OUT_DIR = "diag_out"
_lines = []          # всё, что печатаем — дублируем в report.txt
_results = []        # (этап, ok, пояснение)
_net = {"stream_req": [], "stream_resp": [], "media": [], "other": [],
        "armed": False,          # True после нажатия отправки — только тогда медиа считаем результатом
        "stream_status": None, "stream_done": False, "stream_body": "",
        "stream_error": "", "stream_timeout": 600}
# статические картинки/видео сайта (аватарки, CTA, favicon, reCAPTCHA) — не результат генерации
STATIC_MEDIA_RE = re.compile(r"(/images/|/videos/cta/|favicon|apple-touch|gstatic\.com|googleusercontent|recaptcha)", re.I)
_console = []
_page_errors = []


def say(text=""):
    print(text, flush=True)
    _lines.append(text)


def stage(name, ok, detail=""):
    icon = "✅" if ok is True else ("⚠️" if ok is None else "❌")
    _results.append((name, ok, detail))
    say(f"{icon} {name}" + (f" — {detail}" if detail else ""))


def hr(title=""):
    say("")
    say("─" * 72)
    if title:
        say(f"▶ {title}")
        say("─" * 72)


# ─────────────────────────────────────────────────────────────
# JS-дамп состояния страницы
# ─────────────────────────────────────────────────────────────
DUMP_JS = r"""
() => {
    const vis = (e) => {
        if (!e) return false;
        const cs = getComputedStyle(e);
        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) return false;
        const r = e.getBoundingClientRect();
        return r.width > 1 && r.height > 1;
    };
    const rect = (e) => { const r = e.getBoundingClientRect();
        return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]; };
    const txt = (e, n) => ((e && (e.innerText || e.textContent || '')) || '')
        .replace(/\s+/g, ' ').trim().slice(0, n || 200);
    const body = document.body ? (document.body.innerText || '') : '';

    const dialogs = Array.from(document.querySelectorAll(
        "[role='dialog'],[role='alertdialog'],[aria-modal='true'],[data-state='open']"))
        .filter(vis)
        .map(d => {
            const cs = getComputedStyle(d);
            return { text: txt(d, 400), position: cs.position, rect: rect(d) };
        });

    const textareas = Array.from(document.querySelectorAll('textarea')).map((t, i) => {
        const cs = getComputedStyle(t);
        let top = 'не проверено';
        try {
            const r = t.getBoundingClientRect();
            const el = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
            if (!el) top = 'null';
            else if (el === t) top = 'САМ TEXTAREA ✔';
            else top = el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + ' ← ПЕРЕКРЫВАЕТ';
        } catch (e) { top = 'ошибка проверки'; }
        return {
            i,
            placeholder: t.placeholder || '',
            aria: t.getAttribute('aria-label') || '',
            visible: vis(t),
            disabled: !!t.disabled,
            readOnly: !!t.readOnly,
            valueLen: (t.value || '').length,
            valueHead: (t.value || '').slice(0, 60),
            position: cs.position,
            rect: rect(t),
            topElement: top,
            outer: (t.outerHTML || '').slice(0, 300),
        };
    });

    const allButtons = Array.from(document.querySelectorAll('button,[role="button"]')).filter(vis);
    const buttons = allButtons.slice(0, 60).map(b => ({
        text: txt(b, 32),
        aria: b.getAttribute('aria-label') || '',
        title: b.getAttribute('title') || '',
        disabled: !!b.disabled || b.getAttribute('aria-disabled') === 'true',
        hasSvg: !!b.querySelector('svg'),
        rect: rect(b),
    }));

    let composer = null;
    const ta = document.querySelector('textarea');
    if (ta) {
        const host = ta.closest('form') || ta.closest('div') || document;
        const send = host.querySelector("button[aria-label='Send message']")
            || host.querySelector("button[type='submit']")
            || Array.from(host.querySelectorAll('button')).filter(b => !!b.querySelector('svg')).pop();
        composer = {
            sendFound: !!send,
            sendLabel: send ? (send.getAttribute('aria-label') || txt(send, 20) || '(без подписи)') : '',
            sendDisabled: send ? (!!send.disabled || send.getAttribute('aria-disabled') === 'true') : null,
            sendDisabledAttr: send ? send.hasAttribute('disabled') : null,
            textareaDisabled: !!ta.disabled,
            textareaReadOnly: !!ta.readOnly,
            buttonsInComposer: Array.from(host.querySelectorAll('button')).map(b => ({
                text: txt(b, 24), aria: b.getAttribute('aria-label') || '', disabled: !!b.disabled,
            })).slice(0, 12),
        };
    }

    return {
        url: location.href,
        title: document.title,
        readyState: document.readyState,
        dialogs,
        counts: {
            dialogsVisible: dialogs.length,
            textareas: textareas.length,
            buttonsVisible: allButtons.length,
            images: document.querySelectorAll('img').length,
            videos: document.querySelectorAll('video').length,
        },
        gateEnterText: /hit enter on your keyboard to agree/i.test(body),
        termsText: Array.from(document.querySelectorAll('body *')).some(e => {
            if (getComputedStyle(e).position !== 'fixed') return false;
            const r = e.getBoundingClientRect();
            const t = e.innerText || '';
            return r.width > 1 && r.height > 1 && /terms of use/i.test(t) && /agree/i.test(t);
        }),
        downloadButtons: document.querySelectorAll("button[aria-label='Download']").length,
        cookieText: /manage cookie preferences|accept cookies|this website uses cookies/i.test(body),
        loginWall: /(sign in to continue|continue with google|continue with email)/i.test(body.slice(0, 4000)),
        captcha: !!document.querySelector("iframe[src*='recaptcha'],iframe[src*='challenges.cloudflare.com'],#cf-wrapper,.cf-turnstile"),
        captchaText: /(security verification|verifying you are human|just a moment|checking your browser)/i.test(body),
        stopButton: !!document.querySelector("button[aria-label='Stop generation']"),
        generating: /(generating|creating|rendering|queued|uploading)/i.test(body),
        textareas,
        buttons,
        composer,
        bodyHead: body.replace(/\s+/g, ' ').trim().slice(0, 1000),
        localStorageKeys: Object.keys(window.localStorage || {}).slice(0, 15),
        cookieNames: document.cookie.split(';').map(c => c.trim().split('=')[0]).filter(Boolean).slice(0, 25),
    };
}
"""

STATE_JS = r"""
() => {
    const body = document.body ? (document.body.innerText || '') : '';
    const ta = document.querySelector('textarea');
    return {
        url: location.href,
        textareaValueLen: ta ? (ta.value || '').length : -1,
        textareaDisabled: ta ? !!ta.disabled : null,
        stop: !!document.querySelector("button[aria-label='Stop generation']"),
        generating: /(generating|creating|rendering|queued)/i.test(body),
        dialogsVisible: Array.from(document.querySelectorAll("[role='dialog'],[aria-modal='true']"))
            .filter(e => { const cs = getComputedStyle(e); const r = e.getBoundingClientRect();
                return cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 1; }).length,
    };
}
"""


async def _count_downloads(page):
    try:
        return await page.evaluate("() => document.querySelectorAll(\"button[aria-label='Download']\").length")
    except Exception:
        return -1


def dump_lines(d, prefix=""):
    """Печатаем дамп компактно, но со всеми деталями."""
    say(f"{prefix}URL: {d.get('url')}")
    say(f"{prefix}title: {d.get('title')!r}  readyState={d.get('readyState')}")
    say(f"{prefix}элементов: {d.get('counts')}")
    say(f"{prefix}признаки: Terms={d.get('termsText')} cookies={d.get('cookieText')} "
        f"gate('hit Enter')={d.get('gateEnterText')} логин-стена={d.get('loginWall')} "
        f"капча={d.get('captcha')}/{d.get('captchaText')} Stop={d.get('stopButton')} "
        f"generating={d.get('generating')}")

    dialogs = d.get("dialogs") or []
    if dialogs:
        say(f"{prefix}МОДАЛКИ ({len(dialogs)}):")
        for i, dlg in enumerate(dialogs):
            say(f"{prefix}  [{i}] position={dlg.get('position')} rect={dlg.get('rect')}")
            say(f"{prefix}      текст: {dlg.get('text')[:400]!r}")
    else:
        say(f"{prefix}МОДАЛКИ: не найдено")

    tas = d.get("textareas") or []
    if tas:
        say(f"{prefix}TEXTAREA ({len(tas)}):")
        for t in tas:
            say(f"{prefix}  [{t['i']}] placeholder={t['placeholder']!r} aria={t['aria']!r} "
                f"visible={t['visible']} disabled={t['disabled']} readOnly={t['readOnly']} "
                f"position={t['position']} rect={t['rect']}")
            say(f"{prefix}      value(len={t['valueLen']})={t['valueHead']!r}")
            say(f"{prefix}      под курсором центра: {t['topElement']}")
    else:
        say(f"{prefix}TEXTAREA: НЕ НАЙДЕНО ❌")

    comp = d.get("composer")
    if comp:
        say(f"{prefix}COMPOSER: sendFound={comp.get('sendFound')} label={comp.get('sendLabel')!r} "
            f"sendDisabled={comp.get('sendDisabled')} (атрибут disabled={comp.get('sendDisabledAttr')})")
        say(f"{prefix}      textarea disabled={comp.get('textareaDisabled')} readOnly={comp.get('textareaReadOnly')}")
        for b in comp.get("buttonsInComposer") or []:
            say(f"{prefix}      кнопка: {b}")

    say(f"{prefix}ВСЕ ВИДИМЫЕ КНОПКИ ({len(d.get('buttons') or [])}):")
    for b in (d.get("buttons") or [])[:30]:
        say(f"{prefix}  · {b}")
    say(f"{prefix}localStorage keys: {d.get('localStorageKeys')}")
    say(f"{prefix}cookie names: {d.get('cookieNames')}")
    say(f"{prefix}bodyHead: {d.get('bodyHead')[:600]!r}")


def save(name, content):
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, name)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode if mode == "wb" else "w", encoding=None if mode == "wb" else "utf-8") as f:
        f.write(content)
    return path


async def snap(page, tag, full=True):
    """Скриншот + (опционально) дамп состояния."""
    try:
        await page.screenshot(path=os.path.join(OUT_DIR, f"{tag}.png"), full_page=full)
        say(f"   📷 diag_out/{tag}.png")
    except Exception as e:
        say(f"   ⚠️ скриншот {tag} не получился: {e}")
    if full:
        try:
            d = await page.evaluate(DUMP_JS)
            save(f"{tag}.json", json.dumps(d, ensure_ascii=False, indent=2))
            save(f"{tag}.dump.txt", "\n".join(f"{k}: {v}" for k, v in d.items()))
            return d
        except Exception as e:
            say(f"   ⚠️ дамп {tag} не получился: {e}")
    return None


# ─────────────────────────────────────────────────────────────
# Работа с cookies без Playwright
# ─────────────────────────────────────────────────────────────
def report_cookies(path):
    hr("0. Cookies")
    if not os.path.exists(path):
        stage("cookies: файл найден", False, f"{path} отсутствует — сессия Arena не подгрузится")
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cookies = data if isinstance(data, list) else data.get("cookies", [])
    except Exception as e:
        stage("cookies: чтение", False, repr(e))
        return False

    now = time.time()
    expired, soon, alive = [], [], []
    for c in cookies:
        exp = c.get("expires", c.get("expirationDate", -1))
        name = c.get("name", "?")
        if exp in (-1, None):
            alive.append(name)
        elif exp < now:
            expired.append(name)
        elif exp - now < 3 * 86400:
            soon.append(name)
        else:
            alive.append(name)

    names = [c.get("name", "") for c in cookies]
    # «важные» — только то, что отвечает за авторизацию
    interesting = [n for n in alive if re.search(r"session|cf_clearance|token|auth|__Secure", n, re.I)]
    consent = [n for n in names if "cookie-pref" in n.lower()]
    say(f"  всего cookies: {len(cookies)}")
    say(f"  живых: {len(alive)} | истекают <3 дней: {len(soon)} | просроченных: {len(expired)}")
    say(f"  авторизация (session/cf_clearance/token): {interesting or 'НЕ НАЙДЕНЫ ⚠️'}")
    say(f"  согласие (cookie-preferences): {consent or 'нет — при первом заходе будет модалка Terms of Use'}")
    if expired:
        say(f"  просроченные: {expired[:10]}")
    if soon:
        say(f"  скоро истекут: {soon[:10]}")

    ok = bool(interesting)
    stage("cookies: куки авторизации на месте", ok,
          ("есть " + ", ".join(interesting[:5])) if ok
          else "нет session/cf_clearance — почти наверняка логин-стена и composer disabled")
    return True


# ─────────────────────────────────────────────────────────────
# Основной прогон
# ─────────────────────────────────────────────────────────────
async def run(args):
    url = args.url or MODES.get(args.mode, MODES["image"])
    hr("0. Окружение")
    say(f"  python      : {sys.version.split()[0]}")
    try:
        from importlib.metadata import version as _pkg_version
        _pw_ver = _pkg_version("playwright")
    except Exception:
        _pw_ver = "?"
    say(f"  playwright  : {_pw_ver}")
    say(f"  stealth     : НЕ используется (как вы и просили)")
    say(f"  режим/URL   : {args.mode} → {url}")
    say(f"  headless    : {not args.headed}")
    say(f"  промпт      : {args.prompt!r}")
    if args.image:
        say(f"  вложение    : {args.image} (существует: {os.path.exists(args.image)})")
    cookies_path = args.cookies
    if not args.no_cookies:
        report_cookies(cookies_path)
    else:
        stage("cookies: пропущены (--no-cookies)", None)

    os.makedirs(OUT_DIR, exist_ok=True)

    async with async_playwright() as pw:
        hr("1. Запуск браузера")
        try:
            browser = await pw.chromium.launch(
                headless=not args.headed,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled", "--window-size=1920,1080"],
            )
            say(f"  браузер: {browser.version}")
            stage("браузер запущен", True, browser.version)
        except Exception as e:
            msg = str(e)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                stage("браузер запущен", False, "нет бинарника Chromium — выполните: playwright install chromium")
            else:
                stage("браузер запущен", False, msg.splitlines()[0] if msg else repr(e))
            return

        ctx_args = dict(viewport={"width": 1920, "height": 1080}, user_agent=UA,
                        locale="en-US", ignore_https_errors=True, accept_downloads=True)
        context = await browser.new_context(**ctx_args)

        if not args.no_cookies and os.path.exists(cookies_path):
            try:
                with open(cookies_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cookies = data if isinstance(data, list) else data.get("cookies", [])
                for c in cookies:
                    if c.get("sameSite") not in ("Strict", "Lax", "None"):
                        c["sameSite"] = "Lax"
                await context.add_cookies(cookies)
                stage("cookies загружены в браузер", True, f"{len(cookies)} шт.")
            except Exception as e:
                stage("cookies загружены в браузер", False, repr(e))

        page = await context.new_page()

        # ── сбор сети/консоли ────────────────────────────────
        page.on("console", lambda m: _console.append(f"[{m.type}] {m.text[:200]}"))
        page.on("pageerror", lambda e: _page_errors.append(str(e)[:300]))

        def on_request(req):
            if "/nextjs-api/stream" in req.url:
                _net["stream_req"].append({"method": req.method, "url": req.url,
                                           "post_len": len(req.post_data or "")})
                say(f"   🌐 ЗАПРОС {req.method} {req.url} (body {len(req.post_data or '')} симв.)")

        async def _read_stream(resp, u):
            try:
                body = await asyncio.wait_for(resp.text(), timeout=_net["stream_timeout"])
            except Exception as e:
                body = ""
                _net["stream_error"] = repr(e)[:200]
            _net["stream_done"] = True
            _net["stream_body"] = body
            say("   🌐 поток SSE завершён" if body else "   🌐 поток SSE не завершился за время ожидания")
            for line in body.splitlines():
                if line.startswith(("a2:", "a3:", "ae:")):
                    say(f"      {line[:300]}")

        async def on_response(resp):
            try:
                u = resp.url
                ct = (resp.headers or {}).get("content-type", "")
                if "/nextjs-api/stream" in u:
                    _net["stream_status"] = resp.status
                    say(f"   🌐 ОТВЕТ {resp.status} {u}")
                    asyncio.ensure_future(_read_stream(resp, u))
                elif ct.startswith(("image/", "video/")):
                    static = STATIC_MEDIA_RE.search(u) is not None
                    size = (resp.headers or {}).get("content-length", "?")
                    if static:
                        return
                    if _net["armed"]:
                        _net["media"].append({"url": u, "ct": ct, "len": size})
                        say(f"   🖼 медиа-ответ ПОСЛЕ отправки {ct} {u[:120]}")
                    else:
                        say(f"   · медиа до отправки (игнор) {ct} {u[:80]}")
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

        # ── 1. открытие страницы ─────────────────────────────
        hr("1. Открытие страницы")
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            say(f"  HTTP статус документа: {resp.status if resp else '?'}")
            if resp is not None and resp.status >= 400:
                say("  ⚠️ документ ответил кодом ≥400 — проверьте 01_loaded.png: если UI Arena виден, это не блокировка")
            say(f"  URL после перехода    : {page.url}")
            if page.url.rstrip("/") in ("https://arena.ai", "https://arena.ai/"):
                say("  ⚠️ Arena редиректнула на battle-страницу (обычно так бывает при пустой сессии)")
            stage("страница открыта", True, page.url)
        except Exception as e:
            stage("страница открыта", False, repr(e))
            await browser.close()
            return

        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(2)

        d = await snap(page, "01_loaded")
        if d:
            dump_lines(d, "  ")
            if d.get("termsText") or d.get("gateEnterText") or d.get("cookieText"):
                stage("модалка согласия видна", True,
                      "terms=%s cookies=%s gate=%s" % (d.get("termsText"), d.get("cookieText"), d.get("gateEnterText")))
            else:
                stage("модалка согласия", None, "не видна — composer должен быть доступен сразу")
            stage("логин-стена", not d.get("loginWall"),
                  "похоже, сессия протухла — нужны свежие cookies" if d.get("loginWall") else "нет")
            stage("капча/Cloudflare", not (d.get("captcha") or d.get("captchaText")), "не найдено")
            ta = next((t for t in (d.get("textareas") or []) if t["visible"]), None)
            stage("поле ввода найдено", bool(ta),
                  f"placeholder={ta['placeholder']!r}, disabled={ta['disabled']}, под курсором: {ta['topElement']}" if ta else "нет видимых textarea")
            comp = d.get("composer") or {}
            say(f"  Send до ввода промпта: disabled={comp.get('sendDisabled')} "
                f"(это норма — поле пустое; проверяем после ввода на шаге 4)")
            blocked = [t["topElement"] for t in (d.get("textareas") or []) if "ПЕРЕКРЫВАЕТ" in str(t.get("topElement"))]
            if blocked:
                stage("поле ввода не перекрыто", False, f"перекрывает: {blocked[0]}")

        # ── 2. закрываем согласие (как это делает бот) ───────
        hr("2. Закрытие модалок согласия")
        try:
            clicked = await page.evaluate(
                r"""() => {
                    const vis = (e) => { if (!e) return false;
                        const cs = getComputedStyle(e);
                        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) return false;
                        const r = e.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
                    const want = ['agree', 'i agree', 'i understand', 'accept cookies', 'accept all cookies', 'got it', 'save preferences'];
                    const hits = [];
                    for (const b of Array.from(document.querySelectorAll('button,[role="button"]'))) {
                        if (!vis(b) || b.disabled) continue;
                        const t = ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '') + ' ' +
                                   (b.getAttribute('title') || '')).trim().toLowerCase();
                        if (want.some(w => t === w || t.startsWith(w))) { try { b.click(); hits.push(t.slice(0, 30)); } catch (e) {} }
                    }
                    return hits;
                }"""
            )
            say(f"  нажато кнопок согласия: {clicked}")
            await asyncio.sleep(2)
            if await page.evaluate("() => /hit enter on your keyboard to agree/i.test(document.body.innerText || '')"):
                say("  текст «hit Enter to agree» ещё виден → жмём Enter")
                await page.keyboard.press("Enter")
                await asyncio.sleep(2)
            d2 = await page.evaluate(DUMP_JS)
            gate = (d2.get("termsText") or d2.get("cookieText")) and (d2.get("counts", {}).get("dialogsVisible", 0) > 0)
            stage("модалки закрыты", not gate,
                  f"видимых диалогов: {d2.get('counts', {}).get('dialogsVisible')}, "
                  f"terms={d2.get('termsText')} cookies={d2.get('cookieText')}")
            await snap(page, "02_after_consent")
        except Exception as e:
            stage("модалки закрыты", False, repr(e))

        # ── 3. вложение ──────────────────────────────────────
        if args.image:
            hr("3. Прикрепление изображения")
            if not os.path.exists(args.image):
                stage("вложение", False, f"файл не найден: {args.image}")
            else:
                try:
                    done = False
                    try:
                        btn = page.get_by_role("button", name=re.compile(r"^Add files?$", re.I)).first
                        if await btn.count():
                            async with page.expect_file_chooser(timeout=8000) as fc:
                                await btn.click()
                            chooser = await fc.value
                            await chooser.set_files(args.image)
                            done = True
                            say("  загружено через кнопку «Add files»")
                    except Exception as e:
                        say(f"  через кнопку не вышло: {e}")
                    if not done:
                        fi = await page.query_selector('input[type="file"]')
                        if fi:
                            await fi.set_input_files(args.image)
                            done = True
                            say("  загружено через input[type=file]")
                    stage("вложение загружено", done, "" if done else "не нашли ни «Add files», ни input[type=file]")
                    await asyncio.sleep(3)
                    d3 = await page.evaluate(DUMP_JS)
                    comp = d3.get("composer") or {}
                    say(f"  после загрузки: sendDisabled={comp.get('sendDisabled')} "
                        f"textareaDisabled={comp.get('textareaDisabled')}")
                    stage("composer готов после вложения", comp.get("sendDisabled") is False,
                          f"sendDisabled={comp.get('sendDisabled')} (если True — Arena ещё обрабатывает файл)")
                    await snap(page, "03_uploaded")
                except Exception as e:
                    stage("вложение загружено", False, repr(e))

        # ── 4. ввод промпта ──────────────────────────────────
        hr("4. Ввод промпта")
        prompt = args.prompt
        try:
            editor = None
            for sel in ("textarea[placeholder*='Describe your video']",
                        "textarea[placeholder^='Describe the image']",
                        "textarea[placeholder*='image' i]", "form textarea", "textarea"):
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    editor = el
                    say(f"  выбран селектор: {sel}")
                    break
            if not editor:
                stage("поле ввода найдено для ввода", False, "ни один селектор не подошёл")
                raise RuntimeError("нет поля ввода")

            method = args.fill_method
            if method in ("auto", "fill"):
                try:
                    await editor.fill(prompt)
                    val = await editor.input_value()
                    say(f"  fill(): в поле {len(val)} симв., совпадает={val.strip() == prompt.strip()}")
                    if val.strip() == prompt.strip():
                        method = "fill"
                except Exception as e:
                    say(f"  fill() не сработал: {e}")
            if method == "type":
                await editor.click()
                await page.keyboard.type(prompt, delay=12)
                say(f"  type(): в поле {len(await editor.input_value())} симв.")
            elif method == "js":
                await page.evaluate(
                    """(t) => { const ta = document.querySelector('textarea');
                        const set = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                        set.call(ta, t); ta.dispatchEvent(new Event('input', { bubbles: true }));
                        ta.dispatchEvent(new Event('change', { bubbles: true })); }""",
                    prompt,
                )
                say(f"  js-setter: в поле {len(await editor.input_value())} симв.")

            await asyncio.sleep(0.5)
            value = await editor.input_value()
            d4 = await page.evaluate(DUMP_JS)
            stage("промпт записан в поле", value.strip() == prompt.strip(),
                  f"в поле {len(value)} симв. (ожидалось {len(prompt)})")
            comp = d4.get("composer") or {}
            stage("кнопка Send стала активной", comp.get("sendDisabled") is False,
                  f"sendDisabled={comp.get('sendDisabled')}")
            say(f"  состояние: {await page.evaluate(STATE_JS)}")
            await snap(page, "04_filled")
        except Exception as e:
            stage("промпт записан в поле", False, repr(e))

        # ── 5. отправка Enter ────────────────────────────────
        hr("5. Отправка: Enter")
        try:
            editor = await page.query_selector("textarea")
            if editor:
                await editor.click()
            _net["armed"] = True
            await page.keyboard.press("Enter")
            sent = False
            t_start = time.time()
            for wait in (1, 3, 6, 10):
                await asyncio.sleep(max(0.1, (t_start + wait) - time.time()))
                st = await page.evaluate(STATE_JS)
                say(f"  +{wait}с: {st}")
                if _net["stream_req"] or st.get("stop") or st.get("generating") or (st.get("textareaValueLen") == 0 and "c/" in st.get("url", "")):
                    sent = True
                    break
            stage("Enter отправил промпт", sent, f"состояние: {st}")
            await snap(page, "05_after_enter")
        except Exception as e:
            stage("Enter отправил промпт", False, repr(e))
            sent = False

        # ── 6. отправка кнопкой ──────────────────────────────
        if not sent:
            hr("6. Отправка: клик по кнопке Send")
            _net["armed"] = True
            try:
                clicked = await page.evaluate(
                    r"""() => {
                        const ta = document.querySelector('textarea');
                        if (!ta) return 'нет textarea';
                        const host = ta.closest('form') || ta.closest('div') || document;
                        const send = host.querySelector("button[aria-label='Send message']")
                            || host.querySelector("button[type='submit']")
                            || Array.from(host.querySelectorAll('button')).filter(b => !!b.querySelector('svg')).pop();
                        if (!send) return 'кнопка Send не найдена';
                        const info = { label: send.getAttribute('aria-label') || '', disabled: !!send.disabled };
                        if (send.disabled) return 'кнопка Send ЗАБЛОКИРОВАНА (disabled) — ' + JSON.stringify(info);
                        send.click();
                        return 'клик выполнен — ' + JSON.stringify(info);
                    }"""
                )
                say(f"  результат клика: {clicked}")
                await asyncio.sleep(4)
                st = await page.evaluate(STATE_JS)
                say(f"  состояние: {st}")
                sent = bool(st.get("stop") or st.get("generating"))
                stage("кнопка Send отправила промпт", sent, f"состояние: {st}")
                await snap(page, "06_after_click")
            except Exception as e:
                stage("кнопка Send отправила промпт", False, repr(e))

        # ── 7. слушаем сеть ──────────────────────────────────
        hr(f"7. Слушаем сеть {args.wait} с")
        for i in range(args.wait):
            await asyncio.sleep(1)
            if i % 15 == 14:
                say(f"  ...{i + 1}с: запросов к stream={len(_net['stream_req'])}, "
                    f"поток завершён={_net['stream_done']}, медиа после отправки={len(_net['media'])}, "
                    f"кнопок Download в DOM={await _count_downloads(page)}")
            if _net["media"] and _net["stream_done"] and i > 10:
                break

        say("")
        say(f"  POST /nextjs-api/stream: {len(_net['stream_req'])} запрос(ов)")
        for r in _net["stream_req"]:
            say(f"    · {r}")
        for r in _net["stream_resp"]:
            say(f"    ← ответ {r['status']}; первые строки:")
            for line in r["head"].splitlines()[:6]:
                say(f"        {line[:200]}")
        say(f"  статус потока: {_net['stream_status']}, завершён: {_net['stream_done']}"
            + (f", ошибка чтения: {_net['stream_error']}" if _net["stream_error"] else ""))
        if _net["stream_body"]:
            say("  первые строки тела потока:")
            for line in _net["stream_body"].splitlines()[:6]:
                say(f"    {line[:200]}")
        say(f"  медиа-ответов ПОСЛЕ отправки (без статики): {len(_net['media'])}")
        for m in _net["media"][:8]:
            say(f"    · {m}")
        dl_count = await _count_downloads(page)
        say(f"  кнопок Download в DOM (карточки результатов): {dl_count}")

        if _console:
            say(f"  консоль браузера (последние {min(len(_console), 15)}):")
            for c in _console[-15:]:
                say(f"    {c}")
        if _page_errors:
            say(f"  ошибки страницы: {_page_errors[:5]}")

        stage("запрос к Arena ушёл", bool(_net["stream_req"]),
              "сеть молчит — промпт не отправлен" if not _net["stream_req"] else "ок")
        body = _net["stream_body"]
        err_lines = [l for l in body.splitlines() if l.startswith(("a3:", "ae:"))]
        if _net["media"] or dl_count:
            stage("Arena вернула результат", True,
                  f"файлов после отправки: {len(_net['media'])}, кнопок Download: {dl_count}")
        elif err_lines:
            stage("Arena вернула результат", False, "ошибка в потоке: " + err_lines[0][:200])
        elif _net["stream_req"] and not _net["stream_done"]:
            stage("Arena вернула результат", None,
                  f"поток ещё открыт через {args.wait} с — генерация идёт или зависла. "
                  f"Повторите с --wait 300 (видео — дольше)")
        else:
            stage("Arena вернула результат", False,
                  "поток завершился без файла и без ошибки — смотрите 07_final.png и тело потока в report.txt")

        # финальный снимок и DOM
        await snap(page, "07_final")
        try:
            html = await page.content()
            save("page.html", html)
            say("   💾 diag_out/page.html (полный HTML страницы)")
        except Exception:
            pass

        await browser.close()

    # ── REPORT ──────────────────────────────────────────────
    hr("REPORT")
    for name, ok, detail in _results:
        icon = "✅" if ok is True else ("⚠️" if ok is None else "❌")
        say(f"{icon} {name}" + (f" — {detail}" if detail else ""))

    bad = [r for r in _results if r[1] is False]
    say("")
    if not bad:
        say("ИТОГ: всё прошло — промпт ушёл, результат получен.")
    else:
        say("ИТОГ: проблема на этапе:")
        for name, _, detail in bad:
            say(f"  ❌ {name} — {detail}")
        say("")
        say("Что это значит:")
        say("  · «модалка согласия видна»/«модалки закрыты» ❌ → Arena держит Terms of Use,")
        say("    composer остаётся disabled. Нужно свежее согласие (кнопка Agree) или новые cookies.")
        say("  · «логин-стена» ❌ → протухла сессия: обновите cookies/arena_cookies.json.")
        say("  · «поле ввода перекрыто» → поверх composer баннер/модалка, Enter до него не доходит.")
        say("  · «кнопка Send стала активной» ❌ → после ввода текста composer остался disabled (сессия/недогрузка UI/вложение ещё грузится).")
        say("  · «Arena вернула результат» ⚠️ → поток ещё открыт: генерация идёт; повторите с --wait 300.")
        say("  · «промпт записан в поле» ❌ → не тот textarea или поле readonly.")
        say("  · «запрос к Arena ушёл» ❌ → сообщение не отправлено (см. пункты выше).")

    say("")
    say(f"Файлы для отправки разработчику: папка {OUT_DIR}/ "
        "(07_final.png, 05_after_enter.png, page.html, report.txt)")


def main():
    ap = argparse.ArgumentParser(description="Диагностика отправки промпта на arena.ai (без бота и без stealth)")
    ap.add_argument("--mode", choices=sorted(MODES.keys()), default="image", help="режим генерации")
    ap.add_argument("--url", default=None, help="свой URL (перебивает --mode)")
    ap.add_argument("--prompt", default="A red apple on a wooden table, studio light", help="текст промпта")
    ap.add_argument("--image", default=None, help="файл изображения (для animate/edit)")
    ap.add_argument("--cookies", default=os.path.join("cookies", "arena_cookies.json"), help="путь к cookies")
    ap.add_argument("--no-cookies", action="store_true", help="не подгружать cookies (чистая сессия)")
    ap.add_argument("--headed", action="store_true", help="показать окно браузера (нужен X-сервер)")
    ap.add_argument("--fill-method", choices=["auto", "fill", "type", "js"], default="auto")
    ap.add_argument("--wait", type=int, default=180, help="сколько секунд слушать сеть после отправки (видео — 300+)")
    args = ap.parse_args()

    t0 = time.time()
    say(f"# diagnose_arena.py — {datetime.now().isoformat(timespec='seconds')}")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        say("\n⛔ Прервано пользователем")
    except Exception:
        say("\n❌ Непредвиденная ошибка:")
        say(traceback.format_exc())
    say(f"\nЗавершено за {time.time() - t0:.1f} с")

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(_lines) + "\n")
    print(f"\n📄 Полный отчёт: {os.path.join(OUT_DIR, 'report.txt')}")


if __name__ == "__main__":
    main()
