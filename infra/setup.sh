#!/usr/bin/env bash
# One-time setup of a throwaway project. Requires: gcloud, bq, an owner account.
# Usage: PROJECT=my-project ./infra/setup.sh
set -euo pipefail
: "${PROJECT:?set PROJECT}"
REGION="${REGION:-us-central1}"
DATASET=mission_ledger
WORKER="ledger-worker@${PROJECT}.iam.gserviceaccount.com"
PUBLISHER="memory-publisher@${PROJECT}.iam.gserviceaccount.com"

gcloud services enable firestore.googleapis.com bigquery.googleapis.com cloudtasks.googleapis.com \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com iam.googleapis.com cloudresourcemanager.googleapis.com cloudscheduler.googleapis.com --project="$PROJECT"

gcloud firestore databases create --location="$REGION" --type=firestore-native --project="$PROJECT" || true
bq --project_id="$PROJECT" mk --location=US -d "$DATASET" || true
sed "s/\${DATASET}/${PROJECT}.${DATASET}/g" infra/bigquery/tables.sql | bq --project_id="$PROJECT" query --use_legacy_sql=false
# Retries must outlast the execution lease (120 s): a task that keeps getting
# 409 while a dead worker's lease runs out must still be alive when it expires.
# 10 s doubling to 300 s, 30 attempts: about 2 h of retries.
QUEUE_FLAGS=(--location="$REGION" --project="$PROJECT" --max-attempts=30 --min-backoff=10s \
  --max-backoff=300s --max-doublings=5)
if gcloud tasks queues describe mission-wakeups --location="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud tasks queues update mission-wakeups "${QUEUE_FLAGS[@]}"
else
  gcloud tasks queues create mission-wakeups "${QUEUE_FLAGS[@]}"
fi
gcloud tasks queues describe mission-wakeups --location="$REGION" --project="$PROJECT" \
  --format='value(retryConfig.maxAttempts,retryConfig.maxBackoff)' | grep -q '^30' \
  || { echo "queue retry settings not applied"; exit 1; }

# Two runtime identities. The worker can read memories, never write them.
gcloud iam service-accounts create ledger-worker --project="$PROJECT" || true
gcloud iam service-accounts create memory-publisher --project="$PROJECT" || true
gcloud iam roles create ledgerSessionsWorker --project="$PROJECT" --file=infra/iam/sessions_worker_role.yaml || true
gcloud iam roles create memoryPublisher --project="$PROJECT" --file=infra/iam/memory_publisher_role.yaml || true
for role in roles/datastore.user roles/bigquery.jobUser roles/cloudtasks.enqueuer \
            roles/aiplatform.memoryViewer "projects/${PROJECT}/roles/ledgerSessionsWorker"; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${WORKER}" --role="$role" --condition=None -q >/dev/null
done
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${PUBLISHER}" \
  --role="projects/${PROJECT}/roles/memoryPublisher" --condition=None -q >/dev/null
bq --project_id="$PROJECT" query --use_legacy_sql=false \
  "GRANT \`roles/bigquery.dataEditor\` ON SCHEMA \`${PROJECT}.${DATASET}\` TO \"serviceAccount:${WORKER}\""
# The worker signs OIDC tokens for its own wake-up tasks, and nothing else.
gcloud iam service-accounts add-iam-policy-binding "$WORKER" --member="serviceAccount:${WORKER}" \
  --role=roles/iam.serviceAccountUser --project="$PROJECT" -q >/dev/null

# Sessions and Memory Bank without deploying agent code: an Agent Runtime
# instance with no code, Memory Bank on a multilingual embedding model
# (the default would be text-embedding-005).
python - <<PY
import vertexai
c = vertexai.Client(project="$PROJECT", location="$REGION")
e = c.agent_engines.create(config={
    "display_name": "mission-ledger-sessions",
    "context_spec": {"memory_bank_config": {"similarity_search_config": {
        "embedding_model": "projects/$PROJECT/locations/$REGION/publishers/google/models/gemini-embedding-001"}}}})
print("ENGINE_ID=" + e.api_resource.name.rsplit("/", 1)[-1])
PY

