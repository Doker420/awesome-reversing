"""Тесты режима «Клон сайта»: проверки адреса, отказы, промпт и лимиты.

Сеть не используется: DNS подменяется фейковым резолвером, HTTP — фейковой сессией.
"""
import asyncio

import pytest

import site_clone
from site_clone import (
    CloneRateLimiter,
    CloneRefused,
    build_clone_prompt,
    build_snapshot,
    detect_sensitive,
    looks_like_js_shell,
    validate_url,
)

PUBLIC = ["93.184.216.34"]


def public_resolver(host):
    return PUBLIC


def private_resolver(host):
    return ["10.0.0.5"]


# ─── validate_url ───────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "file:///etc/passwd",
    "ftp://example.com/",
    "javascript:alert(1)",
    "http://user:pass@example.com/",
    "http://localhost/",
    "http://example.localhost/",
    "http://intranet.local/",
    "http://127.0.0.1/",
    "http://10.0.0.1/",
    "http://[::1]/",
    "http://example.com:8080/",
    "http://example.com:22/",
    "https://example.com/login",
    "https://example.com/account/settings",
    "https://example.com/checkout",
    "https://paypal.example.com/",
    "https://sberbank-clone.example.com/",
    "https://id.example.com/",
    "https://mail.example.com/",
    "",
    "   ",
    "https://" + "a" * 3000 + ".com/",
])
def test_validate_url_refuses(raw):
    with pytest.raises(CloneRefused):
        validate_url(raw, resolver=public_resolver)


def test_validate_url_refuses_domain_resolving_to_private_network():
    with pytest.raises(CloneRefused):
        validate_url("https://example.com/", resolver=private_resolver)


def test_validate_url_refuses_mixed_public_and_private_addresses():
    with pytest.raises(CloneRefused):
        validate_url("https://example.com/", resolver=lambda h: ["93.184.216.34", "192.168.1.1"])


def test_validate_url_accepts_public_site_and_normalizes():
    url = validate_url("example.com/landing?x=1#top", resolver=public_resolver)
    assert url == "https://example.com/landing?x=1"


def test_validate_url_accepts_explicit_https_port_443():
    assert validate_url("https://example.com:443/", resolver=public_resolver) == "https://example.com:443/"


def test_validate_url_does_not_treat_tld_id_as_identity_host():
    # последняя метка домена не проверяется: example.id — обычный домен
    assert validate_url("https://example.id/", resolver=public_resolver) == "https://example.id/"


# ─── detect_sensitive / js shell ────────────────────────────────

def test_password_field_is_refused():
    html = '<form><input type="password" name="p"></form>'
    assert detect_sensitive(html) is not None


def test_card_autocomplete_is_refused():
    html = '<input autocomplete="cc-number">'
    assert detect_sensitive(html) is not None


def test_card_name_field_is_refused():
    html = '<input name="card_number" placeholder="0000">'
    assert detect_sensitive(html) is not None


def test_login_title_with_email_input_is_refused():
    html = '<input type="email" name="e">'
    assert detect_sensitive(html, title="Sign in to Acme") is not None


def test_plain_landing_is_allowed():
    html = (
        "<html><head><title>Fitness Studio</title></head>"
        "<body><h1>Train hard</h1><section>Classes</section>"
        "<a href='/prices'>Prices</a></body></html>"
    )
    assert detect_sensitive(html, title="Fitness Studio") is None


def test_js_shell_is_detected():
    html = '<html><body><div id="root"></div><script src="app.js"></script></body></html>'
    assert looks_like_js_shell(html)


def test_server_rendered_page_is_not_js_shell():
    html = '<html><body><div id="app"><h1>Welcome to our studio</h1><p>' + "text " * 60 + "</p></div></body></html>"
    assert not looks_like_js_shell(html)


# ─── snapshot и промпт ──────────────────────────────────────────

SAMPLE_HTML = (
    "<html><head><title>Acme &amp; Co</title>"
    "<meta name='description' content='Coffee roasters'>"
    "<link rel='stylesheet' href='/s.css'></head>"
    "<body><nav><a href='/'>Home</a><a href='/shop'>Shop</a></nav>"
    "<h1>Fresh beans</h1><h2>Our story</h2>"
    "<section><img src='x.png'><p>Roasted weekly in small batches.</p></section>"
    "<script>var secret = 1;</script></body></html>"
)
SAMPLE_CSS = "body{font-family:'Inter',sans-serif;color:#1a1a1a;background:rgb(250, 248, 240)}"


def test_snapshot_extracts_design_facts_without_scripts():
    snap = build_snapshot("https://acme.example/", SAMPLE_HTML, [SAMPLE_CSS])
    assert snap["title"] == "Acme & Co"
    assert snap["description"] == "Coffee roasters"
    assert "Fresh beans" in snap["headings"]
    assert "Shop" in snap["links"]
    assert "#1a1a1a" in snap["colors"]
    assert "Inter" in snap["fonts"][0]
    assert snap["images"] == 1
    assert "secret" not in snap["text"]


def test_prompt_contains_rules_and_is_bounded():
    snap = build_snapshot("https://acme.example/", SAMPLE_HTML, [SAMPLE_CSS])
    prompt = build_clone_prompt(snap)
    assert "не копируй логотипы" in prompt
    assert "не добавляй формы для паролей" in prompt
    assert "https://acme.example/" in prompt
    assert len(build_clone_prompt(snap, limit=300)) <= 300


def test_prompt_limit_default():
    big = {"url": "https://x.example/", "css": "a" * 50_000, "text": "b" * 5000}
    assert len(build_clone_prompt(big)) <= site_clone.MAX_PROMPT_CHARS


# ─── редиректы (SSRF на каждом шаге) ────────────────────────────

class _FakeContent:
    def __init__(self, body):
        self._body = body

    async def read(self, n):
        return self._body[:n]


class _FakeResp:
    def __init__(self, status, headers, body=b""):
        self.status = status
        self.headers = headers
        self.content = _FakeContent(body)
        self.charset = "utf-8"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.requested = []

    def get(self, url, allow_redirects=False):
        self.requested.append(url)
        status, headers, body = self.routes[url]
        return _FakeResp(status, headers, body)


def test_redirect_to_loopback_is_refused():
    session = _FakeSession({
        "https://example.com/": (302, {"Location": "http://127.0.0.1:8000/admin"}, b""),
    })
    with pytest.raises(CloneRefused):
        asyncio.run(site_clone._get_following_redirects(
            session, "https://example.com/", public_resolver, 1000))
    assert "http://127.0.0.1:8000/admin" not in session.requested


def test_redirect_to_login_page_is_refused():
    session = _FakeSession({
        "https://example.com/": (302, {"Location": "https://example.com/login"}, b""),
    })
    with pytest.raises(CloneRefused):
        asyncio.run(site_clone._get_following_redirects(
            session, "https://example.com/", public_resolver, 1000))


def test_redirect_loop_is_stopped():
    session = _FakeSession({
        "https://example.com/": (302, {"Location": "https://example.com/"}, b""),
    })
    with pytest.raises(CloneRefused):
        asyncio.run(site_clone._get_following_redirects(
            session, "https://example.com/", public_resolver, 1000))


def test_plain_200_returns_body_and_type():
    session = _FakeSession({
        "https://example.com/": (200, {"Content-Type": "text/html; charset=utf-8"}, b"<html></html>"),
    })
    body, ctype, final = asyncio.run(site_clone._get_following_redirects(
        session, "https://example.com/", public_resolver, 1000))
    assert body == "<html></html>"
    assert "text/html" in ctype
    assert final == "https://example.com/"


# ─── лимиты ─────────────────────────────────────────────────────

def test_cooldown_and_daily_limit():
    now = [1_000_000.0]
    limiter = CloneRateLimiter(cooldown=120, daily_limit=2, clock=lambda: now[0])

    assert limiter.check(1) is None
    limiter.record(1)

    assert limiter.check(1) is not None          # cooldown
    now[0] += 121
    assert limiter.check(1) is None
    limiter.record(1)

    now[0] += 121
    assert limiter.check(1) is not None          # суточная квота 2 исчерпана
    assert limiter.check(2) is None              # другой пользователь не затронут

    now[0] += 86400
    assert limiter.check(1) is None              # окно суток сдвинулось
