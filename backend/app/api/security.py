"""Request-level protections applied to every response.

- Security headers (no framing, no MIME sniffing, strict CSP for the dashboard).
- CSRF: state-changing API requests must carry ``X-Requested-With: CertVerify``. Browsers
  only let pages on our own origin add custom headers (no CORS is enabled), so another site
  cannot make a signed-in browser change anything. The session cookie is also SameSite=Strict.
"""

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "CertVerify"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if (
            request.method not in SAFE_METHODS
            and request.url.path.startswith("/api/")
            and request.headers.get(CSRF_HEADER) != CSRF_VALUE
        ):
            response: Response = JSONResponse(
                {"detail": "Missing request header; reload the page and try again."},
                status_code=403,
            )
        else:
            response = await call_next(request)
        for k, v in HEADERS.items():
            response.headers.setdefault(k, v)
        return response
