"""FastAPI application factory and ASGI entrypoint."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.router import api_router
from app.api.security import SecurityMiddleware
from app.auth.totp import derive_key
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.security import load_storage_key
from app.db.session import Database
from app.notify.email import build_sender
from app.storage import EncryptedLocalStorage

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Starting %s v%s (%s)", settings.app_name, __version__, settings.environment)
        yield
        app.state.db.dispose()
        logger.info("Shutdown complete")

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        debug=settings.debug,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = Database(settings.database_url)
    # Loaded eagerly so a missing/invalid key fails at startup, not on the first upload.
    master_key = load_storage_key(settings)
    app.state.storage = EncryptedLocalStorage(settings.storage_dir, master_key)
    app.state.totp_key = derive_key(master_key)
    app.state.code_key = derive_key(master_key, "login-code")
    app.state.email = build_sender(settings)
    app.add_middleware(SecurityMiddleware)

    app.include_router(api_router, prefix=settings.api_prefix)
    _serve_frontend(app)
    return app


FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _serve_frontend(app: FastAPI, dist: Path = FRONTEND_DIST) -> None:
    """Serve the built dashboard (``make frontend-build``) at /, if it has been built.
    Unknown paths fall back to index.html so the dashboard's own routes work on reload."""
    if not (dist / "index.html").is_file():
        return
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        target = (dist / path).resolve()
        if path and target.is_file() and target.is_relative_to(dist):
            return FileResponse(target)
        return FileResponse(dist / "index.html")
