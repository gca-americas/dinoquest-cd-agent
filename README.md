# CDAgent

Autonomous canary deployment agent. Receives A2A deploy requests, computes a risk score,
sets a risk-calibrated canary percentage, monitors Cloud Run metrics, and promotes or
rolls back automatically. Learns from past deployments via Firestore — recognized patterns
skip the slow ramp and go straight to the proven canary percentage (Behavior A).

## Project structure

```
├── main.py                   # Flask entrypoint — A2A task endpoint + Agent Card
├── agent.py                  # LlmAgent definition, loads canary-deploy skill
├── tools.py                  # Cloud Run, Cloud Monitoring, Firestore tools
├── utils.py                  # emit_event (Pub/Sub) + resolve_secret (Secret Manager)
├── skills/
│   └── canary-deploy/
│       └── SKILL.md          # Agent playbook — edit to change deploy behavior
├── requirements.txt
└── Dockerfile
```

---

## Prerequisites

### 1. Enable GCP APIs

```bash
gcloud services enable \
  run.googleapis.com \
  monitoring.googleapis.com \
  firestore.googleapis.com \
  pubsub.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com \
  --project=$PROJECT_ID
```

### 2. Create the Firestore database (if not already exists)

```bash
gcloud firestore databases create \
  --region=us-central1 \
  --project=$PROJECT_ID
```

The deployment patterns are stored in collection `cdagent_deployment_patterns`.
No schema setup required — Firestore is schemaless.

### 3. Create the service account

```bash
PROJECT_ID=$(gcloud config get-value project)

gcloud iam service-accounts create cd-agent \
  --display-name="CDAgent Canary Deployer" \
  --project=$PROJECT_ID
```

### 4. Grant IAM roles

```bash
SA="cd-agent@${PROJECT_ID}.iam.gserviceaccount.com"

# Deploy new Cloud Run revisions and shift traffic
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA}" --role="roles/run.developer"

# Read images from Artifact Registry
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA}" --role="roles/artifactregistry.reader"

# Poll error rate and latency metrics during canary window
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA}" --role="roles/monitoring.viewer"

# Read and write Firestore deployment patterns
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA}" --role="roles/datastore.user"

# Call Vertex AI (Gemini model)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA}" --role="roles/aiplatform.user"

# Publish events to harness-events topic (run after topic is created)
gcloud pubsub topics add-iam-policy-binding harness-events \
  --member="serviceAccount:${SA}" --role="roles/pubsub.publisher" \
  --project=$PROJECT_ID
```

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | Yes | — | GCP project ID |
| `GOOGLE_GENAI_USE_VERTEXAI` | Yes | — | Set to `True` |
| `HOST` | **Yes (prod)** | `localhost` | Public hostname of this service — used to build the A2A agent card `url` field. Must be set to the Cloud Run hostname (e.g. `cd-agent-xxx-uc.a.run.app`) or callers will POST to `localhost` instead of this service. |
| `PROTOCOL` | **Yes (prod)** | `http` | `https` in Cloud Run, `http` for local dev |
| `PORT` | No | `8080` | Listening port |
| `CLOUD_RUN_REGION` | No | `us-central1` | Region of the target Cloud Run service |
| `CD_TARGET_SERVICE` | No | `dinoquest` | Default Cloud Run service name to deploy to |
| `GITHUB_OWNER` | Yes | `weimeilin79` | GitHub repo owner (for PR comments + releases) |
| `GITHUB_REPO` | Yes | `dinoquest` | GitHub repo name |
| `GITHUB_TOKEN` | Yes | — | GitHub PAT with `repo` scope — used to post PR comments and create releases |
| `APP_ENV_VARS` | No | — | Comma-separated `KEY=VALUE` pairs injected into every deployed revision (e.g. `LEADERBOARD_ENABLED=true`) |
| `APP_SECRET_ENV_VARS` | No | — | Comma-separated `KEY=secret_name:version` pairs mounted from Secret Manager (e.g. `GEMINI_API_KEY=gemini-api-key:1`) |
| `SLACK_WEBHOOK_URL` | No | — | Webhook for posting deploy reports back to Slack |
| `HARNESS_EVENTS_TOPIC` | For dino-theater | — | `projects/{project}/topics/harness-events` |

CDAgent uses Application Default Credentials for all GCP APIs (Cloud Run, Monitoring, Firestore).
`GITHUB_TOKEN` is the only secret required explicitly — store it in Secret Manager and reference
via `GITHUB_TOKEN_SECRET` if preferred (see `utils.resolve_secret`).

---

## Running locally

```bash
cd CDAgent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
gcloud auth application-default login
```

Create a `.env` file (copy the values below):

```env
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_GENAI_USE_VERTEXAI=True
HARNESS_EVENTS_TOPIC=projects/your-project-id/topics/harness-events
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
CLOUD_RUN_REGION=us-central1
CD_TARGET_SERVICE=dinoquest
PORT=8081
GITHUB_OWNER=weimeilin79
GITHUB_REPO=dinoquest
GITHUB_TOKEN=ghp_...
APP_ENV_VARS=LEADERBOARD_ENABLED=true
APP_SECRET_ENV_VARS=GEMINI_API_KEY=gemini-api-key:1
```

Start the server (listens on port 8081 locally to avoid clashing with CIAgent on 8080):

```bash
python main.py
```

### Trigger a deploy via Slack endpoint

```bash
# Minimal — CDAgent derives feature name from SHA
curl -s -X POST http://localhost:8081/slack \
  -d "text=deploy us-central1-docker.pkg.dev/PROJECT/dinoquest/app:SHA" \
  -d "user_name=test" -d "user_id=test" -d "channel_id=test"

# With PR context — CDAgent posts comment on the PR after deploy
curl -s -X POST http://localhost:8081/slack \
  -d "text=deploy us-central1-docker.pkg.dev/PROJECT/dinoquest/app:SHA PR #22 https://github.com/weimeilin79/dinoquest/pull/22" \
  -d "user_name=test" -d "user_id=test" -d "channel_id=test"
```

### Send a test A2A deploy request (called by CIAgent)

```bash
curl -X POST http://localhost:8081/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tasks/send",
    "id": "deploy-test-001",
    "params": {
      "id": "deploy-test-001",
      "message": {
        "role": "user",
        "parts": [{"text": "Deploy image us-central1-docker.pkg.dev/PROJECT/dinoquest/app:SHA\nPR: #22 — feat: add Level 2 gameplay\nBranch: level_2\nCommit: SHA\nChanged files: backend/main.py, frontend/index.html"}],
        "contextId": "deploy-test-001"
      }
    }
  }'
```

### Check the Agent Card

```bash
curl http://localhost:8081/.well-known/agent.json
```

### Inspect Firestore patterns after a deploy

```bash
gcloud firestore documents list \
  projects/$PROJECT_ID/databases/\(default\)/documents/cdagent_deployment_patterns \
  --project=$PROJECT_ID
```

---

## Deploying to Cloud Run

### 1. Build and push the image

```bash
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
gcloud secrets add-iam-policy-binding gemini-api-key \
  --project=${PROJECT_ID} \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"  


PROJECT_ID=$(gcloud config get-value project)
IMAGE="us-central1-docker.pkg.dev/${PROJECT_ID}/dinoquest/cd-agent:latest"

gcloud builds submit --tag $IMAGE . --project=$PROJECT_ID
```

### 2. Store GitHub token in Secret Manager

```bash
echo -n "ghp_your_token_here" | \
  gcloud secrets create github-token --data-file=- --project=$PROJECT_ID

gcloud secrets add-iam-policy-binding github-token \
  --member="serviceAccount:${SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project=$PROJECT_ID
```

Grant the compute SA access to the app secrets (needed by deployed dinoquest revisions):

```bash
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")

gcloud secrets add-iam-policy-binding gemini-api-key \
  --project=$PROJECT_ID \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

### 3. Deploy CDAgent to Cloud Run

```bash
SA="cd-agent@${PROJECT_ID}.iam.gserviceaccount.com"
TOPIC="projects/${PROJECT_ID}/topics/harness-events"
SERVICE_NAME="dinoquest"
GITHUB_OWNER="weimeilin79"
GITHUB_REPO="https://github.com/weimeilin79/dinoquest"

CD_AGENT_URL=$(gcloud run services describe cd-agent \
  --region=us-central1 --format="value(status.url)" --project=$PROJECT_ID | sed 's|https://||')

gcloud run deploy cd-agent \
  --image=$IMAGE \
  --region=us-central1 \
  --service-account=$SA \
  --memory=1Gi \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID}" \
  --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=True" \
  --set-env-vars="HOST=${CD_AGENT_URL},PROTOCOL=https" \
  --set-env-vars="CD_TARGET_SERVICE=${SERVICE_NAME}" \
  --set-env-vars="HARNESS_EVENTS_TOPIC=${TOPIC}" \
  --set-env-vars="GITHUB_OWNER=${GITHUB_OWNER}" \
  --set-env-vars="GITHUB_REPO=${GITHUB_REPO}" \
  --set-env-vars="LEADERBOARD_ENABLED=true" \
  --set-secrets="GEMINI_API_KEY=gemini-api-key:latest" \
  --set-secrets="GITHUB_TOKEN=github-token:latest" \
  --set-secrets="SLACK_WEBHOOK_URL=slack-webhook:latest" \
  --allow-unauthenticated \
  --min-instances=1 \
  --no-cpu-throttling \
  --timeout=300 \
  --project=$PROJECT_ID
```

Note: `--min-instances=1 --no-cpu-throttling` keeps the instance warm so Slack commands
respond within the 3-second deadline.

---

## Cloud Run background thread behavior

When CDAgent receives an A2A request from CIAgent, it immediately returns an
acknowledgment and runs the full canary deployment in a background thread. This keeps
the caller's HTTP connection short, but has an important Cloud Run implication:

After the ACK is returned, Cloud Run has no active request. It can scale down the
instance after the idle timeout (~15 min in practice). CDAgent's canary pipeline takes
5–10 min. The `daemon=False` flag tells Python not to exit until the thread finishes,
but Cloud Run sends SIGKILL regardless after the grace period. If you want a guarantee,
set `min-instances=1` on both agents — that keeps the instance alive permanently. For a
demo this is the right call anyway (no cold starts).

The deploy command above already includes `--min-instances=1` for this reason.

### Background task timeout

`_AGENT_TURN_TIMEOUT_S` in `main.py` caps how long a single CD run may take before
`asyncio.wait_for` cancels it and a timeout notice is posted to Slack. Default:
**900s (15 min)** — sized to cover a full canary deploy, traffic shifts, and the
metric-polling window with margin.

On timeout the user gets `"CD run timed out after 900s — see logs."` in Slack so a
hung canary doesn't fail silently.

Raise this if your canary window is longer (e.g. you extended `poll_canary_metrics`
to monitor for >15 min). Lower it if you'd rather fail fast in a demo.

### 4. Updating after code changes

```bash
gcloud builds submit --tag $IMAGE . && \
gcloud run services update cd-agent --image=$IMAGE --region=us-central1
```

### 5. Point CIAgent at CDAgent

After deploying, set `CDAGENT_URL` on CIAgent's Cloud Run service:

```bash
CDAGENT_URL=$(gcloud run services describe cd-agent \
  --region=us-central1 --format="value(status.url)")

gcloud run services update ci-agent \
  --update-env-vars="CDAGENT_URL=${CDAGENT_URL}" \
  --region=us-central1
```

### 3. Updating after code changes

```bash
gcloud builds submit --tag $IMAGE . && \
gcloud run services update cd-agent --image=$IMAGE --region=us-central1
```

---

## How CIAgent calls CDAgent (A2A)

After a successful CI build and image verification, CIAgent sends an A2A JSON-RPC request
to CDAgent's `POST /` endpoint. CDAgent derives `feature_name` and `change_description`
from the context — no manual input needed.

```json
{
  "jsonrpc": "2.0",
  "method": "tasks/send",
  "id": "correlation-id",
  "params": {
    "id": "correlation-id",
    "message": {
      "role": "user",
      "parts": [{
        "text": "Deploy image us-central1-docker.pkg.dev/PROJECT/dinoquest/app:SHA\nPR: #22 — feat: add Level 2 gameplay\nBranch: level_2\nCommit: SHA\nChanged files: backend/main.py, frontend/index.html\nCorrelation ID: correlation-id"
      }],
      "contextId": "correlation-id"
    }
  }
}
```

CDAgent derives:
- `feature_name` → branch name in kebab-case (`level_2` → `level-2`)
- `change_description` → summary of changed files (`backend/main.py` → "backend API changes")

After promotion, CDAgent:
1. Creates a GitHub release tagged `v<sha>` with the full CD report as release notes
2. Posts a deployment summary comment on the PR
3. Posts the pipeline diagram with ✅/❌ ticks to Slack

---

## Behavior A — Pattern memory (the fast-lane wow moment)

**First request: "ship volcano-dodge"**
1. CDAgent checks Firestore — no matching pattern found
2. Risk-scores the change → e.g. score 6/10 → starts at 10% canary
3. Runs full slow ramp: 10% → 25% → 50% → 100% with 15-min monitoring window
4. Writes pattern to Firestore: `{feature_signature: "volcano-dodge", optimal_canary_percent: 50, ...}`

**Second request: "ship lava-floor"**
1. CDAgent checks Firestore — finds `volcano-dodge` pattern
2. Agent reasons: "lava-floor is a similar new game mechanic, fast-lane applies"
3. Skips directly to 50% canary
4. dino-theater shows both deploys side by side with elapsed time — the speed difference is visible

**To reset Behavior A between dress rehearsals:**
```bash
# Delete all deployment pattern documents
gcloud firestore documents delete \
  --project=$PROJECT_ID \
  $(gcloud firestore documents list \
    projects/$PROJECT_ID/databases/\(default\)/documents/cdagent_deployment_patterns \
    --format="value(name)" --project=$PROJECT_ID)
```

Or use the Firebase Console → Firestore → delete the `cdagent_deployment_patterns` collection.

---

## Traffic shift events in dino-theater

Every call to `_shift_traffic` emits a `traffic_shifted` event to Pub/Sub:

```json
{
  "agent": "CDAgent",
  "event_type": "traffic_shifted",
  "payload": {
    "service": "dinoquest",
    "revision": "dinoquest-00042-abc",
    "percent": 10
  }
}
```

dino-theater renders this as the canary traffic bar animating from 0% → 10% → 50% → 100%.
The bar moves in real time as each shift event arrives.
