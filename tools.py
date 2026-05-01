import json
import logging
import os
import time
from datetime import datetime, timezone

import requests
from google.api_core.exceptions import NotFound
from google.cloud import firestore, monitoring_v3, run_v2

from utils import emit_event

GITHUB_API = "https://api.github.com"

log = logging.getLogger(__name__)


def _build_container_env() -> list:
    """Build Cloud Run EnvVar list from APP_ENV_VARS and APP_SECRET_ENV_VARS in environment."""
    envs = []
    for pair in os.environ.get("APP_ENV_VARS", "").split(","):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            envs.append(run_v2.EnvVar(name=k.strip(), value=v.strip()))
    for pair in os.environ.get("APP_SECRET_ENV_VARS", "").split(","):
        pair = pair.strip()
        if "=" in pair:
            name, ref = pair.split("=", 1)
            secret, version = (ref.split(":") + ["latest"])[:2]
            envs.append(run_v2.EnvVar(
                name=name.strip(),
                value_source=run_v2.EnvVarSource(
                    secret_key_ref=run_v2.SecretKeySelector(
                        secret=secret.strip(),
                        version=version.strip(),
                    )
                ),
            ))
    return envs


# ── Cloud Run traffic management ──────────────────────────────────────────────

def get_stable_revision(project_id: str, service_name: str, region: str) -> str:
    """Return the revision currently serving the majority of production traffic.
    Returns new_service=true if the service doesn't exist yet."""
    client = run_v2.ServicesClient()
    name = f"projects/{project_id}/locations/{region}/services/{service_name}"
    try:
        svc = client.get_service(name=name)
    except NotFound:
        return json.dumps({"stable_revision": None, "stable_percent": 0, "new_service": True})
    traffic = sorted(svc.traffic_statuses, key=lambda t: t.percent, reverse=True)
    if not traffic:
        return json.dumps({"stable_revision": None, "stable_percent": 0, "new_service": False})
    top = traffic[0]
    # traffic_statuses may have an empty revision when traffic targets LATEST —
    # fall back to latest_ready_revision for the actual name.
    rev = top.revision.split("/")[-1] if top.revision else ""
    if not rev and svc.latest_ready_revision:
        rev = svc.latest_ready_revision.split("/")[-1]
    return json.dumps({
        "stable_revision": rev or None,
        "stable_percent": top.percent,
        "new_service": False,
    })


def deploy_revision(project_id: str, service_name: str, region: str, image_uri: str) -> str:
    """Deploy a new Cloud Run revision. Creates the service if it doesn't exist yet."""
    client = run_v2.ServicesClient()
    parent = f"projects/{project_id}/locations/{region}"
    name = f"{parent}/services/{service_name}"
    container_env = _build_container_env()
    try:
        svc = client.get_service(name=name)
        svc.template.containers[0].image = image_uri
        svc.template.containers[0].env = container_env
        svc.traffic = []
        op = client.update_service(service=svc)
    except NotFound:
        log.info("Service %s not found — creating new service", service_name)
        new_svc = run_v2.Service(
            template=run_v2.RevisionTemplate(
                containers=[run_v2.Container(image=image_uri, env=container_env)],
            ),
            traffic=[],
        )
        op = client.create_service(parent=parent, service=new_svc, service_id=service_name)
    try:
        result = op.result()
    except Exception as e:
        log.error("deploy_revision op failed: %s", e)
        return json.dumps({"error": str(e), "status": "failed", "image": image_uri})
    new_rev = result.latest_created_revision.split("/")[-1] if result.latest_created_revision else ""
    log.info("Deployed revision %s (no traffic)", new_rev)
    return json.dumps({"status": "deployed", "new_revision": new_rev, "image": image_uri})


def shift_traffic(
    project_id: str,
    service_name: str,
    region: str,
    new_revision: str,
    new_percent: int,
    stable_revision: str = "",
    correlation_id: str = "",
) -> str:
    """Split traffic between new revision (canary) and stable. new_percent 0–100.

    At 100%, the new revision takes all traffic (promotion complete).
    At 0%, traffic returns to stable (rollback).
    """
    client = run_v2.ServicesClient()
    name = f"projects/{project_id}/locations/{region}/services/{service_name}"
    svc = client.get_service(name=name)

    # Without a stable revision to absorb the remainder, a partial split is invalid.
    effective_percent = 100 if not stable_revision else new_percent

    traffic = [
        run_v2.TrafficTarget(
            type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
            revision=new_revision,
            percent=effective_percent,
        )
    ]
    if stable_revision and effective_percent < 100:
        traffic.append(run_v2.TrafficTarget(
            type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
            revision=stable_revision,
            percent=100 - effective_percent,
        ))

    svc.traffic = traffic
    op = client.update_service(service=svc)
    try:
        op.result()
    except Exception as e:
        log.error("shift_traffic op failed: %s", e)
        return json.dumps({"error": str(e), "status": "failed"})

    emit_event("CDAgent", "traffic_shifted", {
        "service": service_name,
        "revision": new_revision,
        "percent": new_percent,
        "stable_revision": stable_revision,
    }, correlation_id)

    log.info("Traffic: %s → %d%% to %s", service_name, new_percent, new_revision)
    return json.dumps({"status": "traffic_shifted", "revision": new_revision, "percent": new_percent})


# ── Canary metrics ────────────────────────────────────────────────────────────

def poll_canary_metrics(
    project_id: str,
    service_name: str,
    canary_revision: str,
    stable_revision: str,
) -> str:
    """Poll Cloud Monitoring for error rate on canary vs stable over the last 30 seconds."""
    client = monitoring_v3.MetricServiceClient()
    window = 30
    now = time.time()
    interval = monitoring_v3.TimeInterval(
        end_time={"seconds": int(now)},
        start_time={"seconds": int(now) - window},
    )
    aggregation = monitoring_v3.Aggregation(
        alignment_period={"seconds": window},
        per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_SUM,
        cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
    )

    def _count(revision: str, code_class: str | None = None) -> float:
        query_filter = (
            f'metric.type="run.googleapis.com/request_count"'
            f' AND resource.labels.revision_name="{revision}"'
            f' AND resource.type="cloud_run_revision"'
        )
        if code_class:
            query_filter += f' AND metric.labels.response_code_class="{code_class}"'
        total = 0.0
        try:
            request = monitoring_v3.ListTimeSeriesRequest(
                name=f"projects/{project_id}",
                filter=query_filter,
                interval=interval,
                aggregation=aggregation,
            )
            for ts in client.list_time_series(request=request):
                for pt in ts.points:
                    total += pt.value.int64_value or pt.value.double_value
        except Exception as e:
            log.warning("metrics fetch failed (%s): %s", revision, e)
        return total

    c_total = _count(canary_revision)
    s_total = _count(stable_revision)
    c_5xx = _count(canary_revision, "5xx")
    s_5xx = _count(stable_revision, "5xx")

    c_rate = (c_5xx / c_total) if c_total > 0 else 0.0
    s_rate = (s_5xx / s_total) if s_total > 0 else 0.0

    return json.dumps({
        "canary_revision": canary_revision,
        "stable_revision": stable_revision,
        "canary_requests": int(c_total),
        "stable_requests": int(s_total),
        "canary_error_rate": round(c_rate, 4),
        "stable_error_rate": round(s_rate, 4),
        "error_rate_ratio": round(c_rate / s_rate, 2) if s_rate > 0 else None,
        "window_seconds": window,
        "verdict": (
            "ROLLBACK" if s_rate > 0 and c_rate > s_rate * 2
            else "HOLD" if s_rate > 0 and c_rate > s_rate * 1.5
            else "OK"
        ),
    })


# ── Firestore deployment memory ───────────────────────────────────────────────

def read_deployment_patterns(project_id: str, feature_signature: str) -> str:
    """Fetch all past deployment patterns from Firestore memory.

    Returns all records — the agent reasons about which (if any) match the current feature.
    """
    db = firestore.Client(project=project_id)
    patterns = [doc.to_dict() for doc in db.collection("cdagent_deployment_patterns").stream()]
    return json.dumps({"patterns": patterns, "queried_signature": feature_signature})


def write_deployment_pattern(project_id: str, pattern: dict, correlation_id: str = "") -> str:
    """Persist a successful deployment pattern to Firestore for future fast-lane matching."""
    db = firestore.Client(project=project_id)
    pattern["timestamp"] = datetime.now(timezone.utc).isoformat()
    doc_id = pattern.get("feature_signature", "unknown").replace(" ", "_")
    db.collection("cdagent_deployment_patterns").document(doc_id).set(pattern)

    emit_event("CDAgent", "memory_written", {
        "collection": "cdagent_deployment_patterns",
        "key": doc_id,
        "summary": (
            f"canary={pattern.get('optimal_canary_percent')}% "
            f"promote_in={pattern.get('time_to_promote')}s"
        ),
    }, correlation_id)

    return json.dumps({"status": "written", "document": doc_id})


# ── GitHub ────────────────────────────────────────────────────────────────────

def post_github_pr_comment(owner: str, repo: str, pr_number: int, body: str, github_token: str) -> str:
    """Post a deployment status comment on a GitHub PR."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
    resp = requests.post(
        url,
        json={"body": body},
        headers={"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"},
        timeout=30,
    )
    return json.dumps({"http_status": resp.status_code})


def create_github_release(
    owner: str,
    repo: str,
    tag: str,
    name: str,
    body: str,
    commit_sha: str,
    github_token: str,
    prerelease: bool = False,
) -> str:
    """Create a GitHub release (and tag) pointing at commit_sha."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/releases"
    resp = requests.post(
        url,
        json={
            "tag_name": tag,
            "name": name,
            "body": body,
            "target_commitish": commit_sha,
            "draft": False,
            "prerelease": prerelease,
        },
        headers={"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"},
        timeout=30,
    )
    data = resp.json()
    if not resp.ok:
        return json.dumps({"error": data.get("message", resp.text), "http_status": resp.status_code})
    return json.dumps({
        "http_status": resp.status_code,
        "release_url": data.get("html_url"),
        "tag": tag,
    })
