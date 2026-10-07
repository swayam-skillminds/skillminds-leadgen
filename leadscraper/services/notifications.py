"""Notifications to our own users (Phase 3.1; A.2.3 allows exactly these).

Targets are per workspace:

* ``desktop`` -- a local OS notification (macOS ``osascript``, Linux
  ``notify-send``, Windows PowerShell). Local mode only; a server has no desk.
* ``webhook`` -- a JSON POST with a ``text`` field, which Slack and Microsoft
  Teams incoming webhooks both display, plus the structured run summary.
* ``email`` -- SMTP to our own addresses. The password is a secret
  *reference* (``password_secret_name``), resolved by ``core.secrets``.

Events: ``run_completed``, ``run_failed``, ``needs_attention``. A run "needs
attention" when it completed but a lead type failed, a quarter or more of its
units failed, or its sources were blocked five or more times -- the cases a
user should look at before trusting the results.

Sending never raises: each target's last error is stored on it, and the run
is unaffected.
"""

from __future__ import annotations

import json
import logging
import platform
import shutil
import smtplib
import subprocess
from email.message import EmailMessage
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine

from ..core.secrets import get_secret, mask_url, secret_source
from ..db.models import FailureRow, NotificationTarget, Project, Run, WorkUnitRow
from .errors import Invalid, NotFound, validation_errors
from .query import Page, aware, clamp, fetch_page, order_by, utcnow
from .workspace import Workspace

log = logging.getLogger(__name__)

TARGETS = NotificationTarget.__table__
RUNS = Run.__table__
UNITS = WorkUnitRow.__table__
FAILURES = FailureRow.__table__
PROJECTS = Project.__table__

EVENTS = ("run_completed", "run_failed", "needs_attention")
BLOCK_REASONS = ("blocked", "login_wall", "captcha", "rate_limited")
TIMEOUT = 10


# --- target configs ----------------------------------------------------------------------

class DesktopConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WebhookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., pattern=r"^https?://", description="Slack / Teams / any JSON webhook.")


class EmailConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    host: str
    port: int = 587
    security: str = Field("starttls", pattern="^(starttls|ssl|none)$")
    username: str | None = None
    password_secret_name: str | None = Field(
        None, description="Name of the secret holding the SMTP password (never the password).")
    sender: str = Field(..., alias="from")
    to: list[str] = Field(..., min_length=1)


CONFIG_MODELS = {"desktop": DesktopConfig, "webhook": WebhookConfig, "email": EmailConfig}


def _validate(kind: str, config: dict[str, Any] | None,
              events: list[str] | None) -> tuple[dict[str, Any], list[str]]:
    model = CONFIG_MODELS.get(kind)
    if model is None:
        raise Invalid(f"unknown notification kind {kind!r} ({', '.join(CONFIG_MODELS)})")
    try:
        parsed = model.model_validate(config or {})
    except ValidationError as exc:
        raise Invalid(f"invalid {kind} target", errors=validation_errors(exc)) from None
    events = list(events or EVENTS)
    unknown = set(events) - set(EVENTS)
    if unknown:
        raise Invalid(f"unknown event(s) {sorted(unknown)}; known: {', '.join(EVENTS)}")
    return parsed.model_dump(by_alias=True, exclude_none=True), events


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """A target as the API shows it: webhook URLs masked, secrets as configured/not."""
    config = dict(row.get("config") or {})
    if "url" in config:
        config["url"] = mask_url(config["url"])
    secret = config.get("password_secret_name")
    out = {"id": int(row["id"]), "name": row["name"], "kind": row["kind"], "config": config,
           "events": list(row.get("events") or []), "enabled": bool(row["enabled"]),
           "created_at": aware(row["created_at"]), "last_sent_at": aware(row.get("last_sent_at")),
           "last_error": row.get("last_error") or "", "secret_configured": None}
    if secret:
        out["secret_configured"] = secret_source(secret) is not None
    return out


# --- CRUD --------------------------------------------------------------------------------

def list_targets(engine: Engine, ws: Workspace, *, sort: str | None = None,
                 limit: int | None = None, offset: int | None = None) -> Page[dict[str, Any]]:
    limit, offset = clamp(limit, offset)
    query = select(TARGETS).where(TARGETS.c.workspace_id == ws.id).order_by(*order_by(
        sort, {"name": TARGETS.c.name, "id": TARGETS.c.id, "kind": TARGETS.c.kind}, "id"))
    with engine.connect() as conn:
        rows, total = fetch_page(conn, query, limit=limit, offset=offset)
    return Page([_public(r) for r in rows], total, limit, offset)


def _row(engine: Engine, ws: Workspace, target_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(select(TARGETS).where(
            TARGETS.c.id == target_id, TARGETS.c.workspace_id == ws.id)).mappings().first()
    if row is None:
        raise NotFound(f"no notification target {target_id}")
    return dict(row)


def get_target(engine: Engine, ws: Workspace, target_id: int) -> dict[str, Any]:
    return _public(_row(engine, ws, target_id))


def create_target(engine: Engine, ws: Workspace, *, name: str, kind: str,
                  config: dict[str, Any] | None = None, events: list[str] | None = None,
                  enabled: bool = True) -> dict[str, Any]:
    if not (name or "").strip():
        raise Invalid("a notification target needs a name")
    config, events = _validate(kind, config, events)
    with engine.begin() as conn:
        target_id = conn.execute(TARGETS.insert().values(
            workspace_id=ws.id, name=name.strip(), kind=kind, config=config, events=events,
            enabled=enabled, created_at=utcnow(), last_error="")).inserted_primary_key[0]
    return get_target(engine, ws, int(target_id))


def update_target(engine: Engine, ws: Workspace, target_id: int, **fields: Any) -> dict[str, Any]:
    row = _row(engine, ws, target_id)
    values: dict[str, Any] = {}
    if fields.get("name") is not None:
        values["name"] = fields["name"].strip()
    if fields.get("config") is not None or fields.get("events") is not None:
        config = fields.get("config")
        if config is not None and row["kind"] == "webhook" and "url" not in config:
            config = {**config, "url": row["config"].get("url")}   # keep the stored URL
        values["config"], values["events"] = _validate(
            row["kind"], config if config is not None else row["config"],
            fields.get("events") if fields.get("events") is not None else row["events"])
    if fields.get("enabled") is not None:
        values["enabled"] = bool(fields["enabled"])
    if values:
        with engine.begin() as conn:
            conn.execute(update(TARGETS).where(TARGETS.c.id == target_id).values(**values))
    return get_target(engine, ws, target_id)


def delete_target(engine: Engine, ws: Workspace, target_id: int) -> dict[str, Any]:
    shaped = get_target(engine, ws, target_id)
    with engine.begin() as conn:
        conn.execute(delete(TARGETS).where(TARGETS.c.id == target_id))
    return shaped


# --- what happened ------------------------------------------------------------------------

def run_events(engine: Engine, run: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(events, reasons) a finished run raises for its notification targets."""
    status = run.get("status")
    if status == "failed":
        return ["run_failed"], [run.get("error") or "the run failed"]
    if status != "completed":
        return [], []
    reasons: list[str] = []
    stats = run.get("stats") or {}
    failed_types = [k for k, v in stats.items()
                    if isinstance(v, dict) and k != "engine" and v.get("failed")]
    if failed_types:
        reasons.append(f"lead type(s) failed: {', '.join(failed_types)}")
    with engine.connect() as conn:
        units = conn.execute(select(UNITS.c.status, func.count()).where(
            UNITS.c.run_id == run["id"]).group_by(UNITS.c.status)).all()
        blocks = conn.execute(select(func.count()).select_from(FAILURES).where(
            FAILURES.c.run_id == run["id"], FAILURES.c.reason.in_(BLOCK_REASONS))).scalar_one()
    counts = {s: int(n) for s, n in units}
    total = sum(counts.values())
    if total and counts.get("failed", 0) / total >= 0.25:
        reasons.append(f"{counts['failed']} of {total} work units failed")
    if blocks >= 5:
        reasons.append(f"sources were blocked {blocks} times")
    return (["run_completed", "needs_attention"] if reasons else ["run_completed"]), reasons


def summary_text(engine: Engine, run: dict[str, Any], events: list[str],
                 reasons: list[str]) -> tuple[str, dict[str, Any]]:
    with engine.connect() as conn:
        project = conn.execute(select(PROJECTS.c.name).where(
            PROJECTS.c.id == run["project_id"])).scalar()
    engine_stats = (run.get("stats") or {}).get("engine") or {}
    minutes = float(run.get("active_seconds") or 0) / 60
    head = {"run_failed": "failed", "needs_attention": "finished -- needs attention"}.get(
        events[-1], "finished")
    text = (f"Lead run {run['id']} ({project}) {head}: {engine_stats.get('new', 0)} new, "
            f"{engine_stats.get('merged', 0)} updated, {engine_stats.get('units_done', 0)}/"
            f"{engine_stats.get('units_total', 0)} units in {minutes:.1f} min.")
    if reasons:
        text += " " + "; ".join(reasons) + "."
    payload = {"event": events[-1], "events": events, "run_id": run["id"], "project": project,
               "status": run.get("status"), "new": engine_stats.get("new", 0),
               "updated": engine_stats.get("merged", 0), "minutes": round(minutes, 1),
               "reasons": reasons}
    return text, payload


# --- sending -------------------------------------------------------------------------------

def _send_desktop(title: str, text: str) -> None:
    import os

    if os.environ.get("LEADSCRAPER_MODE") == "server":
        raise RuntimeError("desktop notifications are local-mode only")
    system = platform.system()
    if system == "Darwin":
        script = f"display notification {json.dumps(text)} with title {json.dumps(title)}"
        subprocess.run(["osascript", "-e", script], check=True, timeout=TIMEOUT,
                       capture_output=True)
    elif system == "Linux":
        if not shutil.which("notify-send"):
            raise RuntimeError("notify-send is not installed")
        subprocess.run(["notify-send", title, text], check=True, timeout=TIMEOUT,
                       capture_output=True)
    elif system == "Windows":
        # A balloon tip from the notification area: no extra modules needed.
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n = New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true;"
            f"$n.ShowBalloonTip(10000, {json.dumps(title)}, {json.dumps(text)}, 'Info');"
            "Start-Sleep -Seconds 5; $n.Dispose()")
        subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True,
                       timeout=TIMEOUT + 10, capture_output=True)
    else:
        raise RuntimeError(f"no desktop notifications on {system}")


def _send_webhook(config: dict[str, Any], text: str, payload: dict[str, Any]) -> None:
    import requests

    response = requests.post(config["url"], json={"text": text, **payload}, timeout=TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"webhook answered HTTP {response.status_code}")


def _send_email(config: dict[str, Any], subject: str, text: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["from"]
    message["To"] = ", ".join(config["to"])
    message.set_content(text)
    password = None
    if config.get("password_secret_name"):
        password = get_secret(config["password_secret_name"])
        if password is None:
            raise RuntimeError(f"secret {config['password_secret_name']!r} is not configured")
    security = config.get("security", "starttls")
    cls = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    with cls(config["host"], int(config.get("port", 587)), timeout=TIMEOUT) as smtp:
        if security == "starttls":
            smtp.starttls()
        if config.get("username") and password is not None:
            smtp.login(config["username"], password)
        smtp.send_message(message)


def send(target: dict[str, Any], title: str, text: str, payload: dict[str, Any]) -> None:
    """Deliver one message; raises on failure (callers record the error)."""
    kind, config = target["kind"], target.get("config") or {}
    if kind == "desktop":
        _send_desktop(title, text)
    elif kind == "webhook":
        _send_webhook(config, text, payload)
    elif kind == "email":
        _send_email(config, title, text)
    else:
        raise RuntimeError(f"unknown notification kind {kind!r}")


def _record(engine: Engine, target_id: int, error: str) -> None:
    values: dict[str, Any] = {"last_error": error[:2000]}
    if not error:
        values["last_sent_at"] = utcnow()
    with engine.begin() as conn:
        conn.execute(update(TARGETS).where(TARGETS.c.id == target_id).values(**values))


def notify_run(engine: Engine, run_id: int) -> list[dict[str, Any]]:
    """Tell every subscribed target how run `run_id` ended. Never raises."""
    try:
        with engine.connect() as conn:
            run = conn.execute(select(RUNS).where(RUNS.c.id == run_id)).mappings().first()
            if run is None:
                return []
            run = dict(run)
            targets = [dict(r) for r in conn.execute(select(TARGETS).where(
                TARGETS.c.workspace_id == run["workspace_id"], TARGETS.c.enabled.is_(True)))
                .mappings()]
        if not targets:
            return []
        events, reasons = run_events(engine, run)
        if not events:
            return []
        text, payload = summary_text(engine, run, events, reasons)
    except Exception as exc:
        log.warning("run %s: could not prepare notifications: %s", run_id, exc)
        return []
    results = []
    for target in targets:
        if not set(events) & set(target.get("events") or []):
            continue
        error = ""
        try:
            send(target, "Lead run " + payload["event"].replace("_", " "), text, payload)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.warning("notification %r (%s) failed: %s", target["name"], target["kind"], error)
        try:
            _record(engine, int(target["id"]), error)
        except Exception as exc:
            log.debug("could not record the notification result: %s", exc)
        results.append({"target_id": int(target["id"]), "ok": not error, "error": error})
    return results


def send_test(engine: Engine, ws: Workspace, target_id: int) -> dict[str, Any]:
    """Send a test message to one target; returns {ok, error}."""
    target = _row(engine, ws, target_id)
    error = ""
    try:
        send(target, "Lead generator: test notification",
             "This is a test notification from the Skillminds lead generator.",
             {"event": "test", "events": ["test"]})
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    _record(engine, target_id, error)
    return {"ok": not error, "error": error}
