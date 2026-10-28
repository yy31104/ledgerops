"""Freeze the reviewed disabled-customer eligibility boundary without changing policy."""

import pytest
from psycopg.pq import TransactionStatus

from ledgerops.db.admission import NotAuthorized, submit_reversal
from tests.integration.purchase_helpers import invoke, purchase_input
from tests.integration.reversal_helpers import (
    original_rows,
    reversal_input,
    reversal_state,
    reverse,
)

pytestmark = pytest.mark.integration


def test_new_reversal_with_disabled_original_customer_is_404_and_retains_nothing(seeded_db, golden):
    assert invoke(seeded_db, *purchase_input(golden)).http_status == 201
    original_id = golden["purchase_request"]["operation_id"]
    original = original_rows(seeded_db, original_id)
    customer = golden["purchase_request"]["customer_account_id"]
    with seeded_db.connect("owner") as owner:
        owner.execute("UPDATE ledger.account SET enabled=false WHERE id=%s", (customer,))
    args = reversal_input(golden)
    before = reversal_state(seeded_db)
    with seeded_db.connect("app") as conn:
        with pytest.raises(NotAuthorized) as caught:
            submit_reversal(conn, *args)
        assert caught.value.http_status == 404 and caught.value.code == "NOT_FOUND"
        assert conn.info.transaction_status == TransactionStatus.IDLE
    assert reversal_state(seeded_db) == before
    assert original_rows(seeded_db, original_id) == original
    with seeded_db.connect("reader") as reader:
        for table in ("operation", "request_result", "reversal_attempt"):
            assert reader.execute(
                f"SELECT count(*) FROM ledger.{table} WHERE operation_id=%s",
                (str(args[1].operation_id),),
            ).fetchone() == (0,)
    # The unretained eligibility error does not burn the submitted business ID.
    with seeded_db.connect("owner") as owner:
        owner.execute("UPDATE ledger.account SET enabled=true WHERE id=%s", (customer,))
    accepted = reverse(seeded_db, *args)
    assert accepted.http_status == 201 and not accepted.replayed
    with seeded_db.connect("owner") as owner:
        owner.execute("UPDATE ledger.account SET enabled=false WHERE id=%s", (customer,))
    committed = reversal_state(seeded_db)
    replay = reverse(seeded_db, *args)
    assert replay.http_status == 201 and replay.replayed and replay.body == accepted.body
    assert reversal_state(seeded_db) == committed
