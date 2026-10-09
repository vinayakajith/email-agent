"""HTTP routes: thin wrappers over the core. All mutating routes require the X-Email-Agent header."""

import asyncio
import json
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from email_agent import conversations, health, jobs, onboarding, profile
from email_agent.accounts import AccountError, get_account
from email_agent.agent.loop import Agent
from email_agent.api import views
from email_agent.drafts import DraftError, approve_draft, reject_draft

router = APIRouter(prefix="/api")


def make_agent(account, conversation_id: str | None) -> Agent:
    """Seam for tests (and future per-request configuration)."""
    return Agent(account, conversation_id=conversation_id)


async def _account():
    try:
        return await get_account()
    except AccountError as e:
        raise HTTPException(409, str(e)) from e


# --- session & status -----------------------------------------------------------------------


@router.get("/session")
async def session():
    st = await onboarding.status()
    out = {"onboarding": st, "profile": None, "latest_conversation": None, "suggestions": [], "status": None}
    if st["account_id"]:
        user = await profile.get(st["account_id"])
        out["profile"] = {"name": user.display_name, "timezone": user.timezone, "about": user.about} if user else None
        recent = await conversations.recent(st["account_id"], limit=1)
        out["latest_conversation"] = str(recent[0]["id"]) if recent else None
        out["suggestions"] = await views.suggestions(st["account_id"])
    out["status"] = await asyncio.to_thread(health.summary)
    return out


@router.get("/status")
async def status():
    return await asyncio.to_thread(health.summary)


# --- onboarding -----------------------------------------------------------------------------


class Keys(BaseModel):
    openai_key: str = Field(min_length=1)
    voyage_key: str = Field(min_length=1)


@router.post("/onboarding/keys")
def save_keys(body: Keys):  # sync: runs in the threadpool (network checks)
    try:
        onboarding.save_api_keys(body.openai_key, body.voyage_key)
    except onboarding.OnboardingError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


class Gmail(BaseModel):
    email: str = Field(min_length=3)
    app_password: str = Field(min_length=1)


@router.post("/onboarding/gmail")
async def connect_gmail(body: Gmail):
    from email_agent.accounts import connect

    try:
        account = await connect(body.email, body.app_password)
    except AccountError as e:
        raise HTTPException(400, str(e)) from e
    return {"email": account.email}


class Profile(BaseModel):
    name: str
    timezone: str
    about: str = ""


@router.post("/onboarding/profile")
async def save_profile(body: Profile):
    account = await _account()
    try:
        await profile.save(account.id, body.name, body.timezone, body.about)
    except profile.ProfileError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


RANGES = {"30d": 30, "90d": 90, "1y": 365}


class Ingest(BaseModel):
    range: str = Field(pattern="^(30d|90d|1y|all)$")


@router.post("/onboarding/ingest")
async def start_ingest(body: Ingest):
    account = await _account()
    if body.range == "all":
        job_id = await jobs.enqueue(account.id, "full")
    else:
        job_id = await jobs.enqueue(account.id, "since", date.today() - timedelta(days=RANGES[body.range]))
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
async def job(job_id: int):
    account = await _account()
    row = await jobs.get(job_id)
    if row is None or row["account_id"] != account.id:
        raise HTTPException(404, "No such job")
    keys = ("id", "mode", "status", "stage", "stage_done", "stage_total", "fetched", "enriched", "embedded", "error")
    return {k: row[k] for k in keys}


# --- conversations & chat -------------------------------------------------------------------


@router.get("/conversations")
async def list_conversations():
    account = await _account()
    return [{"id": str(c["id"]), "title": c["title"], "updated_at": c["updated_at"].isoformat()}
            for c in await conversations.recent(account.id, limit=50)]


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: uuid.UUID):
    account = await _account()
    if await conversations.get(account.id, conversation_id) is None:
        raise HTTPException(404, "No such conversation")
    items = await conversations.items(conversation_id)
    return {"id": str(conversation_id), "messages": await views.render_conversation(account.id, items, conversation_id)}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: uuid.UUID | None = None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat")
async def chat(body: ChatRequest):
    account = await _account()
    if body.conversation_id and await conversations.get(account.id, body.conversation_id) is None:
        raise HTTPException(404, "No such conversation")
    agent = make_agent(account, str(body.conversation_id) if body.conversation_id else None)

    async def stream():
        # If the browser goes away, this generator is cancelled; the agent then drops the
        # uncommitted step, so the stored conversation stays valid.
        async for event, data in views.chat_events(agent, body.message):
            yield _sse(event, data)

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/drafts/{draft_id}/approve")
async def approve(draft_id: int):
    account = await _account()
    try:
        return await approve_draft(account, draft_id)
    except DraftError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/drafts/{draft_id}/reject")
async def reject(draft_id: int):
    account = await _account()
    try:
        return await reject_draft(account, draft_id)
    except DraftError as e:
        raise HTTPException(400, str(e)) from e
