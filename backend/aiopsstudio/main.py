"""FastAPI application factory; deployment material remains outside the database."""
from contextlib import asynccontextmanager
import json

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api import configure_api
from .config import ConfigService, Vault, read_private_file
from .store import Store
from .identity import IdentityService
from .attachments import AttachmentsService, configure_attachments
from .conversations import ConversationsService, configure_conversations
from .compat import environment, state_directory


def create_app(*, database_url=None, master_key=None, password=None, public_origin=None, artifact_dir=None, testing=False, store=None, enable_runtime=True):
    config = environment()
    database_url = database_url or config.get("AIOPSSTUDIO_DATABASE_URL")
    if not store and not database_url:
        raise ValueError("缺少 AIOPSSTUDIO_DATABASE_URL，正式运行必须配置 PostgreSQL")
    if not testing and ((store and store.sqlite) or (database_url and database_url.startswith("sqlite:"))):
        raise ValueError("SQLite 仅用于单元测试，正式运行必须使用 PostgreSQL")
    if master_key is None:
        key_file = config.get("AIOPSSTUDIO_MASTER_KEY_FILE")
        if not key_file:
            raise ValueError("必须通过 AIOPSSTUDIO_MASTER_KEY_FILE 提供数据库之外的独立主密钥")
        master_key = read_private_file(key_file, "独立主密钥")
    if password is None:
        password_file = config.get("AIOPSSTUDIO_PASSWORD_FILE")
        password = read_private_file(password_file, "登录口令") if password_file else config.get("AIOPSSTUDIO_PASSWORD")
    if not password or len(password) < 12:
        raise ValueError("工作台登录口令至少需要 12 个字符")
    public_origin = (public_origin or config.get("AIOPSSTUDIO_PUBLIC_ORIGIN", "http://127.0.0.1:3000")).rstrip("/")
    artifact_dir = artifact_dir or config.get("AIOPSSTUDIO_ARTIFACT_DIR") or str(state_directory(config) / "artifacts")
    owns_store = store is None
    store = store or Store(database_url)

    @asynccontextmanager
    async def lifespan(app):
        if enable_runtime:
            await app.state.runtime.start()
        try:
            yield
        finally:
            if enable_runtime:
                await app.state.runtime.stop()
            if owns_store:
                store.close()

    app = FastAPI(title="AIOpsStudio 工作台", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.vault = Vault(store, master_key)
    app.state.config = ConfigService(store, app.state.vault, artifact_dir)
    app.state.public_origin = public_origin
    app.state.identity = IdentityService(store, password)
    app.state.attachments = AttachmentsService(store, artifact_dir, app.state.vault)
    app.state.conversations = ConversationsService(app, store)
    app.state.testing = testing
    configure_api(app)
    configure_conversations(app)
    configure_attachments(app)
    if enable_runtime:
        from .runtime import configure_runtime
        configure_runtime(app, store)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error):
        return JSONResponse({"detail": "请求参数格式无效，请检查字段与类型", "code": "invalid_request"}, status_code=422)

    @app.exception_handler(json.JSONDecodeError)
    async def invalid_json(request: Request, error):
        return JSONResponse({"detail": "请求正文不是有效 JSON", "code": "invalid_json"}, status_code=422)

    @app.get("/healthz")
    async def health():
        store.get("settings", "main")
        return {"status": "ok", "database": "sqlite_test" if store.sqlite else "postgresql"}
    return app
