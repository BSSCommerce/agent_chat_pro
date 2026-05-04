"""Routes for Agent Chat Pro community plugin."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, inspect, or_, text
from sqlalchemy.orm import Session

from agent_chat_pro.models import (
    AgentChatProMessage,
    AgentChatProThread,
    AgentChatProUserPreference,
)
from core.agents.models import Agent, AgentVisibleUser
from core.auth.service import get_current_user_from_request
from core.database.base import SessionLocal
from core.database.models import User, UserRole
from core.plugin_sdk.registry import get_registry
from core.template_env import get_templates

router = APIRouter(prefix="/agent-chat-pro", tags=["agent-chat-pro"])


def _ensure_agent_chat_pro_fork_column(db: Session) -> None:
    """SQLite / Postgres: add fork_bootstrap_pending when upgrading an existing DB."""
    bind = db.get_bind()
    insp = inspect(bind)
    try:
        cols = {c["name"] for c in insp.get_columns("plugin_agent_chat_pro_threads")}
    except Exception:
        return
    if "fork_bootstrap_pending" in cols:
        return
    dialect = bind.dialect.name
    if dialect == "sqlite":
        db.execute(
            text(
                "ALTER TABLE plugin_agent_chat_pro_threads "
                "ADD COLUMN fork_bootstrap_pending BOOLEAN NOT NULL DEFAULT 0"
            )
        )
    else:
        db.execute(
            text(
                "ALTER TABLE plugin_agent_chat_pro_threads "
                "ADD COLUMN IF NOT EXISTS fork_bootstrap_pending BOOLEAN NOT NULL DEFAULT false"
            )
        )
    db.commit()


def get_db_acp() -> Generator[Session, None, None]:
    """Same as ``get_db`` but ensures Agent Chat Pro migrations ran for this session."""
    db = SessionLocal()
    try:
        _ensure_agent_chat_pro_fork_column(db)
        yield db
    finally:
        db.close()


class PreferencePayload(BaseModel):
    default_agent_alias: str | None = Field(default=None, max_length=128)
    theme: str | None = Field(default=None, max_length=16)


class ThreadCreatePayload(BaseModel):
    agent_alias: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=255)


class ThreadUpdatePayload(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    agent_alias: str | None = Field(default=None, max_length=128)
    fork_bootstrap_pending: bool | None = None


class ForkThreadPayload(BaseModel):
    up_to_message_id: int = Field(ge=1)


class MessageCreatePayload(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=100000)
    status: str = Field(default="completed", max_length=32)


def _auth_or_redirect(db: Session, request: Request):
    user = get_current_user_from_request(db, request)
    if user is None:
        return None, RedirectResponse(url="/login", status_code=303)
    return user, None


def _auth_or_401(db: Session, request: Request):
    user = get_current_user_from_request(db, request)
    if user is None:
        return None, JSONResponse(status_code=401, content={"detail": "Authentication required"})
    return user, None


def _is_super_admin(user: User) -> bool:
    return user.role == UserRole.SUPER_ADMIN


def _agent_visibility_filter(db: Session, user: User):
    user_id = str(getattr(user, "id", "") or "").strip()
    visible_subquery = (
        db.query(AgentVisibleUser.id)
        .filter(
            AgentVisibleUser.agent_id == Agent.id,
            AgentVisibleUser.user_id == user_id,
        )
        .exists()
    )
    return or_(
        Agent.created_by_user_id.is_(None),
        Agent.created_by_user_id == "",
        Agent.created_by_user_id == user_id,
        visible_subquery,
    )


def _visible_agents_query(db: Session, user: User):
    query = db.query(Agent).filter(Agent.alias.is_not(None))
    if not _is_super_admin(user):
        query = query.filter(_agent_visibility_filter(db, user))
    return query


def _get_visible_agent_by_alias(db: Session, user: User, alias: str) -> Agent | None:
    clean_alias = (alias or "").strip()
    if not clean_alias:
        return None
    return _visible_agents_query(db, user).filter(Agent.alias == clean_alias).first()


def _agent_payload(agent: Agent) -> dict[str, Any]:
    return {
        "id": agent.id,
        "name": agent.name,
        "alias": (agent.alias or "").strip(),
        "description": agent.description or "",
        "status": agent.status or "",
        "tags": agent.tags or "",
    }


def _message_payload(message: AgentChatProMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "status": message.status,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def _thread_payload(db: Session, thread: AgentChatProThread) -> dict[str, Any]:
    agent = db.query(Agent).filter(Agent.id == thread.agent_id).first()
    last_message = (
        db.query(AgentChatProMessage)
        .filter(AgentChatProMessage.thread_id == thread.id)
        .order_by(desc(AgentChatProMessage.id))
        .first()
    )
    message_count = (
        db.query(AgentChatProMessage)
        .filter(AgentChatProMessage.thread_id == thread.id)
        .count()
    )
    return {
        "id": thread.id,
        "title": thread.title,
        "status": thread.status,
        "external_thread_id": thread.external_thread_id,
        "agent": _agent_payload(agent) if agent else None,
        "message_count": message_count,
        "last_message": _message_payload(last_message) if last_message else None,
        "created_at": thread.created_at.isoformat() if thread.created_at else None,
        "updated_at": thread.updated_at.isoformat() if thread.updated_at else None,
        "fork_bootstrap_pending": bool(getattr(thread, "fork_bootstrap_pending", False)),
    }


def _preference_payload(
    db: Session,
    preference: AgentChatProUserPreference | None,
) -> dict[str, Any]:
    if preference is None:
        return {"default_agent_alias": "", "theme": "light"}
    agent_alias = ""
    if preference.default_agent_id:
        agent = db.query(Agent).filter(Agent.id == preference.default_agent_id).first()
        agent_alias = (agent.alias or "").strip() if agent else ""
    return {
        "default_agent_alias": agent_alias,
        "theme": preference.theme if preference.theme in {"light", "dark"} else "light",
    }


def _thread_for_user(db: Session, user: User, thread_id: int) -> AgentChatProThread | None:
    return (
        db.query(AgentChatProThread)
        .filter(
            AgentChatProThread.id == thread_id,
            AgentChatProThread.user_id == str(user.id),
            AgentChatProThread.status != "deleted",
        )
        .first()
    )


def _default_title_from_message(content: str) -> str:
    normalized = " ".join((content or "").split())
    if not normalized:
        return "New chat"
    return normalized[:72] + ("..." if len(normalized) > 72 else "")


@router.get("", include_in_schema=False)
async def agent_chat_pro_page(request: Request, db: Session = Depends(get_db_acp)):
    user, redirect = _auth_or_redirect(db, request)
    if redirect:
        return redirect

    agents = (
        _visible_agents_query(db, user)
        .order_by(Agent.name.asc(), Agent.alias.asc(), Agent.id.asc())
        .all()
    )
    preference = (
        db.query(AgentChatProUserPreference)
        .filter(AgentChatProUserPreference.user_id == str(user.id))
        .first()
    )
    registry = get_registry()
    return get_templates().TemplateResponse(
        request=request,
        name="agent_chat_pro.html",
        context={
            "request": request,
            "user": user,
            "active_page": "community_agent_chat_pro",
            "plugin_menu_items": registry.all_menu_items,
            "agent_options": [
                _agent_payload(agent)
                for agent in agents
                if (agent.alias or "").strip()
            ],
            "preference": _preference_payload(db, preference),
        },
    )


@router.get("/api/bootstrap")
async def bootstrap_api(request: Request, db: Session = Depends(get_db_acp)):
    user, err = _auth_or_401(db, request)
    if err:
        return err

    agents = (
        _visible_agents_query(db, user)
        .order_by(Agent.name.asc(), Agent.alias.asc(), Agent.id.asc())
        .all()
    )
    preference = (
        db.query(AgentChatProUserPreference)
        .filter(AgentChatProUserPreference.user_id == str(user.id))
        .first()
    )
    threads = (
        db.query(AgentChatProThread)
        .filter(
            AgentChatProThread.user_id == str(user.id),
            AgentChatProThread.status != "deleted",
        )
        .order_by(desc(AgentChatProThread.updated_at), desc(AgentChatProThread.id))
        .limit(100)
        .all()
    )
    return JSONResponse(
        content={
            "ok": True,
            "agents": [_agent_payload(agent) for agent in agents if (agent.alias or "").strip()],
            "preference": _preference_payload(db, preference),
            "threads": [_thread_payload(db, thread) for thread in threads],
        }
    )


@router.post("/api/preferences")
async def save_preferences_api(
    payload: PreferencePayload,
    request: Request,
    db: Session = Depends(get_db_acp),
):
    user, err = _auth_or_401(db, request)
    if err:
        return err

    preference = (
        db.query(AgentChatProUserPreference)
        .filter(AgentChatProUserPreference.user_id == str(user.id))
        .first()
    )
    if preference is None:
        preference = AgentChatProUserPreference(user_id=str(user.id), theme="light")
        db.add(preference)

    if payload.theme is not None:
        theme = payload.theme.strip().lower()
        if theme not in {"light", "dark"}:
            return JSONResponse(status_code=400, content={"detail": "Theme must be light or dark"})
        preference.theme = theme

    if payload.default_agent_alias is not None:
        alias = payload.default_agent_alias.strip()
        if not alias:
            preference.default_agent_id = None
        else:
            agent = _get_visible_agent_by_alias(db, user, alias)
            if agent is None:
                return JSONResponse(status_code=404, content={"detail": "Agent not found"})
            preference.default_agent_id = agent.id

    db.commit()
    db.refresh(preference)
    return JSONResponse(content={"ok": True, "preference": _preference_payload(db, preference)})


@router.get("/api/threads")
async def list_threads_api(request: Request, db: Session = Depends(get_db_acp)):
    user, err = _auth_or_401(db, request)
    if err:
        return err

    search = (request.query_params.get("q") or "").strip()
    query = db.query(AgentChatProThread).filter(
        AgentChatProThread.user_id == str(user.id),
        AgentChatProThread.status != "deleted",
    )
    if search:
        like = f"%{search}%"
        message_match = (
            db.query(AgentChatProMessage.id)
            .filter(
                AgentChatProMessage.thread_id == AgentChatProThread.id,
                AgentChatProMessage.content.ilike(like),
            )
            .exists()
        )
        query = query.filter(or_(AgentChatProThread.title.ilike(like), message_match))
    threads = (
        query.order_by(desc(AgentChatProThread.updated_at), desc(AgentChatProThread.id))
        .limit(100)
        .all()
    )
    return JSONResponse(
        content={"ok": True, "threads": [_thread_payload(db, thread) for thread in threads]}
    )


@router.post("/api/threads")
async def create_thread_api(
    payload: ThreadCreatePayload,
    request: Request,
    db: Session = Depends(get_db_acp),
):
    user, err = _auth_or_401(db, request)
    if err:
        return err

    agent = _get_visible_agent_by_alias(db, user, payload.agent_alias)
    if agent is None:
        return JSONResponse(status_code=404, content={"detail": "Agent not found"})

    title = (payload.title or "").strip() or "New chat"
    thread = AgentChatProThread(
        user_id=str(user.id),
        agent_id=agent.id,
        external_thread_id=str(uuid.uuid4()),
        title=title,
        status="active",
    )
    db.add(thread)
    db.commit()
    db.refresh(thread)
    return JSONResponse(
        content={"ok": True, "thread": _thread_payload(db, thread)},
        status_code=201,
    )


def _parse_message_limit(raw: str | None, default: int, hard_max: int = 100) -> int:
    try:
        n = int(raw) if raw is not None and str(raw).strip() != "" else default
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, hard_max))


@router.get("/api/threads/{thread_id}")
async def get_thread_api(thread_id: int, request: Request, db: Session = Depends(get_db_acp)):
    user, err = _auth_or_401(db, request)
    if err:
        return err

    thread = _thread_for_user(db, user, thread_id)
    if thread is None:
        return JSONResponse(status_code=404, content={"detail": "Thread not found"})

    limit = _parse_message_limit(request.query_params.get("limit"), 20)
    before_raw = (request.query_params.get("before_id") or "").strip()
    before_id: int | None = None
    if before_raw.isdigit():
        before_id = int(before_raw)

    base = db.query(AgentChatProMessage).filter(AgentChatProMessage.thread_id == thread.id)
    total_count = base.count()

    if before_id is None:
        rows = base.order_by(desc(AgentChatProMessage.id)).limit(limit).all()
    else:
        rows = (
            base.filter(AgentChatProMessage.id < before_id)
            .order_by(desc(AgentChatProMessage.id))
            .limit(limit)
            .all()
        )
    rows = list(reversed(rows))

    oldest_id = rows[0].id if rows else None
    has_older = False
    if oldest_id is not None:
        has_older = (
            db.query(AgentChatProMessage)
            .filter(
                AgentChatProMessage.thread_id == thread.id,
                AgentChatProMessage.id < oldest_id,
            )
            .count()
            > 0
        )

    return JSONResponse(
        content={
            "ok": True,
            "thread": _thread_payload(db, thread),
            "messages": [_message_payload(message) for message in rows],
            "pagination": {
                "total": total_count,
                "has_older": has_older,
                "oldest_loaded_id": oldest_id,
                "newest_loaded_id": rows[-1].id if rows else None,
                "limit": limit,
            },
        }
    )


@router.post("/api/threads/{thread_id}/fork")
async def fork_thread_api(
    thread_id: int,
    payload: ForkThreadPayload,
    request: Request,
    db: Session = Depends(get_db_acp),
):
    """Copy messages up to ``up_to_message_id`` into a new thread with a fresh LangGraph thread id.

    Checkpoints are not duplicated; the UI keeps a transcript copy. While ``fork_bootstrap_pending``
    is true, the next user message prepends that transcript once for the model.
    """
    user, err = _auth_or_401(db, request)
    if err:
        return err

    source = _thread_for_user(db, user, thread_id)
    if source is None:
        return JSONResponse(status_code=404, content={"detail": "Thread not found"})

    pivot = (
        db.query(AgentChatProMessage)
        .filter(
            AgentChatProMessage.id == payload.up_to_message_id,
            AgentChatProMessage.thread_id == source.id,
        )
        .first()
    )
    if pivot is None:
        return JSONResponse(status_code=404, content={"detail": "Message not found"})

    prefix_rows = (
        db.query(AgentChatProMessage)
        .filter(
            AgentChatProMessage.thread_id == source.id,
            AgentChatProMessage.id <= payload.up_to_message_id,
        )
        .order_by(AgentChatProMessage.id.asc())
        .all()
    )
    base_title = (source.title or "Chat").strip() or "Chat"
    new_title = f"Fork · {base_title}"[:255]
    new_thread = AgentChatProThread(
        user_id=str(user.id),
        agent_id=source.agent_id,
        external_thread_id=str(uuid.uuid4()),
        title=new_title,
        status="active",
        fork_bootstrap_pending=True,
    )
    db.add(new_thread)
    db.flush()
    for row in prefix_rows:
        db.add(
            AgentChatProMessage(
                thread_id=new_thread.id,
                role=row.role,
                content=row.content,
                status=row.status,
            )
        )
    new_thread.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(new_thread)
    return JSONResponse(
        content={"ok": True, "thread": _thread_payload(db, new_thread)},
        status_code=201,
    )


@router.patch("/api/threads/{thread_id}")
async def update_thread_api(
    thread_id: int,
    payload: ThreadUpdatePayload,
    request: Request,
    db: Session = Depends(get_db_acp),
):
    user, err = _auth_or_401(db, request)
    if err:
        return err

    thread = _thread_for_user(db, user, thread_id)
    if thread is None:
        return JSONResponse(status_code=404, content={"detail": "Thread not found"})

    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            return JSONResponse(status_code=400, content={"detail": "Title cannot be empty"})
        thread.title = title[:255]
    if payload.agent_alias is not None:
        agent = _get_visible_agent_by_alias(db, user, payload.agent_alias)
        if agent is None:
            return JSONResponse(status_code=404, content={"detail": "Agent not found"})
        thread.agent_id = agent.id

    if payload.fork_bootstrap_pending is not None:
        if payload.fork_bootstrap_pending is True:
            return JSONResponse(
                status_code=400,
                content={"detail": "fork_bootstrap_pending may only be set to false"},
            )
        thread.fork_bootstrap_pending = False

    db.commit()
    db.refresh(thread)
    return JSONResponse(content={"ok": True, "thread": _thread_payload(db, thread)})


@router.delete("/api/threads/{thread_id}")
async def delete_thread_api(thread_id: int, request: Request, db: Session = Depends(get_db_acp)):
    user, err = _auth_or_401(db, request)
    if err:
        return err

    thread = _thread_for_user(db, user, thread_id)
    if thread is None:
        return JSONResponse(status_code=404, content={"detail": "Thread not found"})
    thread.status = "deleted"
    db.commit()
    return JSONResponse(content={"ok": True})


@router.post("/api/threads/{thread_id}/messages")
async def create_message_api(
    thread_id: int,
    payload: MessageCreatePayload,
    request: Request,
    db: Session = Depends(get_db_acp),
):
    user, err = _auth_or_401(db, request)
    if err:
        return err

    thread = _thread_for_user(db, user, thread_id)
    if thread is None:
        return JSONResponse(status_code=404, content={"detail": "Thread not found"})

    role = payload.role.strip().lower()
    if role not in {"user", "assistant", "system"}:
        return JSONResponse(
            status_code=400,
            content={"detail": "Role must be user, assistant, or system"},
        )
    status = payload.status.strip().lower() or "completed"
    if status not in {"completed", "error", "interrupted", "pending"}:
        return JSONResponse(status_code=400, content={"detail": "Invalid message status"})

    message = AgentChatProMessage(
        thread_id=thread.id,
        role=role,
        content=payload.content,
        status=status,
    )
    db.add(message)
    if role == "user" and thread.title == "New chat":
        thread.title = _default_title_from_message(payload.content)
    thread.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(thread)
    db.refresh(message)
    return JSONResponse(
        content={
            "ok": True,
            "thread": _thread_payload(db, thread),
            "message": _message_payload(message),
        },
        status_code=201,
    )
