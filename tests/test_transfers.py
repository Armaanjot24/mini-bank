from decimal import Decimal

import pymysql
import pytest

from app import banking, customers
from app import database as db
from app.errors import (
    AccountNotActive,
    InsufficientFunds,
    LimitExceeded,
    ValidationError,
)


def test_transfer_moves_money_between_accounts(new_account):
    a = new_account(balance=1000)
    b = new_account(balance=500)

    result = banking.transfer(a, b, 300, risk_check=False)

    assert result["status"] == "COMPLETED"
    assert banking.get_balance(a) == Decimal("700.00")
    assert banking.get_balance(b) == Decimal("800.00")


def test_transfer_creates_exactly_two_ledger_legs(new_account):
    a = new_account(balance=1000)
    b = new_account(balance=0)

    result = banking.transfer(a, b, 250, risk_check=False)

    legs = db.query_all(
        "SELECT * FROM transactions WHERE transfer_id = %s ORDER BY transaction_type",
        (result["transfer_id"],),
    )
    assert len(legs) == 2
    assert {leg["transaction_type"] for leg in legs} == {"TRANSFER_IN", "TRANSFER_OUT"}
    assert all(leg["amount"] == Decimal("250.00") for leg in legs)

    out_leg = next(leg for leg in legs if leg["transaction_type"] == "TRANSFER_OUT")
    assert out_leg["balance_after"] == out_leg["balance_before"] - Decimal("250.00")


def test_money_is_conserved_across_a_transfer(new_account):
    a = new_account(balance=1000)
    b = new_account(balance=500)
    total_before = banking.get_balance(a) + banking.get_balance(b)

    banking.transfer(a, b, 425.50, risk_check=False)

    assert banking.get_balance(a) + banking.get_balance(b) == total_before


def test_insufficient_balance_leaves_both_accounts_untouched(new_account):
    a = new_account(balance=100)
    b = new_account(balance=100)

    with pytest.raises(InsufficientFunds):
        banking.transfer(a, b, 500, risk_check=False)

    assert banking.get_balance(a) == Decimal("100.00")
    assert banking.get_balance(b) == Decimal("100.00")
    assert db.query_all("SELECT * FROM transfers WHERE from_account_id = %s", (a,)) == ()


def test_self_transfer_is_rejected(new_account):
    a = new_account(balance=1000)
    with pytest.raises(ValidationError, match="same account"):
        banking.transfer(a, a, 100, risk_check=False)
    assert banking.get_balance(a) == Decimal("1000.00")


def test_transfer_to_frozen_account_is_rejected(new_account):
    a = new_account(balance=1000)
    b = new_account(balance=0)
    customers.set_account_status(b, "FROZEN")

    with pytest.raises(AccountNotActive):
        banking.transfer(a, b, 100, risk_check=False)

    assert banking.get_balance(a) == Decimal("1000.00")


def test_daily_transfer_limit_is_enforced(new_account):
    a = new_account(balance=100000, daily_transfer_limit=1000, max_txn_amount=900)
    b = new_account(balance=0)

    banking.transfer(a, b, 900, risk_check=False)
    with pytest.raises(LimitExceeded, match="Daily transfer limit"):
        banking.transfer(a, b, 200, risk_check=False)

    assert banking.get_balance(a) == Decimal("99100.00")


def test_duplicate_reference_cannot_move_money_twice(new_account):
    a = new_account(balance=1000)
    b = new_account(balance=0)
    reference = banking.new_reference("IDEMPOTENT-TEST")

    banking.transfer(a, b, 100, reference=reference, risk_check=False)
    with pytest.raises(pymysql.MySQLError):
        banking.transfer(a, b, 100, reference=reference, risk_check=False)

    assert banking.get_balance(a) == Decimal("900.00")
    assert banking.get_balance(b) == Decimal("100.00")


def test_rollback_leaves_no_partial_state(new_account):
    a = new_account(balance=1000)
    b = new_account(balance=500)

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with db.transaction() as cur:
            for account_id in sorted([a, b]):
                banking.lock_account(cur, account_id)
            cur.execute(
                "UPDATE accounts SET balance = balance - 300 WHERE account_id = %s", (a,)
            )
            cur.execute(
                "UPDATE accounts SET balance = balance + 300 WHERE account_id = %s", (b,)
            )
            cur.execute(
                """
                INSERT INTO transfers (from_account_id, to_account_id, amount,
                                       status, reference)
                VALUES (%s, %s, 300, 'COMPLETED', 'ROLLBACK-TEST-1')
                """,
                (a, b),
            )
            raise Boom("crash before commit")

    assert banking.get_balance(a) == Decimal("1000.00")
    assert banking.get_balance(b) == Decimal("500.00")
    assert db.query_all(
        "SELECT * FROM transfers WHERE reference = 'ROLLBACK-TEST-1'"
    ) == ()
