# Azure preparation

This recipe has not been deployed. Runtime behavior and cost are unmeasured.
The assistant is off by default: `assistant_image = null`,
`assistant_service_enabled = false` and `assistant_external_ingress = false`.
Use synthetic records. A budget alert is not a spending cap.

## Static checks

```sh
terraform -chdir=deploy/terraform/azure fmt -check -recursive
terraform -chdir=deploy/terraform/azure init -backend=false
terraform -chdir=deploy/terraform/azure validate
tflint --chdir=deploy/terraform/azure --recursive
uvx --from checkov==3.4.5 checkov --directory deploy/terraform/azure \
  --framework terraform --skip-download --download-external-modules false
```

These commands download providers and check schemas/configuration without creating
resources. The module uses AzureRM 4.x. Keep the generated lock file with the
private Terraform workspace to reproduce provider selections.

| Check | Recorded local result |
|---|---|
| Terraform 1.13.5, AzureRM 4.81.0, random 3.9.1 | Formatting and validation pass |
| TFLint 0.64.0, configured Terraform rules | No issues |
| Additional AzureRM ruleset 0.33.0 | Nine missing-`prevent_destroy` warnings by default; thirteen with assistant DB/secrets |
| Checkov 3.4.5 | Eight passes, zero failures, five skips, no parsing errors |

The lifecycle warnings cover disposable trial resources. `prevent_destroy` would
block their teardown. Persistent data needs retention, backups and deletion
protection. No lifecycle warning is suppressed.

The five Checkov exceptions cover Key Vault private access (`CKV_AZURE_189`,
`CKV2_AZURE_32`), PostgreSQL geo-backup/private access (`CKV_AZURE_136`,
`CKV2_AZURE_57`) and its Azure-services firewall rule (`CKV2_AZURE_26`). That
firewall rule permits traffic from Azure services beyond this subscription;
database authentication still applies. Default-deny Key Vault access does not
replace private networking.

Checkov does not expand every conditional resource or independently check the
assistant's Container Apps, jobs and identity permissions. A direct assistant
module scan has zero applicable checks. These static results establish no Azure
startup, networking or runtime-security result.

## Two-stage assistant startup

Build `assistant/Dockerfile` and pin the image digest. Container Apps must be able
to read the registry; this module does not configure registry authentication.
Keep credentials out of Terraform variables/state where possible.

1. Follow the [core bootstrap guide](../../../docs/internals/azure.md). The API
   stays in restore mode and worker dispatch stays disabled. The provider uses
   ephemeral SQLite and cannot hold real payment records safely.
2. Set the subscription, region, resource group, trial duration, budget and teardown
   deadline. Add the deployer's current IPv4 `/32` to
   `key_vault_deployer_ipv4_cidrs`; default-deny Key Vault rejects other sources.
   Check managed-identity secret retrieval through the chosen network.
3. Set `assistant_image`, keeping `assistant_service_enabled = false`. Review and
   save the plan before applying it. This creates a separate `ledgerops_assistant`
   database, enables the server's `vector` allowlist and creates a manual init job.
   It does not start the service.
4. Check that `vector` is available at version 0.8.0 or later for strict iterative
   HNSW scans. An allowlist does not install the extension; initialization needs
   `CREATE EXTENSION vector` permission in the new database. Availability depends
   on PostgreSQL version and region.
5. Set three Key Vault secrets outside Terraform: `assistant-admin-database-url`,
   `assistant-runtime-database-url` and `assistant-runtime-password`. DSNs must
   point to `ledgerops_assistant`, use `sslmode=verify-full` and the real server
   hostname. URL-encode passwords. The init identity reads only the owner
   DSN/password secrets; runtime reads only its `assistant_runtime` DSN secret.
   Never use the ledger database. Placeholder secrets do not work.
6. Start the manual job and save its execution status/logs privately:

   ```sh
   az containerapp job start --resource-group "$TRIAL_RESOURCE_GROUP" \
     --name "$ASSISTANT_INIT_JOB"
   az containerapp job execution list --resource-group "$TRIAL_RESOURCE_GROUP" \
     --name "$ASSISTANT_INIT_JOB" --output json
   ```

   Confirm success and the corpus/model identity. The job creates the runtime
   role and indexes documents; Terraform does not run it automatically.
7. Review a second plan with `assistant_service_enabled = true`. Ingress remains
   internal unless explicitly enabled. Generation is fake and callers supply
   their own LedgerOps token; no demo credential is configured. BGE weights may
   download to ephemeral `/tmp`. Azure cold starts and outbound model access are
   untested. The assistant has a separate logical database on the same server,
   not a separate infrastructure trust boundary.

References: [Azure pgvector](https://learn.microsoft.com/en-us/azure/postgresql/extensions/how-to-use-pgvector),
[manual Container Apps jobs](https://learn.microsoft.com/en-us/azure/container-apps/jobs).

## Read-only smoke test

From a machine that can reach both services, set `LEDGEROPS_OPERATOR_TOKEN`
without putting it in shell history:

```sh
python3 assistant/scripts/cloud_trial.py smoke \
  --api-origin "$API_ORIGIN" --assistant-origin "$ASSISTANT_ORIGIN" \
  --output .cache/cloud-trial/smoke.json
```

The helper sends six GET requests and refuses redirects/proxies. It checks health,
caller authentication, anonymous denial and the absence of a remote demo token.
It creates no payout and calls no model. Outputs are exclusive: failed runs remain
intact. A transport failure retains prior checks, null status, elapsed time and a
fixed error category; the command exits nonzero. Reports contain no URL, token or
raw exception text. These checks do not replace a worker-crash or draft-resume trial.

## Teardown and cost

Estimate the chosen region with the
[Azure calculator](https://azure.microsoft.com/en-us/pricing/calculator/): database
compute/storage/backups, apps/jobs, logs, Key Vault and traffic. Record duration and
assumptions. Database charges and purge-protected vault retention can outlive a
short container run.

At the deadline, save test evidence and a private state inventory. Review a destroy
plan before executing it:

```sh
terraform -chdir=deploy/terraform/azure plan -destroy -out=trial-destroy.tfplan
terraform -chdir=deploy/terraform/azure apply trial-destroy.tfplan
az resource list --resource-group "$TRIAL_RESOURCE_GROUP" --output json
```

Check for remaining chargeable resources and retained backups, including resources
outside the group. Vault soft deletion/purge protection can prevent name reuse.
Keep state, saved plans, billing exports and credentials out of Git.

After billing settles, export costs filtered to the trial resource group:

```sh
python3 assistant/scripts/cloud_trial.py record-cost \
  --amount "$ACTUAL_TOTAL" --currency "$BILLING_CURRENCY" \
  --resource-group "$TRIAL_RESOURCE_GROUP" --export "$PRIVATE_BILLING_EXPORT" \
  --output .cache/cloud-trial/cost.json
```

The helper records the supplied total and export hash; it does not query billing
or verify that total. Actual cost remains **unmeasured** until a trial and billing
export exist.
