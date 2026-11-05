"""Payout drill client; runs inside the synthetic-probe Pod (CA + seed output mounted).

  python /opt/probe/drill.py start       purchase 20, then payout 10 + fee 2; print payout id
  python /opt/probe/drill.py wait ID     poll until the provider outcome is terminal

Stdlib only. Tokens are read from the seed output and never printed.
"""

import http.client
import json
import ssl
import sys
import time
from uuid import uuid4

SEED = "/run/bootstrap/acceptance.json"
TERMINAL = {"SUCCEEDED", "FAILED", "CLOSED_ABSENT"}  # migration 007; anything else is pending


def request(method, path, token, payload=None):
    context = ssl.create_default_context(cafile="/run/ca/ca.crt")
    conn = http.client.HTTPSConnection("api.ledgerops.svc", 8443, context=context, timeout=10)
    headers = {"Authorization": "Bearer " + token, "Connection": "close"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
        headers["Idempotency-Key"] = "drill-" + uuid4().hex
    conn.request(method, path, body, headers)
    response = conn.getresponse()
    status, raw = response.status, response.read()
    conn.close()
    return status, json.loads(raw) if raw else None


def merchant_balance(state):
    _, accounts = request("GET", "/v1/accounts", state["tokens"]["merchant"])
    account = accounts["accounts"][0]
    return {name: account[name] for name in ("booked", "reserved", "available")}


def start(state):
    status, _ = request(
        "POST",
        "/v1/purchases",
        state["tokens"]["customer"],
        {
            "operation_id": str(uuid4()),
            "customer_account_id": state["customer_account"],
            "merchant_account_id": state["merchant_account"],
            "amount_minor": "20",
            "currency": "PLN",
        },
    )
    if status != 201:
        raise SystemExit(f"purchase returned {status} (customer funds exhausted after 5 drills?)")
    payout_id = str(uuid4())
    status, _ = request(
        "POST",
        "/v1/payouts",
        state["tokens"]["merchant"],
        {
            "operation_id": payout_id,
            "merchant_account_id": state["merchant_account"],
            "beneficiary_id": state["beneficiary_id"],
            "provider_namespace": state["namespace"],
            "principal_minor": "10",
            "fee_minor": "2",
            "currency": "PLN",
            "contract_version": 1,
        },
    )
    if status != 202:
        raise SystemExit(f"payout returned {status}")
    print(json.dumps({"payout_id": payout_id, "merchant_after_admission": merchant_balance(state)}))


def wait(state, payout_id, timeout=180):
    started = time.monotonic()
    while True:
        status, body = request("GET", "/v1/payouts/" + payout_id, state["tokens"]["merchant"])
        if status == 200 and body["knowledge"] in TERMINAL:
            break
        if time.monotonic() - started > timeout:
            raise SystemExit(f"payout still pending after {timeout}s: {status} {body}")
        time.sleep(1)
    print(
        json.dumps(
            {
                "payout_id": payout_id,
                "knowledge": body["knowledge"],
                "reservation_state": body["reservation_state"],
                "attempts": body["attempts"],
                "seconds_waited": round(time.monotonic() - started, 1),
                "merchant_after_settlement": merchant_balance(state),
            }
        )
    )


def main():
    with open(SEED, "rb") as stream:
        state = json.load(stream)
    if sys.argv[1:2] == ["start"]:
        start(state)
    elif sys.argv[1:2] == ["wait"] and len(sys.argv) == 3:
        wait(state, sys.argv[2])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
