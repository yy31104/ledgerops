CREATE SCHEMA IF NOT EXISTS assistant_workflow;
CREATE TABLE IF NOT EXISTS assistant_workflow.runs (
    thread_key text PRIMARY KEY,
    request_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS assistant_workflow.drafts (
    owner_key text NOT NULL,
    request_id uuid NOT NULL,
    inputs_sha256 text NOT NULL,
    draft_sha256 text NOT NULL,
    action_id uuid NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY(owner_key, request_id)
);
