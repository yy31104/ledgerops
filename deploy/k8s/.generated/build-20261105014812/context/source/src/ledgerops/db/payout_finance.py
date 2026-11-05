"""Terminal payout posting; caller holds namespace and payout locks in one transaction."""

from uuid import uuid4

from ledgerops.domain import MAX_TOTAL_MINOR


class LocalSettlementBlocked(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def advance_knowledge(cur, payout, state, sequence):
    if payout["version"] >= 2**63 - 1:
        raise LocalSettlementBlocked("CAPACITY_EXCEEDED")
    cur.execute(
        "UPDATE ledger.payout SET knowledge=%s,source_sequence=%s,version=version+1 "
        "WHERE workspace_id=%s AND operation_id=%s",
        (
            state,
            max(sequence, payout["source_sequence"]),
            payout["workspace_id"],
            payout["operation_id"],
        ),
    )


def _account_targets(cur, payout, state):
    targets = {payout["merchant_account_id"]: "MERCHANT_LIABILITY"}
    clearing_id = None
    if state == "SUCCEEDED":
        clearing = cur.execute(
            "SELECT id FROM ledger.account WHERE workspace_id=%s AND class='SYNTHETIC_CLEARING'",
            (payout["workspace_id"],),
        ).fetchall()
        if len(clearing) != 1:
            raise LocalSettlementBlocked("PROJECTION_MISSING")
        clearing_id = clearing[0]["id"]
        targets[clearing_id] = "SYNTHETIC_CLEARING"
    return targets, clearing_id


def _lock_and_check_projections(cur, payout, state):
    targets, clearing_id = _account_targets(cur, payout, state)
    rows = {}
    for account_id in sorted(targets, key=lambda value: value.int):
        row = cur.execute(
            "SELECT "
            "a.class,a.normal_side,a.currency,b.debits,b.credits,b.reserved,b.booked,b.version "
            "FROM ledger.account a JOIN ledger.account_balance b ON b.workspace_id=a.workspace_id "
            "AND b.account_id=a.id WHERE a.workspace_id=%s AND a.id=%s FOR UPDATE OF b",
            (payout["workspace_id"], account_id),
        ).fetchone()
        normal = "D" if account_id == clearing_id else "C"
        if row is None or (row["class"], row["normal_side"], row["currency"]) != (
            targets[account_id],
            normal,
            "PLN",
        ):
            raise LocalSettlementBlocked("PROJECTION_MISSING")
        folded = cur.execute(
            "SELECT coalesce(sum(amount_minor) FILTER(WHERE side='D'),0) AS debits,"
            "coalesce(sum(amount_minor) FILTER(WHERE side='C'),0) AS credits "
            "FROM ledger.journal_entry WHERE workspace_id=%s AND account_id=%s",
            (payout["workspace_id"], account_id),
        ).fetchone()
        if (row["debits"], row["credits"]) != (folded["debits"], folded["credits"]):
            raise LocalSettlementBlocked("PROJECTION_MISMATCH")
        expected_reserved = cur.execute(
            "SELECT coalesce(sum(r.gross_minor),0) AS reserved FROM ledger.reservation r "
            "JOIN ledger.reservation_event e ON e.workspace_id=r.workspace_id AND "
            "e.payout_id=r.payout_id "
            "WHERE r.workspace_id=%s AND r.account_id=%s AND e.state='HELD' AND e.event_no="
            "(SELECT max(last.event_no) FROM ledger.reservation_event last "
            "WHERE last.workspace_id=r.workspace_id AND last.payout_id=r.payout_id)",
            (payout["workspace_id"], account_id),
        ).fetchone()["reserved"]
        if row["reserved"] != expected_reserved:
            raise LocalSettlementBlocked("HOLD_PROJECTION_MISMATCH")
        if row["version"] >= 2**63 - 1:
            raise LocalSettlementBlocked("CAPACITY_EXCEEDED")
        rows[account_id] = row
    return rows, clearing_id


def _success_journal(cur, payout, clearing_id):
    workspace, payout_id, gross = (
        payout["workspace_id"],
        payout["operation_id"],
        payout["gross_minor"],
    )
    journal_id = uuid4()
    cur.execute(
        "INSERT INTO ledger.journal "
        "(workspace_id,journal_id,operation_id,kind,line_count,total_minor) "
        "VALUES (%s,%s,%s,'PAYOUT',2,%s)",
        (workspace, journal_id, payout_id, gross),
    )
    cur.execute(
        "INSERT INTO ledger.journal_entry "
        "(workspace_id,journal_id,line_no,account_id,side,amount_minor) "
        "VALUES (%s,%s,1,%s,'D',%s),(%s,%s,2,%s,'C',%s)",
        (
            workspace,
            journal_id,
            payout["merchant_account_id"],
            gross,
            workspace,
            journal_id,
            clearing_id,
            gross,
        ),
    )
    return journal_id


def settle(cur, payout, observation, now):
    state = observation["state"]
    workspace, payout_id, gross = (
        payout["workspace_id"],
        payout["operation_id"],
        payout["gross_minor"],
    )
    hold = cur.execute(
        "SELECT state,version FROM ledger.reservation WHERE workspace_id=%s AND payout_id=%s FOR "
        "UPDATE",
        (workspace, payout_id),
    ).fetchone()
    if hold is None or (hold["state"], hold["version"]) != ("HELD", 1):
        raise LocalSettlementBlocked("HOLD_PROJECTION_MISMATCH")
    balances, clearing_id = _lock_and_check_projections(cur, payout, state)
    merchant = balances[payout["merchant_account_id"]]
    if int(merchant["reserved"]) < gross:
        raise LocalSettlementBlocked("HOLD_PROJECTION_MISMATCH")
    if payout["version"] >= 2**63 - 1:
        raise LocalSettlementBlocked("CAPACITY_EXCEEDED")
    if state == "SUCCEEDED":
        clearing = balances[clearing_id]
        if (
            int(merchant["debits"]) + gross > MAX_TOTAL_MINOR
            or int(clearing["credits"]) + gross > MAX_TOTAL_MINOR
            or int(clearing["booked"]) - int(clearing["reserved"]) < gross
        ):
            raise LocalSettlementBlocked("CAPACITY_EXCEEDED")
        journal_id = _success_journal(cur, payout, clearing_id)
        terminal_hold = "CONSUMED"
    else:
        journal_id, terminal_hold = None, "RELEASED"
    for account_id in sorted(balances, key=lambda value: value.int):
        if account_id == payout["merchant_account_id"]:
            debit = gross if state == "SUCCEEDED" else 0
            cur.execute(
                "UPDATE ledger.account_balance SET "
                "debits=debits+%s,reserved=reserved-%s,version=version+1 "
                "WHERE workspace_id=%s AND account_id=%s",
                (debit, gross, workspace, account_id),
            )
        else:
            cur.execute(
                "UPDATE ledger.account_balance SET credits=credits+%s,version=version+1 "
                "WHERE workspace_id=%s AND account_id=%s",
                (gross, workspace, account_id),
            )
    cur.execute(
        "UPDATE ledger.reservation SET state=%s,version=2 WHERE workspace_id=%s AND payout_id=%s",
        (terminal_hold, workspace, payout_id),
    )
    cur.execute(
        "INSERT INTO ledger.reservation_event "
        "(workspace_id,payout_id,event_no,state,observation_id) "
        "VALUES (%s,%s,2,%s,%s)",
        (workspace, payout_id, terminal_hold, observation["observation_id"]),
    )
    cur.execute(
        "INSERT INTO ledger.payout_resolution "
        "(workspace_id,payout_id,observation_id,state,journal_id,resolved_at) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (workspace, payout_id, observation["observation_id"], state, journal_id, now),
    )
    advance_knowledge(cur, payout, state, observation["source_sequence"])
    cur.execute(
        "UPDATE ledger.dispatch_intent SET state='DONE',next_action='LOOKUP',next_due_at=%s "
        "WHERE workspace_id=%s AND payout_id=%s",
        (now, workspace, payout_id),
    )
