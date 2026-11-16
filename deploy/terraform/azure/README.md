# Azure rehearsal preparation

This configuration has not been deployed. It prepares an optional assistant for a
future, separately approved synthetic-data trial. No cloud cost has been measured.
Keep `assistant_image = null`, `assistant_service_enabled = false` and
`assistant_external_ingress = false` until that trial is approved. A budget alert
does not enforce a spending cap.

## Validate without an Azure account

```sh
terraform -chdir=deploy/terraform/azure fmt -check -recursive
terraform -chdir=deploy/terraform/azure init -backend=false
terraform -chdir=deploy/terraform/azure validate
```

These commands download providers and validate their schemas, without planning or
creating resources. Run `tflint` and `checkov -d deploy/terraform/azure` separately
when available. The checked-in module uses AzureRM 4.x; retain the generated lock
file in the private rehearsal workspace to reproduce provider selections.

## Two-stage assistant startup

Use an image built from `assistant/Dockerfile`, pinned by its `sha256` digest and
readable by Container Apps. Private registry authentication must be configured
beforehand; this module does not grant registry access. Keep image builds and
credentials out of Terraform variables and state where possible.

1. Review the existing core service bootstrap instructions in the Azure operations
   guide. The API remains in restore mode and worker dispatch remains disabled.
   The provider simulator uses ephemeral SQLite; it is unsuitable for real money.
2. Agree the subscription, region, maximum trial duration, spending limit and
   teardown owner before any `plan` or `apply`. Use a dedicated resource group and
   synthetic records. Add the deployer's current public IPv4 `/32` explicitly to
   `key_vault_deployer_ipv4_cidrs`; the default-deny Key Vault otherwise rejects
   secret writes. Do not open it to all addresses. Verify managed-identity secret
   retrieval from the chosen Container Apps network before proceeding.
3. Set `assistant_image` while leaving `assistant_service_enabled = false`.
   Review a saved Terraform plan, including PostgreSQL and Key Vault changes,
   before applying it. This creates a separate `ledgerops_assistant` database,
   enables the server's `vector` extension allowlist and creates a manual init
   job. It does not start the assistant service.
4. Check the installed extension version is at least 0.8.0, required by strict
   iterative HNSW scans. The server allowlist alone does not create the extension;
   initialization needs permission to run `CREATE EXTENSION vector` in the new
   database. Availability depends on the Azure PostgreSQL version and region.
5. Populate three Key Vault secrets out of band: `assistant-admin-database-url`,
   `assistant-runtime-database-url` and `assistant-runtime-password`. Both DSNs
   must point to `ledgerops_assistant`, use TLS verification (`sslmode=verify-full`)
   and the actual Azure server hostname. The owner DSN is used only for
   initialization. The runtime DSN uses `assistant_runtime`; URL-encode its
   password. Never point either DSN at the ledger database. The initializer
   identity can read only its owner DSN/password secrets; the runtime identity can
   read only its runtime DSN secret. Placeholder secret values deliberately do not
   work.
6. Run the manual job and retain its execution status and logs in a private
   evidence directory:

   ```sh
   az containerapp job start --resource-group "$TRIAL_RESOURCE_GROUP" \
     --name "$ASSISTANT_INIT_JOB"
   az containerapp job execution list --resource-group "$TRIAL_RESOURCE_GROUP" \
     --name "$ASSISTANT_INIT_JOB" --output json
   ```

   Confirm success and corpus/model identity before the second stage. The job
   installs the runtime role and indexes the corpus. No job runs automatically
   during Terraform application.
7. Review a second plan with `assistant_service_enabled = true`. Ingress remains
   internal unless explicitly enabled for the approved test. The assistant uses
   fake generation only and requires the caller's own LedgerOps token. It has no
   demonstration credential. Its startup may download BGE model weights to
   ephemeral `/tmp`; cold starts and outbound model access have not been tested
   on Azure. The separate database shares the PostgreSQL server with the ledger;
   it is not a separate infrastructure trust boundary.

The [Azure pgvector guide](https://learn.microsoft.com/en-us/azure/postgresql/extensions/how-to-use-pgvector)
describes extension activation. The [Container Apps jobs guide](https://learn.microsoft.com/en-us/azure/container-apps/jobs)
describes manual job execution and history.

## Read-only smoke evidence

Run from a machine that can reach both origins. Read the caller token into
`LEDGEROPS_OPERATOR_TOKEN` without putting it in shell history. The helper sends
six GET requests, refuses redirects and proxies, records only statuses/latencies,
and fails if authentication or health checks disagree with expectations. It does
not create a payout or call a model.

```sh
python3 assistant/scripts/cloud_trial.py smoke \
  --api-origin "$API_ORIGIN" --assistant-origin "$ASSISTANT_ORIGIN" \
  --output .cache/cloud-trial/smoke.json
```

This checks readiness, caller authentication, anonymous denial and absence of a
remote demo token. It is not a substitute for the local worker-crash scenario,
draft-resume acceptance test or a full cloud end-to-end trial. Preserve failed
runs too; output files are created exclusively to avoid overwriting evidence.

## Teardown and actual cost record

Before a future trial, obtain an estimate from the
[Azure pricing calculator](https://azure.microsoft.com/en-us/pricing/calculator/)
for the selected region: PostgreSQL compute/storage/backups, Container Apps
compute/job execution, Log Analytics ingestion/retention, Key Vault and outbound
traffic. Record trial duration and assumptions. Minimum database charges and
purge-protected Key Vault retention can outlive a short container execution.

At the agreed deadline, stop testing, save smoke/job evidence and a Terraform
state inventory privately, review a destroy plan and execute it only with the
owner's authorization:

```sh
terraform -chdir=deploy/terraform/azure plan -destroy -out=trial-destroy.tfplan
terraform -chdir=deploy/terraform/azure apply trial-destroy.tfplan
az resource list --resource-group "$TRIAL_RESOURCE_GROUP" --output json
```

Confirm no chargeable resources remain, including orphaned resources outside the
group and retained database backups. Key Vault soft deletion/purge protection may
prevent immediate name reuse. Do not commit state, saved plans, billing exports or
credentials. After the billing data settles, export the resource-group-filtered
actual cost and record the owner-verified total:

```sh
python3 assistant/scripts/cloud_trial.py record-cost \
  --amount "$ACTUAL_TOTAL" --currency "$BILLING_CURRENCY" \
  --resource-group "$TRIAL_RESOURCE_GROUP" --export "$PRIVATE_BILLING_EXPORT" \
  --output .cache/cloud-trial/cost.json
```

This hashes the supplied export and records the total without copying account
details. It does not query Azure billing or independently verify the total. Until
a trial and billing export exist, report actual spend as **not measured**.
