"""Optional same-origin site; runtime web source lives under src/frontend."""

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def mount_website(app: FastAPI, configured: Path | None = None) -> None:
    packaged = Path(__file__).resolve().parents[1] / "website"
    root = configured or (
        packaged if packaged.is_dir() else Path(__file__).resolve().parents[2] / "frontend"
    )
    if not (root / "index.html").is_file() or not (root / "assets").is_dir():
        raise ValueError(
            "website assets are missing; set GROCERY_BACKEND_FRONTEND_DIR or disable website"
        )
    app.mount("/assets", StaticFiles(directory=root / "assets"), name="website-assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(root / "index.html")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path == "/" or request.url.path.startswith("/assets/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            )
        return response
