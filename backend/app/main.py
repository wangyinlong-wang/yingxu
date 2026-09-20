from __future__ import annotations

import fcntl
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .api import router
from .config import ROOT, AppConfig
from .db import make_engine, migrate, seed
from .services import AppError

logger = logging.getLogger("yingxu.api")


def create_app(config: AppConfig | None = None, *, initialize: bool = True) -> FastAPI:
    # API-side link expansion must not log complete signed or tracked URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = config or AppConfig()
    engine = make_engine(config)

    @asynccontextmanager
    async def lifespan(app):
        config.configure_tools()
        with (config.data_dir / "runs" / "api.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AppError("API_ALREADY_RUNNING", "同一数据目录已有 API 服务运行", 409) from None
            try:
                if initialize:
                    migrate(config)
                    seed(engine, config)
                yield
            finally:
                engine.dispose()
                fcntl.flock(handle, fcntl.LOCK_UN)

    app = FastAPI(
        title="映序本地内容工作台",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.engine, app.state.config = engine, config
    origins = [f"http://{host}:{port}" for host in ("localhost", "127.0.0.1") for port in (config.port, 5173)]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        request.state.request_id = str(uuid4())
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            return JSONResponse(
                {
                    "code": "ORIGIN_DENIED",
                    "message": "仅允许本机工作台发起请求",
                    "request_id": request.state.request_id,
                },
                status_code=403,
            )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        )
        if request.url.path.startswith("/api/") and "/assets/" not in request.url.path:
            response.headers["Cache-Control"] = "no-store"
        return response

    def problem(request: Request, code: str, message: str, status: int, details=None):
        value = {"code": code, "message": message, "request_id": getattr(request.state, "request_id", "")}
        if details:
            value["details"] = details
        return JSONResponse(value, status_code=status)

    @app.exception_handler(AppError)
    async def application_error(request: Request, error: AppError):
        return problem(request, error.code, error.message, error.status, error.details)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, error: RequestValidationError):
        # Do not echo invalid input bodies, which may accidentally contain credentials.
        details = {
            "errors": [
                {"field": ".".join(map(str, item["loc"])), "message": item["msg"]} for item in error.errors()
            ]
        }
        return problem(request, "INVALID_REQUEST", "请求字段不符合要求", 422, details)

    @app.exception_handler(IntegrityError)
    async def duplicate_record(request: Request, error: IntegrityError):
        return problem(request, "RECORD_CONFLICT", "记录已存在或关联状态发生变化，请刷新后处理", 409)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        # IntegrationError is imported late so CLI diagnostics work before optional Android modules load.
        from .integrations.errors import IntegrationError

        if isinstance(error, IntegrationError):
            return problem(request, error.code, str(error), 409)
        logger.error("Request %s failed: %s", getattr(request.state, "request_id", ""), type(error).__name__)
        return problem(request, "INTERNAL_ERROR", "操作未完成，请查看任务记录或本地运行日志", 500)

    app.include_router(router)
    static_dir = ROOT / "frontend" / "dist"
    if (static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="frontend-assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path.startswith("api/"):
            raise AppError("NOT_FOUND", "接口不存在", 404)
        index = static_dir / "index.html"
        if not index.is_file():
            raise AppError("FRONTEND_NOT_BUILT", "前端尚未构建；开发时请访问本地前端开发服务", 503)
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
