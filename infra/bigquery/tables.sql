-- Control plane: one row per business request, written by people or upstream jobs.
CREATE TABLE IF NOT EXISTS `${DATASET}.missions` (
  mission_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  kind STRING NOT NULL,          -- selects the model fixture in fixture mode
  prompt STRING NOT NULL,        -- the business need, as a prompt
  persona STRING NOT NULL,       -- personas/<name>.md
  skills ARRAY<STRING>,          -- skills/<name>/ folders
  contact_alias STRING NOT NULL, -- email, +E164 phone or @handle
  facts_json STRING,             -- the only facts outbound text may rely on
  requested_by STRING
);

-- Append-only reporting. Never read to decide whether an action may run.
CREATE TABLE IF NOT EXISTS `${DATASET}.mission_reports` (
  mission_id STRING NOT NULL,
  reported_at TIMESTAMP NOT NULL,
  status STRING NOT NULL,
  detail_json STRING,
  worker STRING,
  model_mode STRING
);

-- Counter-example only (scenario 6): an insert-only MERGE used as a lock.
CREATE TABLE IF NOT EXISTS `${DATASET}.naive_contact_claims` (
  contact_id STRING NOT NULL,
  mission_id STRING NOT NULL,
  claimed_at TIMESTAMP NOT NULL
);
