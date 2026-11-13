"""Browser-side usage events and errors, plus the admin's view and switch for usage data."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import admin_user
from ..models import User
from ..security import SESSION_COOKIE, read_session
from ..services import telemetry
from ..services.settings import get_setting, set_setting

router = APIRouter(tags=["telemetry"])


class ClientEvent(BaseModel):
    name: str = Field(max_length=60)
    props: dict = Field(default_factory=dict)


class ClientError(BaseModel):
    kind: str = Field(default="Error", max_length=80)
    message: str = Field(default="", max_length=2000)
    stack: list[str] = Field(default_factory=list, max_length=30)
    route: str = Field(default="", max_length=200)


class ClientBatch(BaseModel):
    events: list[ClientEvent] = Field(default_factory=list, max_length=50)
    errors: list[ClientError] = Field(default_factory=list, max_length=10)


@router.post("/api/telemetry/client", status_code=204)
def client_batch(body: ClientBatch, request: Request):
    """From the React app: page views (route templates only) and uncaught JS errors."""
    t = telemetry.client()
    uid = read_session(request.cookies.get(SESSION_COOKIE))
    for e in body.events:
        t.track("ui." + e.name.removeprefix("ui."), uid, **e.props)
    for err in body.errors:
        # JS stacks contain URLs; keep only function names and the bundle file name.
        stack = [s.split("(")[0].strip()[:80] or s.rsplit("/", 1)[-1][:80] for s in err.stack]
        t.record_error(source="browser", kind=err.kind, message=err.message, stack=stack, route=err.route)


class TelemetryIn(BaseModel):
    enabled: bool


def _status(db: Session) -> dict:
    s = get_settings()
    t = telemetry.client()
    return {
        "enabled": (get_setting(db, telemetry.SETTING_KEY) or "on") != "off",
        "server_disabled": not s.telemetry_enabled,
        "collector_configured": bool(s.telemetry_url),
        "instance_id": telemetry.instance_id(),
        "what_is_sent": telemetry.WHAT_IS_SENT,
        "never_sent": telemetry.NEVER_SENT,
        "last_sent_at": t.last_sent_at,
        "last_error": t.last_error,
        "last_batch": t.last_batch,
    }


@router.get("/api/admin/telemetry")
def get_telemetry(db: Session = Depends(get_db), _: User = Depends(admin_user)):
    return _status(db)


@router.put("/api/admin/telemetry")
def put_telemetry(body: TelemetryIn, db: Session = Depends(get_db), user: User = Depends(admin_user)):
    set_setting(db, telemetry.SETTING_KEY, "on" if body.enabled else "off")
    db.commit()
    telemetry.invalidate_enabled_cache()
    return _status(db)
