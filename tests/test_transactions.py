from decimal import Decimal

import pytest

from app import banking, customers
from app import database as db
from app.errors import (
    AccountNotActive,
    InsufficientFunds,
    LimitExceeded,
    ValidationError,
)


def test_deposit_increases_balance_and_records_transaction(new_account):
    account_id = new_account(balance=1000)
    result = banking.deposit(account_id, 1000)

    assert result["balance_before"] == Decimal("1000.00")
    assert result["balance_after"] == Decimal("2000.00")
    assert banking.get_balance(account_id) == Decimal("2000.00")

    row = db.query_one(
        "SELECT * FROM transactions WHERE reference = %s", (result["reference"],)
    )
    assert row["transaction_type"] == "DEPOSIT"
    assert row["status"] == "SUCCESS"
    assert row["amount"] == Decimal("1000.00")


def test_withdrawal_decreases_balance(new_account):
    account_id = new_account(balance=1000)
    result = banking.withdraw(account_id, 500)

    assert result["balance_after"] == Decimal("500.00")
    assert banking.get_balance(account_id) == Decimal("500.00")


def test_insufficient_balance_is_rejected_and_balance_unchanged(new_account):
    account_id = new_account(balance=500)

    with pytest.raises(InsufficientFunds):
        banking.withdraw(account_id, 700)

    assert banking.get_balance(account_id) == Decimal("500.00")

    successes = db.query_all(
        """
        SELECT * FROM transactions
        WHERE account_id = %s AND transaction_type = 'WITHDRAWAL'
          AND status = 'SUCCESS'
        """,
        (account_id,),
    )
    assert not successes


def test_failed_withdrawal_is_still_recorded(new_account):
    account_id = new_account(balance=500)

    with pytest.raises(InsufficientFunds):
        banking.withdraw(account_id, 700)

    failures = db.query_all(
        "SELECT * FROM transactions WHERE account_id = %s AND status = 'FAILED'",
        (account_id,),
    )
    assert len(failures) == 1
    assert failures[0]["balance_before"] is None
    assert "less than requested" in failures[0]["failure_reason"]


def test_zero_and_negative_amounts_are_rejected(new_account):
    account_id = new_account(balance=100)
    for amount in (0, -50):
        with pytest.raises(ValidationError):
            banking.deposit(account_id, amount)
    assert banking.get_balance(account_id) == Decimal("100.00")


def test_per_transaction_limit_is_enforced(new_account):
    account_id = new_account(balance=100000, max_txn_amount=1000)
    with pytest.raises(LimitExceeded):
        banking.withdraw(account_id, 1500)
    assert banking.get_balance(account_id) == Decimal("100000.00")


def test_frozen_account_cannot_transact(new_account):
    account_id = new_account(balance=1000)
    customers.set_account_status(account_id, "FROZEN")

    with pytest.raises(AccountNotActive):
        banking.deposit(account_id, 100)
    with pytest.raises(AccountNotActive):
        banking.withdraw(account_id, 100)

    assert banking.get_balance(account_id) == Decimal("1000.00")


def test_ledger_reconciles_after_many_operations(new_account):
    account_id = new_account(balance=1000)
    banking.deposit(account_id, 250)
    banking.withdraw(account_id, 100)
    banking.deposit(account_id, 75)
    banking.withdraw(account_id, 25)

    assert banking.get_balance(account_id) == Decimal("1200.00")
    assert banking.reconcile(account_id) == ()


def test_history_returns_newest_first(new_account):
    account_id = new_account(balance=1000)
    banking.deposit(account_id, 10)
    banking.deposit(account_id, 20)

    history = banking.transaction_history(account_id)
    assert len(history) == 3
    assert history[0]["amount"] == Decimal("20.00")
