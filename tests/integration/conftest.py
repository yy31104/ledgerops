import pytest

from tests.integration.helpers import post_fixture, seed_accounts


@pytest.fixture
def seeded_db(db, golden):
    seed_accounts(db, golden)
    post_fixture(db, golden, "OPENING")
    return db
