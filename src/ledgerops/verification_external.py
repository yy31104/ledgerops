"""Independent v6 evidence relationships, using only exported data and stdlib.

Envelope encoding and business bindings are checked here. No key material is
accepted or inferred: this module cannot establish HMAC authenticity or independent
provider effects from a ledger export alone.
"""

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

_TERMINAL = {"SUCCEEDED", "FAILED", "CLOSED_ABSENT"}
_CHANNELS = {"PUT", "LOOKUP", "CLOSE", "CALLBACK", "HISTORY"}
_MAX_ROWS = 10000
_INCIDENT_CODES = {
    "PROVIDER_CONTRADICTION": {
        "EVENT_ID_REUSED",
        "SEQUENCE_REUSED",
        "TERMINAL_CONTRADICTION",
        "NONTERMINAL_AFTER_FINAL",
    },
    "LOCAL_BLOCKER": {
        "PROJECTION_MISSING",
        "CAPACITY_EXCEEDED",
        "HOLD_PROJECTION_MISMATCH",
        "PROJECTION_MISMATCH",
    },
    "EVIDENCE_LIMIT": {"EVIDENCE_CAPACITY"},
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


def _hex(value, *, maximum=65536, digest=False):
    pattern = r"[0-9a-f]{64}" if digest else r"(?:[0-9a-f]{2})*"
    _require(type(value) is str and re.fullmatch(pattern, value) is not None, "hex bytes required")
    _require(len(value) <= maximum * 2, "evidence byte limit exceeded")
    return bytes.fromhex(value)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("non-finite JSON constant")


def _object(raw):
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    _require(type(value) is dict, "JSON object required")
    return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _observation(row, payout):
    _uuid(row["observation_id"])
    _uuid(row["event_id"])
    _time(row["received_at"])
    _require(
        row["provider_namespace"] == payout["provider_namespace"], "provider namespace mismatch"
    )
    raw = _hex(row["envelope_hex"])
    _hex(row["envelope_sha256"], digest=True)
    _require(hashlib.sha256(raw).hexdigest() == row["envelope_sha256"], "envelope digest mismatch")
    envelope = _object(raw)
    _require(envelope.keys() == {"key_id", "payload", "signature"}, "invalid envelope fields")
    _require(raw == _canonical(envelope), "noncanonical retained envelope")
    key_id, signed, signature = row["key_id"], row["signed_payload"], row["signature"]
    _require(type(key_id) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key_id), "invalid key ID")
    _require(
        type(signed) is str and 0 < len(signed.encode()) <= 65536, "signed payload out of bounds"
    )
    _hex(signature, digest=True)
    _require(
        envelope == {"key_id": key_id, "payload": signed, "signature": signature},
        "envelope columns disagree",
    )
    value = _object(signed)
    common = {"kind", "issuer", "contract_version", "namespace", "provider_key", "state"}
    _require(
        value.get("issuer") == "ledgerops-provider-sim"
        and type(value.get("contract_version")) is int
        and value["contract_version"] == 1
        and value.get("namespace") == payout["provider_namespace"]
        and value.get("provider_key") == payout["provider_key"]
        and value.get("kind") == row["kind"]
        and value.get("state") == row["state"],
        "observation issuer, identity or state binding mismatch",
    )
    sequence = _integer(row["source_sequence"])
    if row["kind"] == "lookup":
        _require(
            value.keys() == common | {"observation_id", "visibility_watermark"},
            "invalid lookup fields",
        )
        _require(
            row["state"] == "NOT_FOUND"
            and value["observation_id"] == row["event_id"]
            and type(value["visibility_watermark"]) is int
            and value["visibility_watermark"] == sequence,
            "lookup watermark/identity mismatch",
        )
    else:
        _require(
            row["kind"] == "execution"
            and value.keys()
            == common
            | {
                "event_id",
                "source_sequence",
                "request_sha256",
                "request",
            },
            "invalid execution fields",
        )
        _integer(sequence, 1)
        request = _hex(payout["provider_request_hex"], maximum=32768)
        _require(
            row["state"] in _TERMINAL | {"ACCEPTED"}
            and value["event_id"] == row["event_id"]
            and type(value["source_sequence"]) is int
            and value["source_sequence"] == sequence
            and value["request_sha256"] == hashlib.sha256(request).hexdigest()
            and _canonical(value["request"]) == request,
            "execution event or captured request binding mismatch",
        )
    return raw


@dataclass
class ExternalFacts:
    observations: dict
    resolutions: dict
    incidents: set
    blocked: set
    contradictions: set


def _incidents(check, snapshot, workspace, payouts, observations):
    rows = check.index(
        check.rows(snapshot, "payout_incidents", workspace),
        "payout_incidents",
        lambda row: _uuid(row["incident_id"]),
    )
    affected, blocked, contradictions = set(), set(), set()
    for identifier, row in rows.items():
        try:
            payout = _uuid(row["payout_id"])
            _require(payout in payouts, "incident payout missing")
            _time(row["created_at"])
            category, code = row["category"], row["code"]
            _require(
                category in {"PROVIDER_CONTRADICTION", "LOCAL_BLOCKER", "EVIDENCE_LIMIT"},
                "invalid incident category",
            )
            _require(
                type(code) is str and re.fullmatch(r"[A-Z0-9_]{1,64}", code),
                "invalid incident code",
            )
            _require(code in _INCIDENT_CODES[category], "incident category/code mismatch")
            observation = row["observation_id"]
            if observation is None:
                _require(category == "EVIDENCE_LIMIT", "incident requires retained observation")
            else:
                _uuid(observation)
                _require(
                    observation in observations
                    and observations[observation]["payout_id"] == payout,
                    "incident observation binding missing",
                )
            related = row["related_observation_id"]
            digest = row["related_payload_sha256"]
            if category == "PROVIDER_CONTRADICTION":
                _require(
                    related is not None and digest is not None,
                    "contradiction lacks related evidence",
                )
            else:
                _require(related is None, "local blocker cannot claim related provider conflict")
            if related is not None:
                _uuid(related)
                _hex(digest, digest=True)
                if related in observations:
                    _require(
                        observations[related]["envelope_sha256"] == digest,
                        "related observation digest mismatch",
                    )
                    if category == "PROVIDER_CONTRADICTION":
                        other = observations[related]["payout_id"]
                        affected.add(other)
                        contradictions.add(other)
                # An external-workspace reference is deliberately opaque; its
                # payload is not authorized by this workspace's export boundary.
            elif digest is not None:
                _hex(digest, digest=True)
            if category == "EVIDENCE_LIMIT":
                _require(digest is not None, "evidence limit must retain incoming digest")
            affected.add(payout)
            if category == "PROVIDER_CONTRADICTION":
                contradictions.add(payout)
            else:
                blocked.add(payout)
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            check.issue(
                "structure", "INVALID_PAYOUT_INCIDENT", identifier, "incident", str(exc), None
            )
    related_rows = check.index(
        check.rows(snapshot, "related_incidents", workspace),
        "related_incidents",
        lambda row: (_uuid(row["incident_id"]), _uuid(row["observation_id"])),
    )
    for identifier, row in related_rows.items():
        try:
            payout = _uuid(row["payout_id"])
            observation = observations.get(row["observation_id"])
            _require(
                payout in payouts
                and observation is not None
                and observation["payout_id"] == payout,
                "cross-workspace incident lacks local observation binding",
            )
            _require(
                type(row["code"]) is str
                and row["code"] in _INCIDENT_CODES["PROVIDER_CONTRADICTION"],
                "invalid incident code",
            )
            _hex(row["related_payload_sha256"], digest=True)
            _time(row["created_at"])
            affected.add(payout)
            contradictions.add(payout)
        except (KeyError, TypeError, ValueError) as exc:
            check.issue(
                "structure", "INVALID_RELATED_INCIDENT", str(identifier), "incident", str(exc), None
            )
    return affected, blocked, contradictions


def _conflicts(check, observations, incident_payouts):
    by_identity, by_sequence, by_payout = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in observations.values():
        by_identity[(row["provider_namespace"], row["event_id"])].append(row)
        if row["kind"] == "execution":
            by_sequence[(row["provider_namespace"], row["source_sequence"])].append(row)
            by_payout[row["payout_id"]].append(row)
    conflicts = set()
    for groups, code in ((by_identity, "EVENT_ID_REUSED"), (by_sequence, "SEQUENCE_REUSED")):
        for rows in groups.values():
            if len(rows) < 2:
                continue
            if len({row["envelope_hex"] for row in rows}) == 1:
                check.issue(
                    "structure",
                    "DUPLICATE_OBSERVATION",
                    rows[0]["payout_id"],
                    "event",
                    "one retained logical event",
                    len(rows),
                )
                continue
            conflicts.update(row["payout_id"] for row in rows)
            for row in rows:
                check.issue(
                    "external",
                    code,
                    row["payout_id"],
                    "event",
                    "stable provider identity",
                    row["event_id"],
                )
    for payout, rows in by_payout.items():
        terminal = [row for row in rows if row["state"] in _TERMINAL]
        if len({row["state"] for row in terminal}) > 1:
            conflicts.add(payout)
            check.issue(
                "external",
                "TERMINAL_CONTRADICTION",
                payout,
                "state",
                "one immutable terminal fact",
                sorted({row["state"] for row in terminal}),
            )
        if terminal and any(
            row["state"] == "ACCEPTED"
            and row["source_sequence"] > min(t["source_sequence"] for t in terminal)
            for row in rows
        ):
            conflicts.add(payout)
            check.issue(
                "external", "NONTERMINAL_AFTER_FINAL", payout, "sequence", "terminal is final", None
            )
    for payout in conflicts - incident_payouts:
        check.issue(
            "structure",
            "MISSING_PROVIDER_INCIDENT",
            payout,
            "incident",
            "retained contradiction",
            None,
        )
    return conflicts


def _receipts_and_quarantine(check, snapshot, workspace, payouts, observations):
    receipts = check.index(
        check.rows(snapshot, "observation_receipts", workspace),
        "observation_receipts",
        lambda row: _uuid(row["receipt_id"]),
    )
    attempts = {
        row.get("attempt_id"): row for row in check.rows(snapshot, "transport_attempts", workspace)
    }
    seen, channels = set(), set()
    for identifier, row in receipts.items():
        try:
            observation = _uuid(row["observation_id"])
            payout = _uuid(row["payout_id"])
            _require(
                payout in payouts
                and observation in observations
                and observations[observation]["payout_id"] == payout,
                "receipt binding missing",
            )
            _require(row["channel"] in _CHANNELS, "unsupported receipt channel")
            _time(row["received_at"])
            key = observation, row["channel"]
            _require(key not in channels, "duplicate receipt channel")
            channels.add(key)
            seen.add(observation)
            attempt = row["transport_attempt_id"]
            if attempt is not None:
                _uuid(attempt)
                _require(
                    attempt in attempts and attempts[attempt]["payout_id"] == payout,
                    "receipt transport attempt mismatch",
                )
        except (KeyError, TypeError, ValueError) as exc:
            check.issue(
                "structure", "INVALID_OBSERVATION_RECEIPT", identifier, "receipt", str(exc), None
            )
    for observation in observations.keys() - seen:
        check.issue(
            "structure",
            "MISSING_OBSERVATION_RECEIPT",
            observation,
            "receipt",
            "one reception channel",
            None,
        )
    quarantine = check.index(
        check.rows(snapshot, "observation_quarantines", workspace),
        "observation_quarantines",
        lambda row: _uuid(row["quarantine_id"]),
    )
    for identifier, row in quarantine.items():
        try:
            _require(
                _uuid(row["payout_id"]) in payouts and row["channel"] in _CHANNELS,
                "quarantine payout/channel missing",
            )
            _require(
                row["reason"] == "INVALID_OBSERVATION",
                "invalid quarantine reason",
            )
            raw = _hex(row["raw_payload_hex"])
            _hex(row["raw_sha256"], digest=True)
            _require(
                hashlib.sha256(raw).hexdigest() == row["raw_sha256"], "quarantine digest mismatch"
            )
            _time(row["received_at"])
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            check.issue(
                "structure", "INVALID_QUARANTINE_RECORD", identifier, "quarantine", str(exc), None
            )
    if quarantine:
        check.issue(
            "warning",
            "QUARANTINED_OBSERVATIONS",
            workspace,
            "quarantine_count",
            "not financial authority",
            len(quarantine),
        )
    return len(quarantine)


def verify_external_records(check, snapshot, workspace, payouts):
    """Return validated local facts; leave money/hold matching to the independent fold."""
    observations = check.index(
        check.rows(snapshot, "provider_observations", workspace),
        "provider_observations",
        lambda row: _uuid(row["observation_id"]),
    )
    for identifier, row in list(observations.items()):
        try:
            payout = _uuid(row["payout_id"])
            _require(payout in payouts, "observation payout missing")
            _observation(row, payouts[payout])
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
            check.issue(
                "structure",
                "INVALID_PROVIDER_OBSERVATION",
                identifier,
                "observation",
                str(exc),
                None,
            )
            check.flags["intent"] = False
            del observations[identifier]
    incident_payouts, blocked, contradictions = _incidents(
        check, snapshot, workspace, payouts, observations
    )
    contradictions |= _conflicts(check, observations, incident_payouts)
    quarantine_count = _receipts_and_quarantine(check, snapshot, workspace, payouts, observations)
    resolutions = check.index(
        check.rows(snapshot, "payout_resolutions", workspace),
        "payout_resolutions",
        lambda row: _uuid(row["payout_id"]),
    )
    for payout, row in list(resolutions.items()):
        try:
            observation = observations.get(_uuid(row["observation_id"]))
            _require(
                payout in payouts
                and observation is not None
                and observation["payout_id"] == payout
                and observation["kind"] == "execution"
                and observation["state"] in _TERMINAL
                and row["state"] == observation["state"],
                "resolution observation binding mismatch",
            )
            if row["state"] == "SUCCEEDED":
                _uuid(row["journal_id"])
            else:
                _require(row["journal_id"] is None, "negative terminal must not have a journal")
            _time(row["resolved_at"])
        except (KeyError, TypeError, ValueError) as exc:
            check.issue(
                "structure", "INVALID_PAYOUT_RESOLUTION", payout, "resolution", str(exc), None
            )
            check.flags["intent"] = False
            del resolutions[payout]
    terminal_payouts = {
        row["payout_id"]
        for row in observations.values()
        if row["kind"] == "execution" and row["state"] in _TERMINAL
    }
    for payout in terminal_payouts - resolutions.keys() - incident_payouts:
        check.issue(
            "intent",
            "MISSING_TERMINAL_RESOLUTION",
            payout,
            "resolution",
            "one terminal application or retained blocker/contradiction",
            None,
        )
    for payout in incident_payouts:
        check.issue(
            "external" if payout in contradictions else "warning",
            "PAYOUT_INCIDENT",
            payout,
            "automatic_processing",
            "stopped pending investigation",
            None,
        )
    check.external_evidence = {
        "signature_authenticity": "NOT_VERIFIED_NO_TRUST_KEYS",
        "independent_provider_history": "NOT_PROVIDED",
        "external_consistency_ok": not contradictions,
        "accepted_payout_count": len(payouts),
        "resolved_payout_count": len(resolutions),
        "payouts_resolved": len(payouts) == len(resolutions),
        "unresolved_payout_ids": sorted(payouts.keys() - resolutions.keys()),
        "incident_payout_ids": sorted(incident_payouts | contradictions),
        "local_blocker_payout_ids": sorted(blocked),
        "quarantine_count": quarantine_count,
        "retained_observation_count": len(observations),
    }
    return ExternalFacts(observations, resolutions, incident_payouts, blocked, contradictions)
