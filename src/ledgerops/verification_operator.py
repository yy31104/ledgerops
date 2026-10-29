"""Independent v9 operator audit bindings; no credentials or trust keys are read.

These checks establish the exported structural relationships and opaque actor
separation. They cannot authenticate historical grants, execution permission or
provider HMACs, and never turn an export into authority to execute an action.
"""

import hashlib
import json
import re
from datetime import datetime
from uuid import UUID

_TYPE = "REPROCESS_TERMINAL_LOCAL_BLOCKER"
_TERMINAL = {"SUCCEEDED", "FAILED", "CLOSED_ABSENT"}
_INTENT_KEYS = {
    "action_type",
    "payout_id",
    "observation_id",
    "evidence_run_id",
    "reason",
    "ttl_seconds",
}
_PROPOSAL_KEYS = _INTENT_KEYS | {
    "workspace_id",
    "action_id",
    "schema_version",
    "created_at",
    "expires_at",
    "maker",
    "auth_generation",
    "evidence",
    "target",
    "observation",
    "observations",
    "incident_ids",
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _uuid(value):
    _require(type(value) is str and str(UUID(value)) == value, "canonical UUID required")
    return value


def _integer(value, minimum=0):
    _require(type(value) is int and minimum <= value <= 2**63 - 1, "bounded integer required")
    return value


def _time(value):
    _require(type(value) is str, "UTC timestamp required")
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    offset = instant.utcoffset()
    _require(offset is not None and offset.total_seconds() == 0, "UTC timestamp required")
    return instant


def _digest(value):
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "SHA-256 required")
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _json(row, name, maximum):
    encoded = row[name + "_hex"]
    _require(
        type(encoded) is str
        and 0 < len(encoded) <= 2 * maximum
        and re.fullmatch(r"(?:[0-9a-f]{2})+", encoded),
        "bounded canonical hex bytes required",
    )
    raw = bytes.fromhex(encoded)
    _require(
        hashlib.sha256(raw).hexdigest() == _digest(row[name + "_sha256"]),
        "operator canonical bytes digest mismatch",
    )

    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    _require(_canonical(value) == raw, "noncanonical operator JSON")
    return value


def _identities(row, names):
    for name in names:
        _uuid(row[name])


def _proposal(row, workspace, payouts, observations, incidents):
    intent = _json(row, "intent", 4096)
    proposal = _json(row, "proposal", 1048576)
    _require(
        type(intent) is dict and intent.keys() == _INTENT_KEYS, "invalid operator intent fields"
    )
    _require(
        type(proposal) is dict and proposal.keys() == _PROPOSAL_KEYS,
        "invalid operator proposal fields",
    )
    _require(
        type(proposal["schema_version"]) is int and proposal["schema_version"] == 1,
        "unsupported operator proposal schema",
    )
    _require(
        {key: proposal[key] for key in _INTENT_KEYS} == intent,
        "proposal differs from immutable intent",
    )
    _identities(
        row,
        (
            "workspace_id",
            "action_id",
            "payout_id",
            "observation_id",
            "evidence_run_id",
            "maker_actor_id",
            "maker_client_id",
            "auth_generation",
        ),
    )
    for name in (
        "workspace_id",
        "action_id",
        "payout_id",
        "observation_id",
        "evidence_run_id",
        "action_type",
    ):
        _require(row[name] == proposal[name], "proposal identity mismatch")
    _require(
        row["workspace_id"] == workspace and row["action_type"] == _TYPE, "unsupported typed action"
    )
    _require(row["payout_id"] in payouts, "action payout missing")
    reason = intent["reason"]
    _require(
        type(reason) is str and "\0" not in reason and 1 <= len(reason.encode()) <= 256,
        "bounded action reason required",
    )
    ttl = _integer(intent["ttl_seconds"], 1)
    _require(ttl <= 3600, "action expiry exceeds one hour")
    created, expires = _time(row["created_at"]), _time(row["expires_at"])
    _require(
        0 < (expires - created).total_seconds() == ttl
        and _time(proposal["created_at"]) == created
        and _time(proposal["expires_at"]) == expires,
        "proposal expiry binding mismatch",
    )
    maker = proposal["maker"]
    _require(
        type(maker) is dict
        and maker.keys() == {"actor_id", "client_id", "membership_version", "client_version"},
        "invalid maker fields",
    )
    for name in maker:
        _require(maker[name] == row["maker_" + name], "maker binding mismatch")
    _integer(maker["membership_version"], 1)
    _integer(maker["client_version"], 1)
    _require(
        proposal["auth_generation"] == row["auth_generation"], "auth generation binding mismatch"
    )
    evidence = proposal["evidence"]
    _require(
        type(evidence) is dict
        and evidence.keys() == {"evidence_epoch", "result_sha256", "ledger_data_sha256"},
        "invalid evidence bindings",
    )
    _integer(evidence["evidence_epoch"], 1)
    _digest(evidence["result_sha256"])
    _digest(evidence["ledger_data_sha256"])
    observation = observations.get(row["observation_id"])
    _require(
        observation is not None
        and observation["payout_id"] == row["payout_id"]
        and observation["kind"] == "execution"
        and observation["state"] in _TERMINAL,
        "action requires its retained terminal observation",
    )
    _require(
        proposal["observation"]
        == {name: observation[name] for name in ("observation_id", "envelope_sha256", "state")},
        "selected observation binding mismatch",
    )
    fingerprints = proposal["observations"]
    _require(
        type(fingerprints) is list and 1 <= len(fingerprints) <= 10000,
        "bounded observation fingerprints required",
    )
    ids = []
    for fingerprint in fingerprints:
        _require(
            type(fingerprint) is dict
            and fingerprint.keys() == {"observation_id", "envelope_sha256"},
            "invalid observation fingerprint",
        )
        identifier = _uuid(fingerprint["observation_id"])
        retained = observations.get(identifier)
        _require(
            retained is not None
            and retained["payout_id"] == row["payout_id"]
            and retained["envelope_sha256"] == _digest(fingerprint["envelope_sha256"]),
            "retained observation fingerprint mismatch",
        )
        ids.append(identifier)
    _require(
        ids == sorted(set(ids)) and row["observation_id"] in ids,
        "observation fingerprint coverage mismatch",
    )
    target = proposal["target"]
    _require(
        type(target) is dict
        and target.keys()
        == {
            "projection_epoch",
            "payout",
            "reservation",
            "accounts",
            "balances",
            "observations",
            "incidents",
            "related_incidents",
        },
        "invalid target binding fields",
    )
    _integer(target["projection_epoch"], 1)
    before = target["payout"]
    _require(
        type(before) is dict
        and before.keys() == payouts[row["payout_id"]].keys() - {"provider_request_hex"},
        "complete target payout required",
    )
    for name, value in before.items():
        if name not in {"knowledge", "version", "source_sequence"}:
            _require(payouts[row["payout_id"]].get(name) == value, "target payout intent mismatch")
    _integer(before["version"], 1)
    _integer(before["source_sequence"])
    _require(
        before["knowledge"] in {"NOT_SENT", "UNKNOWN", "ACCEPTED"}
        and before["version"] <= _integer(payouts[row["payout_id"]]["version"], 1),
        "target must capture unresolved prior payout knowledge and version",
    )
    _require(
        target["reservation"]["payout_id"] == row["payout_id"]
        and target["reservation"]["account_id"] == before["merchant_account_id"]
        and target["reservation"]["gross_minor"] == before["gross_minor"]
        and target["reservation"]["state"] == "HELD"
        and type(target["reservation"]["version"]) is int
        and target["reservation"]["version"] == 1,
        "target hold binding mismatch",
    )
    _require(target["observations"] == fingerprints, "source observation coverage mismatch")
    incident_ids = proposal["incident_ids"]
    _require(
        type(incident_ids) is list
        and 1 <= len(incident_ids) <= 10000
        and [_uuid(value) for value in incident_ids] == sorted(set(incident_ids)),
        "exact sorted incident identities required",
    )
    source_ids = []
    for source in target["incidents"]:
        identifier = source["incident_id"]
        _require(incidents.get(identifier) == source, "source incident binding mismatch")
        _require(
            source["payout_id"] == row["payout_id"]
            and source["category"] == "LOCAL_BLOCKER"
            and source["observation_id"] == row["observation_id"],
            "action cannot dispose a nonlocal or different-source incident",
        )
        source_ids.append(identifier)
    _require(
        source_ids == incident_ids and target["related_incidents"] == [],
        "source incident coverage mismatch",
    )
    _require(
        type(target["accounts"]) is list
        and type(target["balances"]) is list
        and len(target["accounts"]) == len(target["balances"]) == 2,
        "target account version coverage mismatch",
    )
    account_ids = [_uuid(value["id"]) for value in target["accounts"]]
    balance_ids = [_uuid(value["account_id"]) for value in target["balances"]]
    _require(
        account_ids == balance_ids == sorted(set(account_ids))
        and before["merchant_account_id"] in account_ids,
        "target account identity mismatch",
    )
    accounts = {value["id"]: value for value in target["accounts"]}
    _require(
        sorted(value["class"] for value in accounts.values())
        == ["MERCHANT_LIABILITY", "SYNTHETIC_CLEARING"],
        "target account class mismatch",
    )
    for value in target["balances"]:
        _integer(value["version"])
        account = accounts[value["account_id"]]
        _require(
            account["workspace_id"] == value["workspace_id"] == workspace
            and account["currency"] == "PLN"
            and value["normal_side"]
            == account["normal_side"]
            == ("D" if account["class"] == "SYNTHETIC_CLEARING" else "C"),
            "target account scope or normal side mismatch",
        )
        money = {}
        for name in ("debits", "credits", "reserved", "booked"):
            amount = value[name]
            _require(
                type(amount) is str
                and re.fullmatch(r"0|[1-9][0-9]{0,37}", amount)
                and int(amount) <= 10**38 - 1,
                "target money must be canonical",
            )
            money[name] = int(amount)
        booked = (
            money["debits"] - money["credits"]
            if account["normal_side"] == "D"
            else money["credits"] - money["debits"]
        )
        _require(
            money["booked"] == booked and money["reserved"] <= booked,
            "target balance projection differs from captured totals",
        )
        if account["id"] == before["merchant_account_id"]:
            _require(
                money["reserved"] >= int(before["gross_minor"]), "target active hold not reserved"
            )
    return proposal


def verify_operator_records(check, snapshot, workspace, payouts, observations, resolutions):
    """Return only incident IDs with a complete independently validated action chain."""
    incidents = check.index(
        check.rows(snapshot, "payout_incidents", workspace),
        "payout_incidents",
        lambda row: _uuid(row["incident_id"]),
    )
    actions = check.index(
        check.rows(snapshot, "operator_actions", workspace),
        "operator_actions",
        lambda row: _uuid(row["action_id"]),
    )
    approvals = check.index(
        check.rows(snapshot, "operator_approvals", workspace),
        "operator_approvals",
        lambda row: _uuid(row["action_id"]),
    )
    executions = check.index(
        check.rows(snapshot, "operator_executions", workspace),
        "operator_executions",
        lambda row: _uuid(row["action_id"]),
    )
    dispositions = check.index(
        check.rows(snapshot, "payout_incident_resolutions", workspace),
        "payout_incident_resolutions",
        lambda row: _uuid(row["incident_id"]),
    )
    proposals, valid_approvals, valid_executions = {}, set(), set()
    for identifier, row in actions.items():
        try:
            proposals[identifier] = _proposal(row, workspace, payouts, observations, incidents)
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
            check.issue(
                "structure", "INVALID_OPERATOR_ACTION", identifier, "proposal", str(exc), None
            )
    for identifier, row in approvals.items():
        try:
            action, proposal = actions.get(identifier), proposals.get(identifier)
            _require(
                action is not None and proposal is not None, "approval action missing or invalid"
            )
            _identities(row, ("checker_actor_id", "checker_client_id"))
            _integer(row["checker_membership_version"], 1)
            _integer(row["checker_client_version"], 1)
            _require(
                row["proposal_sha256"] == action["proposal_sha256"],
                "approval proposal hash mismatch",
            )
            _require(
                row["checker_actor_id"] != action["maker_actor_id"],
                "maker cannot approve own action",
            )
            _require(
                _time(action["created_at"])
                <= _time(row["approved_at"])
                < _time(action["expires_at"]),
                "approval outside proposal lifetime",
            )
            valid_approvals.add(identifier)
        except (KeyError, TypeError, ValueError) as exc:
            check.issue(
                "structure", "INVALID_OPERATOR_APPROVAL", identifier, "approval", str(exc), None
            )
    seen_payouts = set()
    for identifier, row in executions.items():
        try:
            action = actions.get(identifier)
            _require(
                identifier in valid_approvals and action is not None,
                "execution lacks valid approval",
            )
            _identities(
                row, ("execution_id", "executor_actor_id", "executor_client_id", "observation_id")
            )
            _require(
                row["proposal_sha256"] == action["proposal_sha256"],
                "execution proposal hash mismatch",
            )
            _require(
                _time(approvals[identifier]["approved_at"])
                <= _time(row["executed_at"])
                < _time(action["expires_at"]),
                "execution outside proposal lifetime",
            )
            payout = action["payout_id"]
            effect = resolutions.get(payout)
            _require(
                payouts[payout]["version"] > proposals[identifier]["target"]["payout"]["version"],
                "executed action must advance target payout version",
            )
            _require(payout not in seen_payouts, "more than one executed action for one payout")
            _require(
                effect is not None
                and row["observation_id"] == action["observation_id"]
                and row["state"] in _TERMINAL
                and all(
                    row[name] == effect[name] for name in ("observation_id", "state", "journal_id")
                ),
                "execution differs from retained financial effect",
            )
            _require(
                type(row["http_status"]) is int
                and row["http_status"] == 200
                and type(row["response_body"]) is dict,
                "execution result must be retained",
            )
            expected_body = {
                "action_id": identifier,
                "execution_id": row["execution_id"],
                "payout_id": payout,
                "observation_id": row["observation_id"],
                "proposal_sha256": action["proposal_sha256"],
                "state": row["state"],
                "journal_id": row["journal_id"],
                "resolved_incident_ids": proposals[identifier]["incident_ids"],
            }
            _require(
                row["response_body"] == expected_body,
                "execution retained response binding mismatch",
            )
            seen_payouts.add(payout)
            valid_executions.add(identifier)
        except (KeyError, TypeError, ValueError) as exc:
            check.issue(
                "structure", "INVALID_OPERATOR_EXECUTION", identifier, "execution", str(exc), None
            )
    valid_dispositions = set()
    by_action = {}
    for identifier, row in dispositions.items():
        try:
            action_id = _uuid(row["action_id"])
            action = actions.get(action_id)
            incident = incidents.get(identifier)
            _require(
                action_id in valid_executions and action is not None and incident is not None,
                "disposition lacks valid executed action or original incident",
            )
            _require(
                incident["category"] == "LOCAL_BLOCKER"
                and row["payout_id"] == incident["payout_id"] == action["payout_id"]
                and row["observation_id"] == incident["observation_id"] == action["observation_id"]
                and identifier in proposals[action_id]["incident_ids"],
                "disposition must bind the same terminal LOCAL_BLOCKER",
            )
            _require(
                _time(row["resolved_at"]) >= _time(executions[action_id]["executed_at"]),
                "disposition predates execution",
            )
            by_action.setdefault(action_id, set()).add(identifier)
            valid_dispositions.add(identifier)
        except (KeyError, TypeError, ValueError) as exc:
            check.issue(
                "structure",
                "INVALID_INCIDENT_DISPOSITION",
                identifier,
                "disposition",
                str(exc),
                None,
            )
    for action_id in valid_executions:
        expected = set(proposals[action_id]["incident_ids"])
        actual = by_action.get(action_id, set())
        if actual != expected:
            check.issue(
                "structure",
                "INCOMPLETE_ACTION_DISPOSITIONS",
                action_id,
                "incident_ids",
                sorted(expected),
                sorted(actual),
            )
            valid_dispositions -= actual
    check.operator_actions = {
        "historical_authorization": "NOT_VERIFIED_NO_AUTH_HISTORY",
        "maker_checker_separation": "DISTINCT_OPAQUE_ACTOR_IDS_CHECKED",
        "proposal_count": len(actions),
        "approval_count": len(approvals),
        "execution_count": len(executions),
        "disposition_count": len(dispositions),
    }
    check.unverified.add("OPERATOR_HISTORICAL_AUTHORIZATION")
    return valid_dispositions
