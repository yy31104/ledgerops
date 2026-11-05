# Kubernetes on kind

A local, synthetic deployment of the API, payout worker, provider simulator and
PostgreSQL on [kind](https://kind.sigs.k8s.io/), with Prometheus, Grafana, one
latency/error dashboard and a triggerable alert. Nothing here touches a cloud
account, and no secret is committed: `scripts/generate_secrets.py` generates random values
and pipes them straight into `kubectl apply`.

Files live in [`deploy/k8s/`](../deploy/k8s). Run targets from the repository root
with `make -C deploy/k8s <target>`.

## Layout

| Workload | Kind | Containers | Probes | Requests / limits |
|---|---|---|---|---|
| `postgres` | StatefulSet + 1 Gi PVC | postgres (same digest as `ops/linux`) | `pg_isready` readiness + liveness | 250m/192Mi – 1/256Mi |
| `bootstrap` | Job | copy-secrets → provision → seed | — | 100m/128Mi – 1/512Mi |
| `api` | Deployment ×2 | api (Gunicorn on :8080) + tls-proxy (Caddy :8443, metrics :9180) | startup/liveness `/health/live`, readiness `/health/ready` | api 100m/128Mi – 500m/192Mi |
| `dispatch` | Deployment ×1, `Recreate` | provider + worker in one Pod, provider SQLite on PVC | provider TCP; worker process liveness | 50m/64Mi – 500m/192Mi each |
| `synthetic-probe` | Deployment | stdlib client calling the API over TLS, exports metrics on :9100 | `/metrics` | 10m/32Mi – 200m/64Mi |
| `prometheus`, `grafana` | Deployments | upstream images | `/-/ready`, `/api/health` | see manifests |

Why these shapes:

- **Provider and worker share a Pod.** The simulator binds `127.0.0.1:8765` and checks
  `Host`, so the worker must share its network namespace. This is the Kubernetes form of
  `network_mode: service:provider` in `ops/linux/compose.yaml`. `Recreate` guarantees
  there are never two workers or two SQLite writers.
- **Secrets are copied by an init container.** The launcher only accepts mode-0600 files
  owned by its own UID; Secret volumes are root-owned. `copy-secrets` copies them into a
  memory-backed `emptyDir` as UID 10001.
- **TLS terminates in a Caddy sidecar.** Production mode requires HTTPS from a trusted
  proxy, and trusted proxies are exact IPs, so the proxy lives at `127.0.0.1` in the same
  Pod. Caddy also provides the request latency histogram (no app change needed).
- **Liveness never touches PostgreSQL**; readiness does. A database outage takes API Pods
  out of the Service instead of restart-looping them.

## Run

Needs Docker, kind, kubectl, uv, openssl.

```bash
make -C deploy/k8s up
```

`up` = `cluster images secrets deploy bootstrap status`. `images` reuses
`ops/linux/stack.py build` and `ops/linux/Dockerfile` unchanged, then `kind load`s the
runtime and proxy images. `secrets` refuses to overwrite an existing set, so a re-run
never rotates passwords under a populated database volume.

```bash
make -C deploy/k8s dashboards
```

Grafana: <http://127.0.0.1:3000> → dashboard **LedgerOps API: latency and errors**.
Prometheus: <http://127.0.0.1:9090/alerts>.

## Metrics, dashboard and alerts

| Signal | Source | Query |
|---|---|---|
| Server latency p50/p95/p99 | Caddy sidecar | `histogram_quantile(0.95, sum by (le) (rate(caddy_http_request_duration_seconds_bucket[1m])))` |
| Server error rate | Caddy sidecar | 5xx / all of `caddy_http_request_duration_seconds_count` |
| Client latency p95 | synthetic probe | `ledgerops_probe_duration_seconds` |
| Client error rate | synthetic probe | (`5xx` + `connect_error`) / all of `ledgerops_probe_requests_total` |

Alerts (`monitoring/alerts.yml`):

- **LedgerOpsApiErrorRateHigh**: client error ratio > 5 % for 30 s. Uses the client
  view on purpose: when PostgreSQL is gone, readiness removes every API Pod from the
  Service, so the server-side proxies see *no* traffic and a server-only ratio is `0/0`.
- **LedgerOpsApiLatencyHigh**: server p95 > 500 ms for 2 min.

## Fault injection and recovery

### Fault 1: kill the worker

```bash
make -C deploy/k8s fault-kill-worker
```

Sends `SIGKILL` to only the worker process from the kind node, leaving the provider
running. Expected: the worker's `restartCount` increments; kubelet restarts it; the
provider's SQLite history is untouched. Any payout that was `MAY_HAVE_SENT` stays
reserved and the restarted worker queries the provider by the original payment key
instead of sending a new payment (same behaviour as
`tests/integration/test_payout_worker_process.py`). For a full Pod loss, use
`kubectl -n ledgerops delete pod -l app=dispatch`: the PVC brings the provider history
back.

Recovery: automatic. Verify with `kubectl -n ledgerops logs deploy/dispatch -c worker`
and look for a fresh start rather than a restart loop. A loop with exit code 2 is the
worker's fail-stop on an invariant violation; follow "Worker fail-stop" in
[`ops/linux/README.md`](../ops/linux/README.md) and do **not** delete holds to clear it.

### Fault 2: disconnect the database

```bash
make -C deploy/k8s fault-db-down
```

Expected timeline:

1. In-flight API calls return `503 RETRY_WITH_SAME_BUSINESS_IDENTITY`.
2. Within ~10 s (`periodSeconds 5 × failureThreshold 2`) readiness fails, API Pods
   leave the Service; liveness keeps passing, so nothing restarts.
3. The probe records `connect_error`; **LedgerOpsApiErrorRateHigh** goes pending, then
   firing after 30 s (`make -C deploy/k8s alert-status`).
4. The worker logs `worker_database_error` and keeps retrying; reserved money stays
   reserved.

Recovery:

```bash
make -C deploy/k8s fault-db-restore
```

PostgreSQL comes back on the same PVC, readiness passes, Pods rejoin the Service and
the alert resolves after the error ratio drops.

## Teardown

```bash
make -C deploy/k8s down
```

Deletes the kind cluster, including PVCs and Secrets.

## Limits

- Not executed end-to-end in the authoring environment (no Docker daemon or kind
  there); see the PR for what was checked.
- Prometheus/Grafana images are pinned by tag, not digest.
- No Alertmanager: alerts are visible in Prometheus only.
- The synthetic probe only issues authenticated reads; it does not create payouts.
- Single-node kind, one PostgreSQL replica, no backups; not an HA design.
