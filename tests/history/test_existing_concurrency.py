"""Existing concurrency integration tests, rerun unchanged with history recording.

Importing a test function makes pytest collect it here, where `db` and `seeded_db`
resolve to the recording fixtures in tests/history/conftest.py. Each test keeps its
own literal business assertions; teardown additionally checks the recorded client
history for G0, G1a/b/c, G-single, G2-item, lost update and duplicate versions.
"""

import pytest

from tests.integration.test_purchase_concurrency import (  # noqa: F401
    test_identity_wait_observes_winner_then_replays_or_conflicts_without_duplicate_effect,
    test_lower_merchant_uuid_is_locked_before_higher_customer_uuid,
    test_two_distinct_debits_of_80_from_100_commit_one_success_and_one_saved_rejection,
)
from tests.integration.test_purchase_retries import (  # noqa: F401
    test_real_deadlock_retries_after_rollback_and_commits_one_effect,
)
from tests.integration.test_r2_mixed import (  # noqa: F401
    test_distinct_original_reversals_share_balances_without_cross_original_waits,
    test_freeze_drains_purchase_reversal_and_original_replay_chain,
    test_purchase_and_reversal_follow_the_observed_balance_queue,
)

pytestmark = pytest.mark.integration
