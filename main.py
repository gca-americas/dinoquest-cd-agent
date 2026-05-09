"""CDAgent — A2A server entrypoint (Starlette + uvicorn via to_a2a).

Entry points:
  POST /          — A2A JSON-RPC (handled by ADK to_a2a)
  POST /slack     — Slack slash command from a human
  POST /chat      — Google Chat webhook from a human

  GET  /.well-known/agent.json  — A2A Agent Card (handled by to_a2a)
  GET  /health                  — Cloud Run health check
"""

import os
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

import asyncio
import json
import logging
import os
import ssl
import threading
import uuid

import certifi
import requests
import uvicorn
from dotenv import load_dotenv
load_dotenv()

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent import build_agent, set_correlation_id
from utils import emit_event, resolve_secret

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

HOST = os.environ.get("HOST", "localhost")
PORT = int(os.environ.get("PORT", 8080))
PROTOCOL = os.environ.get("PROTOCOL", "http")
# Cloud Run exposes HTTPS on 443, not 8080. Use 443 for the agent card URL when
# running in HTTPS mode so callers don't get a :8080 suffix that's unreachable.
_CARD_PORT = 443 if PROTOCOL == "https" else PORT
APP_NAME = "cd_pipeline"
_USER_ID = "caller"

_SLACK_WEBHOOK_URL: str = ""
_slack_lock = threading.Lock()


def _make_tls12_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(certifi.where())
    return ctx


class _TLS12Adapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = _make_tls12_context()
        super().init_poolmanager(*args, **kwargs)


def _resolve_slack_webhook() -> None:
    global _SLACK_WEBHOOK_URL
    val = resolve_secret("SLACK_WEBHOOK_URL", "SLACK_WEBHOOK_SECRET")
    if val:
        _SLACK_WEBHOOK_URL = val
    else:
        log.warning("SLACK_WEBHOOK_URL and SLACK_WEBHOOK_SECRET both unset — Slack post-back disabled")


def _post_to_slack(text: str) -> None:
    if not _SLACK_WEBHOOK_URL:
        log.warning("Slack post-back skipped — _SLACK_WEBHOOK_URL is empty")
        return
    log.info("Slack post-back sending | text_len=%d preview=%.80s", len(text or ""), (text or "")[:80])
    for attempt in range(3):
        if attempt:
            import time; time.sleep(2 ** attempt)
        try:
            with _slack_lock:
                with requests.Session() as session:
                    adapter = _TLS12Adapter(pool_connections=1, pool_maxsize=1, max_retries=0)
                    session.mount("https://", adapter)
                    resp = session.post(
                        _SLACK_WEBHOOK_URL,
                        json={"text": text},
                        headers={"Connection": "close"},
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        log.info("Slack post-back OK")
                        return
                    log.warning("Slack post-back HTTP %s: %s", resp.status_code, resp.text[:100])
                    return
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
            log.warning("Slack post-back attempt %d failed: %s", attempt + 1, e)
    log.warning("Slack post-back failed after 3 attempts")


# ── Shared agent + session service (persistent across Slack/Chat messages) ────

_agent = build_agent()
_session_service = InMemorySessionService()
_runner = Runner(agent=_agent, app_name=APP_NAME, session_service=_session_service)


async def _get_or_create_session(session_id: str) -> None:
    existing = await _session_service.get_session(
        app_name=APP_NAME, user_id=_USER_ID, session_id=session_id
    )
    if existing is None:
        await _session_service.create_session(
            app_name=APP_NAME, user_id=_USER_ID, session_id=session_id
        )
        log.info("New session created | session_id=%s", session_id)
    else:
        log.info("Resuming existing session | session_id=%s turns=%s",
                 session_id, len(existing.events))


# ── Agent runner (Slack/Chat routes use this directly) ────────────────────────

async def _run_agent(task_id: str, message: str, correlation_id: str) -> str:
    set_correlation_id(correlation_id)
    await _get_or_create_session(task_id)
    final_response = ""
    async for event in _runner.run_async(
        user_id=_USER_ID,
        session_id=task_id,
        new_message=types.Content(role="user", parts=[types.Part(text=message)]),
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_response = event.content.parts[0].text
    emit_event("CDAgent", "pipeline_step", {"step": f"CD report: {final_response[:260]}"}, correlation_id)
    return final_response


# Background tasks run on uvicorn's long-lived event loop via asyncio.create_task
# instead of `threading.Thread + asyncio.run`. The old pattern closed a fresh
# loop per request, leaving module-level async clients bound to dead loops —
# which surfaces as "Event loop is closed" on the next request.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _schedule_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


# Hard ceiling on a single CD turn so that no failure mode (stuck LLM, stuck
# tool, unreachable peer) can leave the task hanging indefinitely. A canary
# deploy with monitoring can run for several minutes; pick a generous bound.
_AGENT_TURN_TIMEOUT_S = 900


async def _run_and_reply_async(task_id: str, message: str, correlation_id: str, reply_fn) -> None:
    try:
        result = await asyncio.wait_for(
            _run_agent(task_id, message, correlation_id),
            timeout=_AGENT_TURN_TIMEOUT_S,
        )
        log.info("Agent run complete [task=%s] result_len=%d preview=%.80s", task_id, len(result or ""), (result or "")[:80])
        # reply_fn uses requests.Session (sync); off-load so it doesn't block the loop.
        await asyncio.to_thread(reply_fn, result)
    except asyncio.TimeoutError:
        log.warning("Agent run timed out after %ds [task=%s]", _AGENT_TURN_TIMEOUT_S, task_id)
        await asyncio.to_thread(reply_fn, f"CD run timed out after {_AGENT_TURN_TIMEOUT_S}s — see logs.")
    except Exception as e:
        log.exception("Background agent run failed for task %s", task_id)
        await asyncio.to_thread(reply_fn, f"CD run failed: {e}")


# ── Middleware: handle A2A task requests asynchronously ───────────────────────
# Intercepts POST /, starts deployment in a background thread, and returns an
# immediate A2A "completed" acknowledgment so the caller's HTTP request finishes
# quickly. CDAgent posts the full deploy report to Slack when done.

class _A2AAsyncMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "POST" and request.url.path == "/":
            try:
                body = await request.body()
                data = json.loads(body)
                msg = data.get("params", {}).get("message", {})
                parts = msg.get("parts", [])
                text = " ".join(p.get("text", "") for p in parts if p.get("text"))
                cid = msg.get("contextId") or str(data.get("id") or "")
                task_id = data.get("params", {}).get("id") or str(uuid.uuid4())
                request_id = data.get("id")

                if cid:
                    set_correlation_id(cid)
                log.info("A2A task received | task_id=%s cid=%s | text=%.200s",
                         task_id, cid, text)

                _schedule_background(
                    _run_and_reply_async(task_id, text, cid, _post_to_slack)
                )

                ack = json.dumps({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "id": task_id,
                        "contextId": cid or task_id,
                        "status": {"state": "completed"},
                        "artifacts": [{
                            "artifactId": f"{task_id}-ack",
                            "parts": [{"kind": "text", "text": "Deploy task accepted — CDAgent processing in background. Results will be posted to Slack."}]
                        }],
                    },
                })
                return Response(content=ack, media_type="application/json", status_code=200)

            except Exception:
                log.warning("A2A async middleware failed, falling through to handler",
                            exc_info=True)

        return await call_next(request)


# ── Slack slash command ───────────────────────────────────────────────────────

async def handle_slack(request: Request) -> Response:
    """Slack slash command. Returns immediately; posts result back via webhook."""
    form = await request.form()
    text = str(form.get("text", "")).strip()
    user_name = str(form.get("user_name", "unknown"))
    user_id = str(form.get("user_id", user_name))
    channel_id = str(form.get("channel_id", "default"))

    if not text:
        return JSONResponse({"text": "Usage: /cd <deploy request>"})

    _deploy_keywords = ("deploy", "canary", "rollback", "promote", "traffic", "revision")
    is_deploy_run = any(kw in text.lower() for kw in _deploy_keywords)
    if is_deploy_run:
        task_id = f"slack-{channel_id}-{user_id}-{uuid.uuid4().hex[:8]}"
    else:
        task_id = f"slack-{channel_id}-{user_id}"

    emit_event("CDAgent", "chat_message_received", {
        "platform": "slack",
        "user": user_name,
        "message": text,
    }, task_id)
    log.info("Slack message from %s: %.100s", user_name, text)

    _schedule_background(
        _run_and_reply_async(task_id, text, task_id, _post_to_slack)
    )

    return JSONResponse({
        "response_type": "in_channel",
        "text": f"🚀 CDAgent on it: *{text[:100]}*",
    })


# ── Google Chat webhook ───────────────────────────────────────────────────────

async def handle_gchat(request: Request) -> Response:
    """Google Chat webhook. Returns immediately; posts result back via Slack webhook."""
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400)

    if body.get("type") == "ADDED_TO_SPACE":
        return JSONResponse({"text": "CDAgent ready. Send a deploy request."})
    if body.get("type") == "REMOVED_FROM_SPACE":
        return Response(status_code=200)

    message = body.get("message", {})
    text = message.get("text", "").strip()
    user_name = message.get("sender", {}).get("displayName", "unknown")
    space_name = body.get("space", {}).get("name", "")

    if not text:
        return JSONResponse({"text": "Send a deploy request, e.g. 'deploy image us-central1-docker.pkg.dev/...:sha'"})

    user_id = message.get("sender", {}).get("name", user_name).replace("/", "-")
    task_id = f"gchat-{space_name.replace('/', '-')}-{user_id}" or f"gchat-{str(uuid.uuid4())}"
    emit_event("CDAgent", "chat_message_received", {
        "platform": "google_chat",
        "user": user_name,
        "message": text,
        "space": space_name,
    }, task_id)
    log.info("GChat message from %s: %.100s", user_name, text)

    _schedule_background(
        _run_and_reply_async(task_id, text, task_id, _post_to_slack)
    )

    return JSONResponse({"text": f"🚀 CDAgent on it: {text[:100]}"})


# ── Health check ──────────────────────────────────────────────────────────────

async def health(request: Request) -> Response:
    return Response(status_code=200)


# ── Build the A2A Starlette app ───────────────────────────────────────────────

_resolve_slack_webhook()

app = to_a2a(_agent, host=HOST, port=_CARD_PORT, protocol=PROTOCOL)
app.add_middleware(_A2AAsyncMiddleware)
app.add_route("/slack", handle_slack, methods=["POST"])
app.add_route("/chat", handle_gchat, methods=["POST"])
app.add_route("/health", health, methods=["GET"])


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
