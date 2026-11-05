#!/usr/bin/env bash
# Kill the payout worker while its provider call is in flight, then prove recovery.
#
# 1. SIGSTOP the provider simulator. The kernel still accepts the worker's connection
#    and buffers the PUT, but nothing answers.
# 2. Admit a payout. The worker commits MAY_HAVE_SENT, then sends PUT and waits.
# 3. SIGKILL the worker mid-call. SIGCONT the provider: it reads the buffered PUT and
#    pays, but the worker that sent it is gone ("provider pays, then the worker dies").
# 4. kubelet restarts the worker. After the 30 s claim lease it recovers with LOOKUP
#    under the same provider key and settles. Expect exactly one provider effect.
set -euo pipefail
NS=${NS:-ledgerops}
NODE=${NODE:-ledgerops-control-plane}
KUBECTL=${KUBECTL:-kubectl}

resume() { docker exec "$NODE" pkill -CONT -f 'provider_sim --database' || true; }
trap resume EXIT

pod=$($KUBECTL -n "$NS" get pod -l app=dispatch -o jsonpath='{.items[0].metadata.name}')
before=$($KUBECTL -n "$NS" get pod "$pod" \
  -o jsonpath='{.status.containerStatuses[?(@.name=="worker")].restartCount}')

docker exec "$NODE" pkill -STOP -f 'provider_sim --database'
echo "provider: SIGSTOP"
started=$($KUBECTL -n "$NS" exec deploy/synthetic-probe -- python /opt/probe/drill.py start)
echo "admitted: $started"
payout=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["payout_id"])' "$started")

sleep 2  # worker polls every 500 ms; its PUT now waits on the stopped provider (5 s timeout)
docker exec "$NODE" pkill -KILL -f 'python -m ledgerops.worker'
echo "worker: SIGKILL during the in-flight PUT"
resume
trap - EXIT
echo "provider: SIGCONT"

after=$before
for _ in $(seq 60); do  # kubelet notices the exit and restarts the container
  after=$($KUBECTL -n "$NS" get pod "$pod" \
    -o jsonpath='{.status.containerStatuses[?(@.name=="worker")].restartCount}')
  [ "$after" -gt "$before" ] && break
  sleep 1
done
echo "worker restarts: $before -> $after"

settled=$($KUBECTL -n "$NS" exec deploy/synthetic-probe -- python /opt/probe/drill.py wait "$payout")
echo "settled: $settled"

effects=$($KUBECTL -n "$NS" exec "$pod" -c provider -- python -c '
import sqlite3, sys
c = sqlite3.connect("file:/var/lib/ledgerops-provider/provider.sqlite3?mode=ro", uri=True)
key = "%." + sys.argv[1]
print(c.execute("SELECT count(*) FROM executions WHERE provider_key LIKE ?", (key,)).fetchone()[0],
      c.execute("SELECT count(*) FROM effects WHERE provider_key LIKE ?", (key,)).fetchone()[0])
' "$payout")
echo "provider executions/effects for this payout: $effects"

attempts=$($KUBECTL -n "$NS" exec postgres-0 -- sh -c \
  'PGPASSWORD="$(cat /run/postgres-secret/password)" psql -h 127.0.0.1 -U postgres -d ledgerops -At -F " " -c "SELECT action, outcome, coalesce(http_status::text, '"'-'"'), to_char(started_at, '"'HH24:MI:SS'"') FROM ledger.transport_attempt WHERE payout_id = '"'$payout'"' ORDER BY started_at"')
echo "transport attempts (action outcome http started), oldest first:"
echo "$attempts" | sed 's/^/  /'

[ "$effects" = "1 1" ] || { echo "FAIL: expected exactly one provider execution and effect"; exit 1; }
[ "$after" -gt "$before" ] || { echo "FAIL: worker was not restarted"; exit 1; }
echo "$attempts" | head -1 | grep -q '^PUT MAY_HAVE_SENT' \
  || { echo "FAIL: the kill did not land during the PUT"; exit 1; }
python3 -c 'import json,sys; s=json.loads(sys.argv[1]); sys.exit(not (s["knowledge"]=="SUCCEEDED" and s["reservation_state"]=="CONSUMED"))' "$settled" \
  || { echo "FAIL: payout did not settle as SUCCEEDED/CONSUMED"; exit 1; }
echo "OK: killed mid-PUT; recovered by LOOKUP with the same key; one provider effect"
