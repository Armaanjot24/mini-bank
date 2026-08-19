from decimal import Decimal

import pymysql
import pytest

from app import customers
from app import database as db
from app.errors import NotFoundError, ValidationError


def test_customer_is_created_with_active_status(new_customer):
    customer = customers.get_customer(new_customer())
    assert customer["status"] == "ACTIVE"


def test_duplicate_email_is_rejected(new_customer):
    customer_id = new_customer()
    email = customers.get_customer(customer_id)["email"]
    with pytest.raises(ValidationError):
        customers.create_customer("Copycat", email, "9999999999", "1990-01-01")


def test_underage_customer_is_rejected():
    with pytest.raises(ValidationError, match="at least 18"):
        customers.create_customer("Baby", "baby@example.com", "9111111111",
                                  "2020-01-01")


def test_account_opens_with_matching_ledger_entry(new_account):
    account_id = new_account(balance=2500)
    account = customers.get_account(account_id)
    assert account["balance"] == Decimal("2500.00")
    assert account["status"] == "ACTIVE"
    assert len(account["account_number"]) == 12
    assert account["account_number"].isdigit()

    rows = db.query_all(
        "SELECT * FROM transactions WHERE account_id = %s", (account_id,)
    )
    assert len(rows) == 1
    assert rows[0]["transaction_type"] == "DEPOSIT"
    assert rows[0]["balance_after"] == Decimal("2500.00")


def test_unknown_account_raises(new_account):
    with pytest.raises(NotFoundError):
        customers.get_account(99999999)


def test_invalid_account_type_is_rejected(new_customer):
    with pytest.raises(ValidationError):
        customers.create_account(new_customer(), "CRYPTO", 100)


def test_own_account_cannot_be_a_beneficiary(new_customer):
    customer_id = new_customer()
    account_id = customers.create_account(customer_id, "SAVINGS", 100)["account_id"]
    with pytest.raises(ValidationError, match="your own account"):
        customers.add_beneficiary(customer_id, account_id, "Me")


def test_same_beneficiary_cannot_be_saved_twice(new_customer, new_account):
    owner = new_customer()
    target = new_account(balance=100)
    customers.add_beneficiary(owner, target, "Friend")
    with pytest.raises(ValidationError):
        customers.add_beneficiary(owner, target, "Friend Again")


def test_database_rejects_negative_balance(new_account):
    account_id = new_account(balance=100)
    with pytest.raises(pymysql.MySQLError) as info:
        db.execute("UPDATE accounts SET balance = -1 WHERE account_id = %s",
                   (account_id,))
    assert "chk_accounts_balance" in str(info.value)


def test_database_rejects_inconsistent_ledger_row(new_account):
    account_id = new_account(balance=100)
    with pytest.raises(pymysql.MySQLError) as info:
        db.execute(
            """
            INSERT INTO transactions
                (account_id, transaction_type, amount, balance_before,
                 balance_after, reference)
            VALUES (%s, 'WITHDRAWAL', 50, 100, 99, 'BAD-MATH-TEST')
            """,
            (account_id,),
        )
    assert "chk_txn_balance_math" in str(info.value)
