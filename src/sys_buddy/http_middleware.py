"""Transport-layer (ASGI) hardening: security headers + a request-size cap.

These wrap the whole app (all four surfaces) and are attached in ``run_server`` via
FastMCP's ``middleware=`` hook. Kept as plain ASGI classes (no Starlette dependency
in the signature) so they compose with the MCP streaming transport untouched.

Security-header choices follow the OWASP secure-headers guidance and the FastAPI/
Starlette production playbooks (CSP, nosniff, frame-ancestors, Referrer-Policy, HSTS).
The request-size cap addresses OWASP API4 (Unrestricted Resource Consumption): it
rejects an oversized body at the edge, before it is read into memory.
"""

from __future__ import annotations

# 1 MiB: comfortably above a full MCP JSON-RPC tool call (contract specs and message
# bodies are already capped at 64 KB per field) while blocking multi-MB body DoS.
REQUEST_MAX_BYTES = 1024 * 1024

# Content-Security-Policy for the dashboard. The page is self-contained inline JS/CSS
# (so 'unsafe-inline' is required), but this still blocks external scripts, data
# exfiltration via fetch/beacon to other origins (connect-src 'self'), framing/
# clickjacking (frame-ancestors 'none'), and <base> hijacking. Fonts are the one
# external dependency; self-hosting them would let us drop 'unsafe-inline' entirely.
DASHBOARD_CSP = (
    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'; "
    "img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'"
)


class SecurityHeadersMiddleware:
    """Add security response headers to every HTTP response (only if not already set,
    so a route that sets its own — e.g. /ui's Referrer-Policy — wins)."""

    def __init__(self, app, *, hsts: bool = False, csp: str = "") -> None:
        self.app = app
        headers: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"permissions-policy", b"geolocation=(), microphone=(), camera=(), payment=()"),
            (b"cross-origin-opener-policy", b"same-origin"),
        ]
        if csp:
            headers.append((b"content-security-policy", csp.encode()))
        if hsts:
            headers.append(
                (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
            )
        self._headers = headers

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {k.lower() for k, _ in headers}
                for key, value in self._headers:
                    if key not in present:
                        headers.append((key, value))
            await send(message)

        await self.app(scope, receive, send_wrapper)


class BodyLimitMiddleware:
    """Reject a request whose declared Content-Length exceeds ``max_bytes`` (413),
    before the body is read. Bounds memory use against a naive large-POST DoS."""

    def __init__(self, app, *, max_bytes: int = REQUEST_MAX_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for key, value in scope.get("headers", []):
                if key == b"content-length":
                    try:
                        if int(value) > self.max_bytes:
                            return await self._reject(send)
                    except ValueError:
                        pass
        return await self.app(scope, receive, send)

    async def _reject(self, send):
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": b'{"error":"request too large"}'})
