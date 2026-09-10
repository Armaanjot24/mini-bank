import random
from datetime import date, datetime
from decimal import Decimal

import pymysql

from app import database as db
from app.errors import NotFoundError, ValidationError

MIN_AGE = 18


def _parse_dob(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _age_on(dob: date, today: date | None = None) -> int:
    today = today or date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _new_account_number() -> str:
    return "".join(random.choices("0123456789", k=12))


def create_customer(full_name, email, phone, date_of_birth) -> int:
    dob = _parse_dob(date_of_birth)
    if _age_on(dob) < MIN_AGE:
        raise ValidationError(f"Customer must be at least {MIN_AGE} years old")

    with db.transaction() as cur:
        try:
            cur.execute(
                """
                INSERT INTO customers (full_name, email, phone, date_of_birth)
                VALUES (%s, %s, %s, %s)
                """,
                (full_name.strip(), email.strip().lower(), phone.strip(), dob),
            )
        except pymysql.err.IntegrityError as exc:
            raise ValidationError(f"Customer already exists: {exc.args[1]}") from exc
        return cur.lastrowid


def get_customer(customer_id) -> dict:
    row = db.query_one("SELECT * FROM customers WHERE customer_id = %s", (customer_id,))
    if row is None:
        raise NotFoundError(f"No customer with id {customer_id}")
    return row


def create_account(customer_id, account_type="SAVINGS", initial_deposit=Decimal("0.00"),
                   daily_transfer_limit=None, max_txn_amount=None) -> dict:
    if account_type not in ("SAVINGS", "CURRENT"):
        raise ValidationError("account_type must be SAVINGS or CURRENT")
    initial_deposit = Decimal(str(initial_deposit)).quantize(Decimal("0.01"))
    if initial_deposit < 0:
        raise ValidationError("initial_deposit cannot be negative")

    customer = get_customer(customer_id)
    if customer["status"] != "ACTIVE":
        raise ValidationError("Cannot open an account for a non-active customer")

    optional = {"daily_transfer_limit": daily_transfer_limit,
                "max_txn_amount": max_txn_amount}
    extra = {k: Decimal(str(v)) for k, v in optional.items() if v is not None}

    for _ in range(10):
        number = _new_account_number()
        columns = ["customer_id", "account_number", "account_type", "balance",
                   *extra]
        values = [customer_id, number, account_type, initial_deposit, *extra.values()]
        placeholders = ", ".join(["%s"] * len(columns))
        try:
            with db.transaction() as cur:
                cur.execute(
                    f"INSERT INTO accounts ({', '.join(columns)}) VALUES ({placeholders})",
                    values,
                )
                account_id = cur.lastrowid
                if initial_deposit > 0:
                    cur.execute(
                        """
                        INSERT INTO transactions
                            (account_id, transaction_type, amount,
                             balance_before, balance_after, reference)
                        VALUES (%s, 'DEPOSIT', %s, %s, %s, %s)
                        """,
                        (account_id, initial_deposit, Decimal("0.00"), initial_deposit,
                         f"OPEN-{number}"),
                    )
                db.audit(cur, "ACCOUNT_CREATED", account_id=account_id,
                         entity_type="account", entity_id=account_id,
                         details={"account_number": number, "type": account_type,
                                  "initial_deposit": str(initial_deposit)})
            return get_account(account_id)
        except pymysql.err.IntegrityError as exc:
            if "uq_accounts_number" in str(exc):
                continue
            raise ValidationError(str(exc.args[1])) from exc
    raise ValidationError("Could not allocate a unique account number")


def get_account(account_id) -> dict:
    row = db.query_one("SELECT * FROM accounts WHERE account_id = %s", (account_id,))
    if row is None:
        raise NotFoundError(f"No account with id {account_id}")
    return row


def list_accounts(customer_id) -> list[dict]:
    return db.query_all(
        "SELECT * FROM accounts WHERE customer_id = %s ORDER BY account_id",
        (customer_id,),
    )


def set_account_status(account_id, status, actor_user_id=None) -> dict:
    if status not in ("ACTIVE", "FROZEN", "CLOSED"):
        raise ValidationError("status must be ACTIVE, FROZEN or CLOSED")
    with db.transaction() as cur:
        cur.execute(
            "UPDATE accounts SET status = %s WHERE account_id = %s", (status, account_id)
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"No account with id {account_id}")
        if status == "FROZEN":
            db.audit(cur, "ACCOUNT_FROZEN", user_id=actor_user_id, account_id=account_id,
                     entity_type="account", entity_id=account_id)
    return get_account(account_id)


def add_beneficiary(customer_id, account_id, nickname) -> int:
    target = get_account(account_id)
    if target["customer_id"] == customer_id:
        raise ValidationError("You cannot add your own account as a beneficiary")
    with db.transaction() as cur:
        try:
            cur.execute(
                """
                INSERT INTO beneficiaries (customer_id, account_id, nickname)
                VALUES (%s, %s, %s)
                """,
                (customer_id, account_id, nickname.strip()),
            )
        except pymysql.err.IntegrityError as exc:
            raise ValidationError(f"Beneficiary already saved: {exc.args[1]}") from exc
        beneficiary_id = cur.lastrowid
        db.audit(cur, "BENEFICIARY_ADDED", account_id=account_id,
                 entity_type="beneficiary", entity_id=beneficiary_id,
                 details={"customer_id": customer_id, "nickname": nickname})
        return beneficiary_id
