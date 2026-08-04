"""How the dashboard resolves — and stops trusting — a viewer token.

All three specs here come from one live incident. A host opened the desktop app's dashboard
window and got "Access denied" with a viewer token that was live and unrevoked, and there
was no way to recover from inside that window: it has no address bar, no devtools and no
private mode. Three separate defects had to line up, and each is pinned below.

Driven through ``_request_token`` / ``_unauthorized`` with a fake request rather than a
live server, the way the rest of ``tests/test_api.py`` drives the ``_``-prefixed helpers.
"""

from __future__ import annotations

from sys_buddy import api


class _Req:
    """The three inputs token resolution reads, and nothing else."""

    def __init__(self, *, cookie=None, query=None, auth=None, scheme="http"):
        self.cookies = {"sb_view": cookie} if cookie else {}
        self.query_params = {"v": query} if query else {}
        self.headers = {"authorization": auth} if auth else {}
        self.url = type("U", (), {"scheme": scheme})()


# --------------------------------------------------------------------------- #
# 1 — an explicit token beats a stale cookie
# --------------------------------------------------------------------------- #
def test_an_explicit_v_wins_over_the_cookie():
    """THE lockout. `sb_view` lives 7 days, so a token that stopped resolving — its db
    replaced, its viewer revoked — kept being sent and kept failing, and a fresh `?v=`
    link was outranked on every `/api/*` call. The dashboard's own error text says "ask
    your host for a fresh dashboard link", advice the resolver then refused to honour."""
    assert api._request_token(_Req(cookie="sbv_dead", query="sbv_fresh")) == "sbv_fresh"


def test_the_cookie_still_works_on_its_own():
    """It is the convenience path and stays that way — it exists to keep the token OUT of
    every URL after the first hop."""
    assert api._request_token(_Req(cookie="sbv_good")) == "sbv_good"


def test_a_bearer_header_beats_the_cookie_too():
    assert api._request_token(_Req(cookie="sbv_dead", auth="Bearer sbv_api")) == "sbv_api"


def test_v_beats_a_bearer_header():
    """Both are explicit; the one in the URL is the one a human just pasted."""
    r = _Req(cookie="sbv_dead", query="sbv_url", auth="Bearer sbv_api")
    assert api._request_token(r) == "sbv_url"


def test_no_token_anywhere_is_empty():
    assert api._request_token(_Req()) == ""


# --------------------------------------------------------------------------- #
# 2 — a cookie that failed is cleared, so a retry is a clean attempt
# --------------------------------------------------------------------------- #
def test_a_401_deletes_the_offending_cookie():
    """A viewer cookie that does not resolve is never going to start resolving. Leaving it
    meant the browser replayed the same dead token for the rest of its 7-day life, so every
    retry failed identically and the failure looked permanent. This is what makes the error
    screen's "Try again" button mean anything."""
    resp = api._unauthorized(_Req(cookie="sbv_dead"))
    assert resp.status_code == 401
    setcookie = "".join(
        v.decode() for k, v in resp.raw_headers if k.decode().lower() == "set-cookie"
    )
    assert "sb_view=" in setcookie
    # Deletion is expressed as an immediate expiry / empty value, not as an absent header.
    assert 'Max-Age=0' in setcookie or 'expires=' in setcookie.lower()


def test_a_401_without_a_cookie_sets_no_cookie_header():
    """Nothing to clear, so nothing is said — a caller using `?v=` or a bearer token never
    has a cookie touched."""
    resp = api._unauthorized(_Req())
    assert resp.status_code == 401
    assert not [k for k, _ in resp.raw_headers if k.decode().lower() == "set-cookie"]


# --------------------------------------------------------------------------- #
# 3 — Secure follows the request, not the configuration
# --------------------------------------------------------------------------- #
def test_the_ui_route_keys_secure_on_the_request_scheme():
    """The latent lockout, asserted at the source because the branch lives inside a route
    closure. Keying `secure` on the configured `public_url` meant a host with an https
    tunnel marked EVERY cookie Secure — including the one set when the dashboard is opened
    on `http://127.0.0.1:8787/ui?v=…`, which is the link `sys-buddy host-viewer` prints. A
    browser silently refuses to send a Secure cookie over http, so `/api/*` arrived
    unauthenticated and the page read "Access denied" with a perfectly valid token."""
    from pathlib import Path

    src = Path(api.__file__).read_text(encoding="utf-8")
    assert "secure = request.url.scheme == \"https\"" in src
    assert 'secure = (cfg.public_url or "").lower().startswith("https://")' not in src, (
        "secure is keyed on the config again — the loopback dashboard link will silently "
        "fail on any host with an https tunnel"
    )


# --------------------------------------------------------------------------- #
# the dashboard's side of it
# --------------------------------------------------------------------------- #
def test_the_access_denied_screen_offers_a_way_out():
    """Inside the desktop app's dashboard window this screen was a dead end — no address
    bar to supply a new `?v=`, nothing to click. It must carry a retry, and the retry must
    be a real reload (so the original URL, `?v=` included, is re-fetched) rather than a
    re-render of the same rejected state."""
    from pathlib import Path

    from sys_buddy import gui

    html = (Path(gui.__file__).parent / "ui.html").read_text(encoding="utf-8")
    # The RENDER branch, found by the line that actually builds the screen — the same
    # `state.error==='unauthorized'` string also appears in the fetch handler that SETS the
    # error, and splitting on it blindly lands there instead.
    denied = [
        ln for ln in html.splitlines()
        if "messageScreen(" in ln and "Access denied" in ln
    ]
    assert len(denied) == 1, f"expected one Access-denied screen, found {len(denied)}"
    assert denied[0].rstrip().endswith("true);"), (
        "the denied screen must pass retry=true — inside the desktop app's dashboard "
        "window it is otherwise a dead end"
    )
    assert "data-reload" in html
    assert "location.reload()" in html
