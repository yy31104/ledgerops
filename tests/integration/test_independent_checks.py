"""Demonstrate the trust boundary with committed writes under the runtime login.

These hand-loaded transactions are fixture SQL, not purchase admission. An
oracle failure counts only after PostgreSQL accepted the intended defective data.
"""

from decimal import Decimal
from uuid import UUID

import pytest

from reference.journal_fold import (
    Balance,
    assert_projection_matches,
    assert_purchase_matches,
    fold_entries,
)
from tests.integration.helpers import fetch_ledger, post_fixture

pytestmark = pytest.mark.integration


def projection_for(snapshot, account_id):
    return next(row for row in snapshot["projections"] if str(row["account_id"]) == str(account_id))


def check_frozen_purchase(golden, before, after):
    assert_purchase_matches(
        golden["purchase_request"],
        after["accounts"],
        after["journals"],
        after["entries"],
        before["projections"],
        after["projections"],
    )


def test_committed_golden_matches_independent_fold_and_original_request(seeded_db, golden):
    before = fetch_ledger(seeded_db)
    post_fixture(seeded_db, golden, kind="PURCHASE")
    after = fetch_ledger(seeded_db)

    assert len(after["journals"]) == 2
    assert len(after["entries"]) == 4
    assert_projection_matches(after["accounts"], after["entries"], after["projections"])
    check_frozen_purchase(golden, before, after)
    folded = fold_entries(after["accounts"], after["entries"])
    workspace_id = golden["workspace"]["id"]
    assert [folded[(workspace_id, account["id"])] for account in golden["accounts"]] == [
        Balance(100, 0, 100),
        Balance(80, 100, 20),
        Balance(0, 80, 80),
        Balance(0, 0, 0),
    ]

    # A second plain SELECT proves the oracle accepts native UUID/NUMERIC values,
    # independently of any normalization performed by fetch_ledger.
    with seeded_db.connect(role="app") as connection:
        assert connection.execute("SELECT current_user").fetchone()[0] == "ledger_app"
        rows = connection.execute(
            "SELECT workspace_id, account_id, debits, credits, booked "
            "FROM ledger.account_balance ORDER BY account_id"
        ).fetchall()
    assert len(rows) == 4
    assert isinstance(rows[0][0], UUID)
    assert isinstance(rows[0][2], Decimal)
    field_names = ("workspace_id", "account_id", "debits", "credits", "booked")
    projections = [dict(zip(field_names, row, strict=True)) for row in rows]
    assert_projection_matches(after["accounts"], after["entries"], projections)


def test_runtime_missed_customer_update_commits_but_fold_identifies_it(seeded_db, golden):
    customer_id = golden["purchase_request"]["customer_account_id"]
    post_fixture(seeded_db, golden, kind="PURCHASE", omit_projection_account_id=customer_id)
    after = fetch_ledger(seeded_db)

    # Independent connection sees all four committed lines and the missed update;
    # this is a business defect, not a rollback, setup failure or unbalanced journal.
    assert len(after["journals"]) == 2
    assert len(after["entries"]) == 4
    customer = projection_for(after, customer_id)
    assert customer["debits"] == 0 and customer["booked"] == 100
    folded = fold_entries(after["accounts"], after["entries"])
    assert folded[(golden["workspace"]["id"], customer_id)] == Balance(80, 100, 20)
    with pytest.raises(
        AssertionError, match=rf"Projection mismatch.*{customer_id}.*debits.*booked"
    ):
        assert_projection_matches(after["accounts"], after["entries"], after["projections"])


def test_runtime_wrong_merchant_commits_and_passes_fold_but_fails_owner_check(seeded_db, golden):
    before = fetch_ledger(seeded_db)
    m1_id = golden["purchase_request"]["merchant_account_id"]
    m2_id = next(account["id"] for account in golden["accounts"] if account["label"] == "M2")
    post_fixture(seeded_db, golden, kind="PURCHASE", credit_account_id=m2_id)
    after = fetch_ledger(seeded_db)

    assert len(after["journals"]) == 2
    assert len(after["entries"]) == 4
    assert projection_for(after, m1_id)["booked"] == 0
    assert projection_for(after, m2_id)["booked"] == 80
    # This passing control demonstrates why journal/projection agreement alone
    # cannot prove the requested merchant received the purchase.
    assert_projection_matches(after["accounts"], after["entries"], after["projections"])
    assert golden["purchase_request"]["merchant_account_id"] == m1_id
    with pytest.raises(AssertionError, match="Purchase owner/amount mismatch"):
        check_frozen_purchase(golden, before, after)


def test_runtime_projection_only_write_commits_but_fold_rejects_it(seeded_db, golden):
    customer_id = golden["purchase_request"]["customer_account_id"]
    with seeded_db.connect(role="app") as connection, connection.transaction():
        assert connection.execute("SELECT current_user").fetchone()[0] == "ledger_app"
        cursor = connection.execute(
            "UPDATE ledger.account_balance SET credits = credits + 1, version = version + 1 "
            "WHERE workspace_id = %s AND account_id = %s",
            (golden["workspace"]["id"], customer_id),
        )
        assert cursor.rowcount == 1
    after = fetch_ledger(seeded_db)
    assert len(after["journals"]) == 1 and len(after["entries"]) == 2
    assert projection_for(after, customer_id)["booked"] == 101
    with pytest.raises(
        AssertionError, match=rf"Projection mismatch.*{customer_id}.*credits.*booked"
    ):
        assert_projection_matches(after["accounts"], after["entries"], after["projections"])
