"""Read-only synthetic API for the local demo. This is not a financial service."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LIMIT = "40000000-0000-4000-8000-000000000004"
TIMEOUT = "40000000-0000-4000-8000-000000000003"
BLOCKER = "40000000-0000-4000-8000-000000000006"
OBSERVATION = "40000000-0000-4000-8000-000000000007"
RUN = "40000000-0000-4000-8000-000000000008"


def observation_case(identifier):
    terminal = identifier == BLOCKER
    payout = {
        "operation_id": identifier,
        "knowledge": "SUCCEEDED" if terminal else "UNKNOWN",
        "reservation_state": "HELD",
        "principal_minor": "60",
        "fee_minor": "2",
        "gross_minor": "62",
        "attempts": "1",
        "version": "2",
    }
    return {
        "payout": payout,
        "recent_observations": [{"observation_id": OBSERVATION, "state": "SUCCEEDED"}]
        if terminal
        else [],
        "recent_incidents": [
            {
                "category": "LOCAL_BLOCKER",
                "code": "SYNTHETIC_REPAIRED_FAULT",
                "observation_id": OBSERVATION,
            }
        ]
        if terminal
        else [],
        "current_reconciliation_references": [{"run_id": RUN, "evidence_epoch": "7"}]
        if terminal
        else [],
        "observations_have_more": False,
        "incidents_have_more": False,
        "reconciliations_have_more": False,
        "evidence_boundary": "Synthetic references, not signed provider verification",
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health/live":
            return self.respond(200, {"status": "synthetic_demo"})
        token = os.environ.get("LEDGEROPS_ASSISTANT_DEMO_TOKEN")
        if not token or self.headers.get("Authorization") != "Bearer " + token:
            return self.respond(401, {"error": "authentication_required"})
        if self.path == "/v1/me":
            return self.respond(
                200,
                {
                    "workspace_id": "00000000-0000-0000-0000-000000000001",
                    "actor_id": "00000000-0000-0000-0000-000000000002",
                    "client_id": "00000000-0000-0000-0000-000000000003",
                    "scopes": [
                        "payout:read",
                        "operations:read",
                        "evidence:read",
                        "accounts:read",
                        "approval:propose",
                    ],
                },
            )
        if self.path == "/v1/accounts":
            return self.respond(
                200,
                {
                    "accounts": [
                        {"booked_minor": "80", "reserved_minor": "62", "available_minor": "18"}
                    ],
                    "next_after": None,
                },
            )
        if self.path == "/v1/metrics":
            return self.respond(200, {"unresolved_count": 1, "blocked_count": 1})
        identifier = self.path.rsplit("/", 1)[-1]
        if identifier not in (LIMIT, TIMEOUT, BLOCKER):
            return self.respond(404, {"error": "not_available"})
        if self.path.startswith("/v1/operations/"):
            return self.respond(
                200,
                {
                    "operation_id": identifier,
                    "http_status": 422 if identifier == LIMIT else 202,
                    "response_body": {"error": {"code": "LIMIT_EXCEEDED"}}
                    if identifier == LIMIT
                    else {"status": "accepted"},
                },
            )
        if self.path.startswith("/v1/payouts/"):
            return (
                self.respond(404, {"error": "not_available"})
                if identifier == LIMIT
                else self.respond(200, observation_case(identifier)["payout"])
            )
        if self.path.startswith("/v1/investigation/operations/"):
            return self.respond(200, observation_case(identifier))
        return self.respond(404, {"error": "not_available"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
