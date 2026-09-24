"""Authenticated mobile web UI and API for the private relationship workbench."""
from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .ai import DeepSeek
from .engine import AutomationService, ReplyEngine
from .store import Problem, Store
from .trends import Trends
from .wechat_bridge import WeChatBridge


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PersonInput(Input):
    name: str = Field(min_length=1, max_length=80)


class AccountInput(Input):
    person_id: str
    label: str = Field(min_length=1, max_length=80)


class ConfigInput(Input):
    cloud: bool
    mode: str = "C"
    enabled: bool = True


class LinkInput(Input):
    external_id: str = Field(min_length=1, max_length=160)
    display_name: str = Field(min_length=1, max_length=160)
    owner_external_id: str = Field(min_length=1, max_length=160)


class MoveInput(Input):
    account_ids: list[str] = Field(min_length=1, max_length=100)
    person_id: str
    confirm_same_person: bool


class ImportInput(Input):
    rows: list[dict] = Field(min_length=1, max_length=5000)
    batch_id: str = Field(min_length=1, max_length=128)
    confirm: bool = False
    digest: str | None = None


class NoteInput(Input):
    content: str = Field(min_length=1, max_length=1000)
    message_id: str | None = None


class DraftInput(Input):
    account_id: str
    message_id: str


class ActionInput(Input):
    action: str
    messages: list[str] | None = None


def create_app(
    path,
    token,
    origin="http://127.0.0.1:8787",
    ai=None,
    bridge=None,
    trends=None,
    automation=False,
):
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("JUNSHI_ADMIN_TOKEN must contain at least 32 characters")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in ("https", "http") or not parsed.hostname
        or parsed.username or parsed.password or parsed.path not in ("", "/")
        or parsed.query or parsed.fragment
    ):
        raise ValueError("Invalid JUNSHI_PUBLIC_ORIGIN")
    if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost", "testserver"):
        raise ValueError("Phone/remote access requires an HTTPS origin")
    origin = origin.rstrip("/")

    store = Store(path)
    model = ai if ai is not None else DeepSeek()
    bridge_obj = bridge if bridge is not None else WeChatBridge(Path(path).parent)
    trend_obj = trends if trends is not None else Trends()
    engine = ReplyEngine(store, model, trend_obj)
    service = AutomationService(store, engine, bridge_obj)

    @asynccontextmanager
    async def lifespan(app):
        if automation:
            service.start()
        try:
            yield
        finally:
            service.stop()

    app = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.store = store
    app.state.model = model
    app.state.bridge = bridge_obj
    app.state.trends = trend_obj
    app.state.engine = engine
    app.state.service = service
    static = Path(__file__).parent / "static"

    @app.middleware("http")
    async def boundary(request, call_next):
        incoming_origin = request.headers.get("origin")
        if incoming_origin and incoming_origin != origin:
            response = JSONResponse({"detail": "Cross-origin requests are not allowed"}, status_code=403)
        elif request.method in ("POST", "PUT", "PATCH"):
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 4 * 1024 * 1024:
                    break
                chunks.append(chunk)
            if size > 4 * 1024 * 1024:
                response = JSONResponse({"detail": "Request exceeds 4 MiB"}, status_code=413)
            else:
                request._body = b"".join(chunks)
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                "img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            ),
        })
        return response

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[parsed.hostname])

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        return JSONResponse(
            {"detail": "Invalid request fields; check format and size limits"},
            status_code=422,
        )

    def authorized(request: Request):
        expected = "Bearer " + token
        provided = request.headers.get("authorization", "")
        if not secrets.compare_digest(provided.encode(), expected.encode()):
            raise Problem("Authentication required", 401)

    secured = [Depends(authorized)]
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def home():
        return FileResponse(static / "index.html")

    @app.get("/api/state", dependencies=secured)
    def state():
        bridge_state = bridge_obj.status()
        bridge_state["automation"] = service.status()
        return {
            "persons": store.state(),
            "model": "deepseek-flash",
            "transport": bridge_state,
            "trends": trend_obj.status(),
            "api_key_configured": bool(getattr(model, "key", False)),
            "inbox": store.inbox_feed(limit=80),
            "storage": "host_local_sqlite",
            "media_upload": False,
        }

    @app.post("/api/persons", dependencies=secured)
    def add_person(body: PersonInput):
        return {"id": store.add_person(body.name)}

    @app.post("/api/accounts", dependencies=secured)
    def add_account(body: AccountInput):
        return {"id": store.add_account(body.person_id, body.label)}

    @app.patch("/api/accounts/{account_id}", dependencies=secured)
    def configure(account_id: str, body: ConfigInput):
        if body.mode not in ("OFF", "C", "B"):
            raise Problem("mode must be OFF, C or B")
        binding = None
        try:
            binding = store.transport_account(account_id)
        except Problem as exc:
            if exc.status != 409:
                raise
        if body.mode == "B":
            if not body.cloud:
                raise Problem("B mode requires DeepSeek text disclosure consent", 409)
            status = bridge_obj.status()
            if not status.get("can_send"):
                raise Problem("The verified WeChat bridge is not ready for B mode", 409)
            if not binding:
                raise Problem("Link this account to a real WeChat contact before B mode", 409)
            if binding["owner_external_id"] != status.get("owner_external_id"):
                raise Problem("This contact belongs to a different logged-in WeChat account", 409)
            bridge_obj.validate_contact(binding["external_id"], binding["display_name"])
        store.configure(account_id, body.cloud, None)
        if binding:
            store.set_transport_mode(account_id, body.mode, body.enabled)
        return {"mode": body.mode, "cloud": body.cloud, "enabled": body.enabled}

    @app.post("/api/accounts/move", dependencies=secured)
    def move(body: MoveInput):
        if not body.confirm_same_person:
            raise Problem("Confirm these accounts belong to the same person")
        store.move(body.account_ids, body.person_id)
        return {"ok": True}

    @app.delete("/api/accounts/{account_id}", dependencies=secured)
    def delete_account(account_id: str):
        store.delete_account(account_id)
        return {"ok": True}

    @app.get("/api/bridge/contacts", dependencies=secured)
    def contacts(q: str = ""):
        if len(q) > 160:
            raise Problem("Contact search is too long")
        return {
            "contacts": bridge_obj.search_contacts(q),
            "status": bridge_obj.status(),
        }

    @app.post("/api/accounts/{account_id}/link-wechat", dependencies=secured)
    def link_wechat(account_id: str, body: LinkInput):
        status = bridge_obj.status()
        if not status.get("connected"):
            raise Problem("WeChat bridge is not connected", 503)
        if body.owner_external_id != status.get("owner_external_id"):
            raise Problem("Stale contact search result; search again", 409)
        verified = bridge_obj.validate_contact(body.external_id, body.display_name)
        store.link_transport(
            account_id,
            verified["owner_external_id"],
            verified["external_id"],
            verified["display_name"],
        )
        return {"ok": True, **verified}

    @app.delete("/api/accounts/{account_id}/link-wechat", dependencies=secured)
    def unlink_wechat(account_id: str):
        store.unlink_transport(account_id)
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/sync", dependencies=secured)
    def sync_history(account_id: str):
        return service.start_sync(account_id)

    @app.get("/api/accounts/{account_id}/sync", dependencies=secured)
    def sync_status(account_id: str):
        return service.sync_status(account_id)

    @app.post("/api/accounts/{account_id}/import", dependencies=secured)
    def ingest(account_id: str, body: ImportInput):
        return store.ingest(
            account_id, body.rows, body.batch_id, body.confirm, body.digest
        )

    @app.get("/api/accounts/{account_id}/context", dependencies=secured)
    def context(account_id: str, q: str = ""):
        if len(q) > 4000:
            raise Problem("Search text too long")
        return store.context(account_id, q)

    @app.delete("/api/accounts/{account_id}/messages/{message_id}", dependencies=secured)
    def delete_message(account_id: str, message_id: str):
        store.delete_message(account_id, message_id)
        return {"ok": True}

    @app.get("/api/accounts/{account_id}/export", dependencies=secured)
    def export(account_id: str):
        rows = store.export(account_id)
        data = "\n".join(
            json.dumps({
                "id": r["source_key"], "role": r["role"], "kind": r["kind"],
                "content": r["content"], "occurred_at": r["occurred_at"],
            }, ensure_ascii=False)
            for r in rows
        )
        return Response(
            data,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="messages.jsonl"'},
        )

    @app.post("/api/accounts/{account_id}/notes", dependencies=secured)
    def add_note(account_id: str, body: NoteInput):
        return {"id": store.add_note(account_id, body.content, body.message_id)}

    @app.delete("/api/accounts/{account_id}/notes/{note_id}", dependencies=secured)
    def delete_note(account_id: str, note_id: str):
        store.delete_note(account_id, note_id)
        return {"ok": True}

    @app.get("/api/inbox", dependencies=secured)
    def inbox():
        return {"items": store.inbox_feed(limit=150)}

    @app.post("/api/drafts", dependencies=secured)
    def draft(body: DraftInput):
        store.enqueue_inbox(body.account_id, [body.message_id])
        result = engine.generate(body.account_id, [body.message_id])
        store.attach_inbox_draft([body.message_id], result["id"], "drafted")
        return result

    @app.post("/api/drafts/{draft_id}", dependencies=secured)
    def action(draft_id: str, body: ActionInput):
        return store.draft_action(draft_id, body.action, body.messages)

    @app.post("/api/outbox/{outbox_id}/retry", dependencies=secured)
    def retry_outbox(outbox_id: str):
        service.retry_outbox(outbox_id)
        return {"ok": True}

    return app
