import logging
import os
import threading
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from utils import emit_event

_cid = threading.local()

def set_correlation_id(cid: str) -> None:
    _cid.value = cid

def _cid_get() -> str:
    return getattr(_cid, "value", "")

from tools import (
    create_github_release,
    deploy_revision as _deploy_revision_impl,
    get_stable_revision as _get_stable_revision_impl,
    poll_canary_metrics,
    post_github_pr_comment,
    read_deployment_patterns,
    shift_traffic as _shift_traffic_impl,
    write_deployment_pattern,
)

_SKILL_DIR = Path(__file__).parent / "skills" / "canary-deploy"
log = logging.getLogger(__name__)

AGENT_CARD = {
    "name": "CDAgent",
    "description": "Autonomous canary deployment agent. Risk-scores changes, runs canary deploys, learns optimal ramp patterns via Firestore memory.",
    "version": "1.0",
    "capabilities": ["canary_deploy", "shift_traffic", "monitor_health", "rollback"],
    "skills": [
        {"id": "canary_deploy", "name": "Canary Deploy", "description": "Deploy new revision at risk-calibrated canary %"},
        {"id": "shift_traffic", "name": "Shift Traffic", "description": "Move traffic between Cloud Run revisions"},
        {"id": "monitor_health", "name": "Monitor Health", "description": "Watch error rate during canary window"},
        {"id": "rollback", "name": "Rollback", "description": "Restore 100% traffic to stable revision"},
    ],
}


def build_agent() -> LlmAgent:
    project_id = os.environ["GOOGLE_CLOUD_PROJECT"]
    region = os.environ.get("CLOUD_RUN_REGION", "us-central1")
    default_service = os.environ.get("CD_TARGET_SERVICE", "dinoquest")
    github_owner = os.environ.get("GITHUB_OWNER", "weimeilin79")
    github_repo = os.environ.get("GITHUB_REPO", "dinoquest-io")
    github_token = os.environ.get("GITHUB_TOKEN", "")

    def get_stable_revision(service_name: str = "") -> str:
        """Get the revision currently serving the majority of traffic."""
        svc = service_name or default_service
        log.info("CD [step 1] get_stable_revision | service=%s", svc)
        result = _get_stable_revision_impl(project_id, svc, region)
        log.info("CD [step 1] get_stable_revision done | %s", result[:200])
        return result

    def deploy_revision(image_uri: str, service_name: str = "") -> str:
        """Deploy a new Cloud Run revision with zero traffic. Returns the new revision name."""
        svc = service_name or default_service
        log.info("CD [step 2] deploy_revision | service=%s image=%s", svc, image_uri)
        emit_event("CDAgent", "thinking",
                   {"summary": f"Deploying new revision for {svc}"},
                   _cid_get())
        result = _deploy_revision_impl(project_id, svc, region, image_uri)
        log.info("CD [step 2] deploy_revision done | %s", result[:200])
        return result

    def shift_traffic(
        new_revision: str,
        new_percent: int,
        stable_revision: str = "",
        service_name: str = "",
        correlation_id: str = "",
    ) -> str:
        """Set traffic split. new_percent=100 promotes, new_percent=0 rolls back."""
        svc = service_name or default_service
        log.info("CD [step 3] shift_traffic | service=%s new=%s pct=%s stable=%s",
                 svc, new_revision, new_percent, stable_revision)
        emit_event("CDAgent", "thinking",
                   {"summary": f"Shifting {new_percent}% traffic to {new_revision}"},
                   _cid_get())
        result = _shift_traffic_impl(
            project_id, svc, region,
            new_revision, new_percent, stable_revision,
            correlation_id or _cid_get(),
        )
        log.info("CD [step 3] shift_traffic done | %s", result[:200])
        return result

    def poll_metrics(canary_revision: str, stable_revision: str, service_name: str = "") -> str:
        """Poll Cloud Monitoring error rate for both revisions over the last 2 minutes.
        The response includes a verdict: OK | HOLD | ROLLBACK.
        """
        svc = service_name or default_service
        log.info("CD [step 4] poll_metrics | service=%s canary=%s stable=%s",
                 svc, canary_revision, stable_revision)
        emit_event("CDAgent", "thinking",
                   {"summary": f"Polling canary metrics for {canary_revision}"},
                   _cid_get())
        result = poll_canary_metrics(project_id, svc, canary_revision, stable_revision)
        log.info("CD [step 4] poll_metrics done | %s", result[:200])
        return result

    def read_patterns(feature_signature: str) -> str:
        """Fetch all past deployment patterns from Firestore memory.
        Look for a pattern similar to feature_signature for fast-lane matching.
        """
        log.info("CD [memory] read_patterns | signature=%.100s", feature_signature)
        result = read_deployment_patterns(project_id, feature_signature)
        log.info("CD [memory] read_patterns done | %s", result[:200])
        return result

    def write_pattern(
        feature_signature: str,
        optimal_canary_percent: int,
        time_to_promote_seconds: int,
        error_budget_impact: float,
        correlation_id: str = "",
    ) -> str:
        """Write a successful deployment pattern to Firestore for future fast-lane matching."""
        log.info("CD [memory] write_pattern | signature=%.100s canary_pct=%s ttp=%s",
                 feature_signature, optimal_canary_percent, time_to_promote_seconds)
        emit_event("CDAgent", "pipeline_step",
                   {"step": "cd report: deployment pattern saved"},
                   _cid_get())
        result = write_deployment_pattern(project_id, {
            "feature_signature": feature_signature,
            "optimal_canary_percent": optimal_canary_percent,
            "time_to_promote": time_to_promote_seconds,
            "error_budget_impact": error_budget_impact,
        }, correlation_id or _cid_get())
        log.info("CD [memory] write_pattern done | %s", result[:200])
        return result

    def post_pr_comment(pr_number: int, body: str) -> str:
        """Post deployment status and release info as a PR comment."""
        log.info("CD [step 6] post_pr_comment | pr=%s", pr_number)
        result = post_github_pr_comment(github_owner, github_repo, pr_number, body, github_token)
        log.info("CD [step 6] post_pr_comment done | %s", result)
        return result

    def create_release(tag: str, name: str, body: str, commit_sha: str, prerelease: bool = False) -> str:
        """Create a GitHub release pointing at commit_sha.
        Use prerelease=True for canary; False for a full promotion."""
        log.info("CD [step 6] create_release | tag=%s commit=%s prerelease=%s", tag, commit_sha, prerelease)
        emit_event("CDAgent", "thinking",
                   {"summary": f"Creating GitHub release {tag}"},
                   _cid_get())
        result = create_github_release(github_owner, github_repo, tag, name, body, commit_sha, github_token, prerelease)
        log.info("CD [step 6] create_release done | %s", result)
        return result

    skill = load_skill_from_dir(_SKILL_DIR)
    cd_toolset = skill_toolset.SkillToolset(skills=[skill])

    return LlmAgent(
        name="cd_pipeline",
        model="gemini-2.5-flash",
        instruction=(
            "You are an autonomous canary deployment agent for DinoQuest. "
            "Follow the canary-deploy skill for every run. "
            "You may be triggered by CIAgent (after a successful build) or directly by a human via Slack. "
            "CRITICAL: Never ask for feature_name or change_description — always derive them from whatever context you have: "
            "  feature_name: use the branch name in kebab-case if provided (level_2 → level-2), "
            "    or extract a slug from the image tag SHA or PR title if no branch is given. "
            "  change_description: summarize changed files if provided "
            "    (backend/main.py → 'backend API changes', frontend/ → 'frontend changes'); "
            "    if no file list, derive from the PR title or image URI. "
            "  image_uri: the full Artifact Registry URI — always required; if missing, report error and stop. "
            "Always check memory first — if a similar past deployment exists, use the fast lane. "
            "Auto-rollback immediately when error rate doubles. "
            "After every successful promotion, write the pattern to memory. "
            "MANDATORY: always end with the full CD report from the skill's Output format section — "
            "including the pipeline diagram with ✅ ❌ ⏭️ ticks, metrics, and summary. "
            "This report is posted to Slack so make it detailed and readable."
        ),
        tools=[
            cd_toolset,
            get_stable_revision,
            deploy_revision,
            shift_traffic,
            poll_metrics,
            read_patterns,
            write_pattern,
            post_pr_comment,
            create_release,
        ],
    )
