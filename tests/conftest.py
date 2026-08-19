import itertools
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import customers  # noqa: E402
from app import database as db  # noqa: E402

_counter = itertools.count(1)


@pytest.fixture(scope="session", autouse=True)
def require_database():
    try:
        db.ping()
    except Exception as exc:
        pytest.skip(f"MySQL not reachable: {exc}", allow_module_level=True)


@pytest.fixture
def new_customer():
    def _make(name="Test Person", dob="1995-01-01"):
        n = next(_counter)
        unique = f"{id(_make) % 100000}{n:05d}"
        return customers.create_customer(
            f"{name} {n}", f"test{unique}@example.com", f"9{unique[-9:]:>09}"[:10], dob
        )
    return _make


@pytest.fixture
def new_account(new_customer):
    def _make(balance=1000, account_type="SAVINGS", customer_id=None, **kwargs):
        customer_id = customer_id or new_customer()
        account = customers.create_account(
            customer_id, account_type, Decimal(str(balance)), **kwargs
        )
        return account["account_id"]
    return _make
