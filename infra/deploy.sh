#!/usr/bin/env bash
# Build from the committed tree and deploy the worker to Cloud Run.
# Usage: PROJECT=... ENGINE_ID=... WEBHOOK_SECRET=... ./infra/deploy.sh [extra --update-env-vars]
set -euo pipefail
: "${PROJECT:?}" "${ENGINE_ID:?}" "${WEBHOOK_SECRET:?}"
REGION="${REGION:-us-central1}"
WORKER="ledger-worker@${PROJECT}.iam.gserviceaccount.com"
git diff --quiet HEAD -- ledger skills personas fixtures || { echo "commit first"; exit 1; }
printf '{"git_commit": "%s", "built_at": "%s"}\n' "$(git rev-parse --short=12 HEAD)" "$(date -u +%FT%TZ)" > ledger/_build.json
gcloud run deploy ledger-worker --source . --region="$REGION" --project="$PROJECT" \
  --service-account="$WORKER" --no-allow-unauthenticated \
  --min-instances=0 --max-instances=3 --cpu=1 --memory=1Gi --concurrency=4 --timeout=300 \
  --set-env-vars="LEDGER_PROJECT=${PROJECT},LEDGER_LOCATION=${REGION},LEDGER_AGENT_ENGINE_ID=${ENGINE_ID},LEDGER_WORKER_SA=${WORKER},LEDGER_WEBHOOK_SECRET=${WEBHOOK_SECRET},GOOGLE_CLOUD_PROJECT=${PROJECT}" \
  "$@"
URL=$(gcloud run services describe ledger-worker --region="$REGION" --project="$PROJECT" --format='value(status.url)')
gcloud run services update ledger-worker --region="$REGION" --project="$PROJECT" --update-env-vars="LEDGER_WORKER_URL=${URL}" >/dev/null
gcloud run services add-iam-policy-binding ledger-worker --region="$REGION" --project="$PROJECT" \
  --member="serviceAccount:${WORKER}" --role=roles/run.invoker >/dev/null
echo "$URL"
