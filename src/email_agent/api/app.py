"""The web application: JSON + SSE API under /api and the built frontend at /."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from email_agent.api.routes import router
from email_agent.db.pool import close_pool

WEB_DIST = Path(__file__).resolve().parents[3] / "web" / "dist"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class LocalGuard:
    """This app can send email from your account, so a web page you visit must not drive it.

    - Host must be localhost: defeats DNS-rebinding, where an attacker's domain resolves to 127.0.0.1.
    - Mutating requests need the X-Email-Agent header: cross-site pages cannot set custom headers
      without a CORS preflight, which this app never grants.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            host = headers.get(b"host", b"").decode("latin-1")
            hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            if hostname not in LOCAL_HOSTS:
                return await JSONResponse({"detail": "Forbidden host"}, 403)(scope, receive, send)
            if scope["method"] not in SAFE_METHODS and headers.get(b"x-email-agent") != b"1":
                return await JSONResponse({"detail": "Missing X-Email-Agent header"}, 403)(scope, receive, send)
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_pool()


def create_app() -> FastAPI:
    app = FastAPI(title="email-agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            file = WEB_DIST / path
            if path and file.is_file() and WEB_DIST in file.resolve().parents:
                return FileResponse(file)
            return FileResponse(WEB_DIST / "index.html")

    app.add_middleware(LocalGuard)
    return app
