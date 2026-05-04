import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

_publisher = None
_publisher_lock = threading.Lock()


def _get_publisher():
    global _publisher
    with _publisher_lock:
        if _publisher is None:
            from google.cloud import pubsub_v1
            _publisher = pubsub_v1.PublisherClient()
        return _publisher


def resolve_secret(env_var: str, secret_env_var: str) -> str | None:
    """Try env var first, then fetch from Secret Manager by resource name."""
    if val := os.environ.get(env_var):
        return val
    if name := os.environ.get(secret_env_var):
        try:
            from google.cloud import secretmanager
            client = secretmanager.SecretManagerServiceClient()
            return client.access_secret_version(name=name).payload.data.decode()
        except Exception as e:
            log.warning("resolve_secret(%s) failed: %s", secret_env_var, e)
    return None


def emit_event(
    agent: str,
    event_type: str,
    payload: dict[str, Any],
    correlation_id: str = "",
) -> None:
    """Publish a structured event to the harness-events Pub/Sub topic.

    Fails silently — never blocks the agent if Pub/Sub is unreachable or unconfigured.
    Set HARNESS_EVENTS_TOPIC=projects/{project}/topics/harness-events to enable.
    """
    topic = os.environ.get("HARNESS_EVENTS_TOPIC", "")
    if not topic:
        return
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "event_type": event_type,
        "payload": payload,
        "correlation_id": correlation_id,
    }
    try:
        future = _get_publisher().publish(topic, json.dumps(event).encode())
        future.add_done_callback(
            lambda f: log.warning("emit_event failed (non-fatal): %s", f.exception())
            if f.exception() else None
        )
    except Exception:
        log.warning("emit_event failed (non-fatal):", exc_info=True)
