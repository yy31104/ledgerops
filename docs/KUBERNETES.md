# Kubernetes on kind

A local, synthetic deployment of the API, payout worker, provider simulator and
PostgreSQL on [kind](https://kind.sigs.k8s.io/), with Prometheus, Grafana, one
latency/error dashboard, a triggerable alert, two fault drills and an independent
ledger check. Nothing touches a cloud account. No secret is committed:
`scripts/generate_secrets.py` creates random values and pipes them straight into
`kubectl apply`.

Files live in [`deploy/k8s/`](../deploy/k8s). Run targets from the repository root
with `make -C deploy/k8s <target>`. Everything below was run on 2026-11-05 with kind
v0.30.0 (Kubernetes v1.34.0), kubectl v1.34.1 and Docker 29.9 on a 4-vCPU Linux VM.
The numbers are from that run, not estimates.

## Layout

| Workload | Kind | Containers | Probes | Requests / limits |
|---|---|---|---|---|
| `postgres` | StatefulSet + 1 Gi PVC | PostgreSQL 18.6 (same digest as `ops/linux`) | `pg_isready` readiness + liveness | 250m/192Mi – 1/256Mi |
| `bootstrap` | Job | copy-secrets → `bootstrap provision` → `bootstrap seed` | — | 100m/128Mi – 1/512Mi |
| `api` | Deployment ×2 | api (Gunicorn :8080) + tls-proxy (Caddy :8443, metrics :9180) | startup/liveness `/health/live`, readiness `/health/ready` | api 100m/128Mi – 500m/192Mi; proxy 25m/32Mi – 250m/96Mi |
| `dispatch` | Deployment ×1, `Recreate` | provider + worker in one Pod; provider SQLite on a PVC | provider: in-Pod TCP connect; worker: process alive | 50m/64Mi – 500m/192Mi each |
| `synthetic-probe` | Deployment | stdlib client calling the API over TLS; exports metrics on :9100 | `/metrics` | 10m/32Mi – 200m/64Mi |
| `prometheus`, `grafana` | Deployments | upstream images, digest-pinned | `/-/ready`, `/api/health` | 50m/128Mi – 500m/512Mi, 50m/128Mi – 500m/256Mi |
| `verify` | Job, on demand (`make verify`) | `bootstrap inspect` | — | 100m/128Mi – 1/512Mi |

The namespace enforces the `restricted` Pod Security Standard. Every container runs
as non-root, drops all capabilities and disallows privilege escalation. All
LedgerOps containers also use a read-only root filesystem.

Why these shapes:

- **Provider and worker share a Pod.** The simulator binds `127.0.0.1:8765` and checks
  `Host`, so the worker must share its network namespace. This is the Kubernetes form of
  `network_mode: service:provider` in `ops/linux/compose.yaml`. `Recreate` ensures
  there are never two workers or two SQLite writers. The provider's probes run *inside*
  the Pod: a `tcpSocket` probe with `host: 127.0.0.1` would test the node's loopback.
- **Secrets are copied by an init container.** The launcher only accepts mode-0600 files
  owned by its own UID, and Secret volumes are root-owned. `copy-secrets` copies them
  into a memory-backed `emptyDir` as UID 10001.
- **TLS terminates in a Caddy sidecar.** Production mode requires HTTPS from a trusted
  proxy, and trusted proxies are exact IPs, so the proxy lives at `127.0.0.1` in the
  same Pod. Caddy also exports the request latency histogram, so the application
  needed no `/metrics` change.
- **Liveness never touches PostgreSQL; readiness does.** A database outage takes API
  Pods out of the Service instead of restart-looping them (measured below: 0 restarts).

## Run

Needs Docker, kind ≥ 0.30, kubectl, uv, openssl and python3. `validate` also needs
kubeconform.

```bash
make -C deploy/k8s up
```

`up` runs `cluster preload images secrets deploy bootstrap ready status`. The measured
time from nothing to all workloads ready was **138 s**.

- `preload` pulls every third-party image **by digest** on the host, tags it with the
  name the manifests use (all `imagePullPolicy: Never`) and imports it into the node.
  The cluster never pulls from a registry.
- `images` builds the `runtime` and `proxy` targets of the unchanged
  `ops/linux/Dockerfile` from the context `ops/linux/stack.py` prepares: the committed
  revision via `git archive` when the tree is clean.
- `secrets` refuses to overwrite an existing set, so a re-run never rotates passwords
  under a populated database volume.

```bash
make -C deploy/k8s validate
```

This renders the kustomization and checks all 22 resources plus `jobs/verify.yaml`
against the Kubernetes schemas. Result: valid 22/22 and 1/1.

```bash
make -C deploy/k8s dashboards
```

Grafana: <http://127.0.0.1:3000>, dashboard **LedgerOps API: latency and errors**.
Prometheus: <http://127.0.0.1:9090/alerts>.

### Environment notes (found by actually running it)

| Symptom | Cause | Handling |
|---|---|---|
| `kind create cluster` fails; every control-plane sandbox reports `runc create failed: … can't get final child's PID from pipe: EOF` | The host cannot lower `oom_score_adj` (no `CAP_SYS_RESOURCE`, as in rootless Docker and some sandboxed VMs), and kubelet asks for -998 | `kind-config.yaml` sets containerd `restrict_oom_score_adj = true`. On ordinary hosts nothing changes |
| `kind load docker-image` fails with `content digest … not found` | Docker's containerd image store keeps only this platform's layers, and kind imports with `--all-platforms` | The Makefile imports with `docker save --platform … \| ctr import --platform …` |
| `pip` in `docker build` fails with `CERTIFICATE_VERIFY_FAILED` | The network intercepts TLS (corporate or sandbox proxy) | `make up EXTRA_CA_CERT=/path/ca.pem`. Only the temporary build context's Dockerfile copy is rewritten, and pip reads the CA through a BuildKit secret mount, so no CA lands in any image layer, ENV or label |
| The worker restarts twice on first boot | It exits (code 2, `worker_database_error`) when it has *never* connected, until `bootstrap provision` creates its role | Expected. Once it has been ready, it reconnects without exiting (see Fault 2) |

## Metrics, dashboard and alerts

| Signal | Source | Query |
|---|---|---|
| Server latency p50/p95/p99 | Caddy sidecar | `histogram_quantile(0.95, sum by (le) (rate(caddy_http_request_duration_seconds_bucket[1m])))` |
| Server error rate | Caddy sidecar | `(sum(rate(…_count{code=~"5.."}[1m])) or vector(0)) / sum(rate(…_count[1m]))` |
| Client latency p95 | synthetic probe | `histogram_quantile(0.95, sum by (le, route) (rate(ledgerops_probe_duration_seconds_bucket[1m])))` |
| Client error rate | synthetic probe | (`5xx` + `connect_error`) / all of `ledgerops_probe_requests_total` |

At steady state the probe makes about 3.5 requests/s. Observed server latency was
p50 25 ms and p95 48 ms.

Alerts (`monitoring/alerts.yml`):

- **LedgerOpsApiErrorRateHigh**: client error ratio > 5 % for 30 s. It uses the client
  view on purpose. When PostgreSQL is gone, readiness removes every API Pod from the
  Service, so the server-side proxies see *no* traffic. A server-only ratio would be
  `0/0` and could never fire.
- **LedgerOpsApiLatencyHigh**: server p95 > 500 ms for 2 min.

## Fault injection and recovery

### Fault 1: kill the worker in the middle of a provider call

```bash
make -C deploy/k8s fault-kill-worker
```

`scripts/fault-kill-worker.sh` makes the README's "provider pays, then the worker is
killed" case happen for real on Kubernetes:

1. `SIGSTOP` the provider simulator (from the kind node). The kernel still accepts the
   worker's connection and buffers its request.
2. Admit a payout of 10 + fee 2 through the API. The worker commits a `MAY_HAVE_SENT`
   attempt, then sends PUT and waits.
3. `SIGKILL` only the worker process, then `SIGCONT` the provider. The provider reads the
   buffered PUT and **pays**, but the worker that sent it is gone.
4. kubelet restarts the worker. After the 30 s claim lease it asks the provider
   (`LOOKUP`) under the **same provider key**.

The script fails unless all of these hold: the first attempt is still `PUT
MAY_HAVE_SENT`, the worker's `restartCount` went up, the payout ends `SUCCEEDED` with
the hold `CONSUMED`, and the provider's SQLite has exactly one execution and one
effect for that key.

Observed (fresh cluster):

```text
admitted: merchant booked 20 / reserved 12 / available 8
worker: SIGKILL during the in-flight PUT
worker restarts: 2 -> 3
transport attempts:
  PUT     MAY_HAVE_SENT  -    02:08:50
  LOOKUP  RESPONSE       200  02:09:26
settled: knowledge SUCCEEDED, reservation CONSUMED, merchant 8 / 0 / 8
provider executions/effects for this payout: 1 1
```

Recovery time is the larger of the 30 s claim lease and kubelet's restart back-off.
Killing the worker repeatedly within 10 minutes grows the back-off (10 s, 20 s, 40 s…);
a third kill in a row recovered after 46 s. A real restart loop with exit code 2 and
`worker_invariant_error` is the worker's fail-stop: follow "Worker fail-stop" in
[`ops/linux/README.md`](../ops/linux/README.md) and do **not** delete holds to clear it.

### Fault 2: disconnect the database

```bash
make -C deploy/k8s fault-db-down
```

This scales PostgreSQL to zero; the data PVC is kept. Recovery:

```bash
make -C deploy/k8s fault-db-restore
```

Observed on a fresh cluster (fault at 02:09:47, restore at 02:11:28):

| Time | After | Observed |
|---|---:|---|
| 02:09:51 | fault +4 s | PostgreSQL Pod gone; in-flight API calls return 503 |
| 02:10:01 | fault +14 s | `LedgerOpsApiErrorRateHigh` **pending** |
| 02:10:02 | fault +15 s | Both API Pods NotReady; probe now sees `connect_error`. **0 API restarts** |
| 02:10:36 | fault +49 s | Alert **firing** (`make -C deploy/k8s alert-status`) |
| 02:11:33 | restore +5 s | PostgreSQL running on the same PVC |
| 02:11:39 | restore +11 s | Both API Pods Ready |
| 02:12:25 | restore +57 s | Alert **inactive** once the 1-minute rate window drains |

During the outage the worker logged `worker_database_error` about once a second and
reconnected by itself afterwards: no restart, the next payout settled on its first
attempt. An earlier run gave the same shape: pending +23 s, firing +54 s, Ready
+5 s, resolved +63 s.

### After the faults: independent verification

```bash
make -C deploy/k8s verify
```

This runs `ops/linux/bootstrap.py inspect` as a one-off Job. It exports the ledger as
the read-only `ledger_evidence_reader` role, then verifies structure, balances, holds
and request intent from that export alone. After both drills:

```text
{'healthy': True, 'verification_complete': True, 'structure_ok': True,
 'projection_ok': True, 'request_intent_ok': True, 'issue_count': 0}
```

## Teardown

```bash
make -C deploy/k8s down
```

This deletes the kind cluster, including PVCs and Secrets (11 s).

## Limits

- Single-node kind, one PostgreSQL replica, no backups and no Alertmanager (alerts are
  visible in Prometheus only). This is a local exercise, not an HA design.
- The customer is seeded with 100, and each `fault-kill-worker` run spends 20 of it,
  so a fresh cluster supports five runs.
- The fault drill reaches into the kind node (`docker exec … pkill`). It is a test
  harness, not something to run against a shared cluster.
- Prometheus keeps its TSDB in an `emptyDir`; a Prometheus restart loses history.
- `EXTRA_CA_CERT` rewrites the pip steps of the Dockerfile *copy* in the temporary
  build context. Without it the build is exactly `ops/linux/Dockerfile`.
