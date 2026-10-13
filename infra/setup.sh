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
  aiplatform.googleapis.com iam.googleapis.com --project="$PROJECT"

gcloud firestore databases create --location="$REGION" --type=firestore-native --project="$PROJECT" || true
bq --project_id="$PROJECT" mk --location=US -d "$DATASET" || true
sed "s/\${DATASET}/${PROJECT}.${DATASET}/g" infra/bigquery/tables.sql | bq --project_id="$PROJECT" query --use_legacy_sql=false
gcloud tasks queues create mission-wakeups --location="$REGION" --project="$PROJECT" \
  --max-attempts=5 --min-backoff=5s --max-backoff=60s || true

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

# Sessions without deploying agent code: an Agent Runtime instance with no code.
# python -c "import vertexai; print(vertexai.Client(project='$PROJECT', location='$REGION').agent_engines.create(config={'display_name':'mission-ledger-sessions'}).api_resource.name)"
