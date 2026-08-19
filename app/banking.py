import uuid
from decimal import Decimal, InvalidOperation

from app import config
from app import database as db
from app.errors import (
    AccountNotActive,
    InsufficientFunds,
    LimitExceeded,
    NotFoundError,
    RiskBlocked,
    ValidationError,
)

CENT = Decimal("0.01")


def money(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(CENT)
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"Not a valid amount: {value!r}") from exc


def new_reference(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16].upper()}"


def lock_account(cur, account_id) -> dict:
    cur.execute(
        "SELECT * FROM accounts WHERE account_id = %s FOR UPDATE", (account_id,)
    )
    row = cur.fetchone()
    if row is None:
        raise NotFoundError(f"No account with id {account_id}")
    return row


def _require_active(account) -> None:
    if account["status"] != "ACTIVE":
        raise AccountNotActive(
            f"Account {account['account_number']} is {account['status']}"
        )


def _require_positive(amount) -> Decimal:
    amount = money(amount)
    if amount <= 0:
        raise ValidationError("Amount must be greater than zero")
    return amount


def _check_max_txn(account, amount) -> None:
    if amount > account["max_txn_amount"]:
        raise LimitExceeded(
            f"Amount {amount} exceeds the per-transaction limit "
            f"{account['max_txn_amount']}"
        )


def _transferred_today(cur, account_id) -> Decimal:
    cur.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS spent
        FROM transactions
        WHERE account_id = %s
          AND transaction_type = 'TRANSFER_OUT'
          AND status IN ('SUCCESS', 'FLAGGED')
          AND created_at >= CURDATE()
        """,
        (account_id,),
    )
    return Decimal(str(cur.fetchone()["spent"]))


def _check_daily_limit(cur, account, amount) -> None:
    spent = _transferred_today(cur, account["account_id"])
    if spent + amount > account["daily_transfer_limit"]:
        raise LimitExceeded(
            f"Daily transfer limit {account['daily_transfer_limit']} would be exceeded "
            f"(already transferred {spent} today)"
        )


def _insert_transaction(cur, account_id, transaction_type, amount, before, after,
                        reference, transfer_id=None, status="SUCCESS",
                        risk_score=None, risk_status=None, risk_reason=None) -> int:
    cur.execute(
        """
        INSERT INTO transactions
            (account_id, transfer_id, transaction_type, amount,
             balance_before, balance_after, status, reference,
             risk_score, risk_status, risk_reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (account_id, transfer_id, transaction_type, amount, before, after,
         status, reference, risk_score, risk_status, risk_reason),
    )
    return cur.lastrowid


def deposit(account_id, amount, reference=None, actor_user_id=None) -> dict:
    amount = _require_positive(amount)
    reference = reference or new_reference("DEP")

    try:
        with db.transaction() as cur:
            account = lock_account(cur, account_id)
            _require_active(account)
            _check_max_txn(account, amount)

            before = account["balance"]
            after = before + amount

            cur.execute(
                "UPDATE accounts SET balance = %s WHERE account_id = %s",
                (after, account_id),
            )
            txn_id = _insert_transaction(cur, account_id, "DEPOSIT", amount,
                                         before, after, reference)
            db.audit(cur, "DEPOSIT", user_id=actor_user_id, account_id=account_id,
                     entity_type="transaction", entity_id=txn_id,
                     details={"amount": str(amount), "balance_after": str(after)})
    except (ValidationError, AccountNotActive, LimitExceeded, NotFoundError) as exc:
        db.record_failure(account_id, "DEPOSIT", amount, reference, str(exc),
                          user_id=actor_user_id)
        raise

    return {"transaction_id": txn_id, "reference": reference,
            "balance_before": before, "balance_after": after}


def withdraw(account_id, amount, reference=None, actor_user_id=None) -> dict:
    amount = _require_positive(amount)
    reference = reference or new_reference("WDR")

    try:
        with db.transaction() as cur:
            account = lock_account(cur, account_id)
            _require_active(account)
            _check_max_txn(account, amount)

            before = account["balance"]
            if before < amount:
                raise InsufficientFunds(
                    f"Balance {before} is less than requested {amount}"
                )
            after = before - amount

            cur.execute(
                "UPDATE accounts SET balance = %s WHERE account_id = %s",
                (after, account_id),
            )
            txn_id = _insert_transaction(cur, account_id, "WITHDRAWAL", amount,
                                         before, after, reference)
            db.audit(cur, "WITHDRAWAL", user_id=actor_user_id, account_id=account_id,
                     entity_type="transaction", entity_id=txn_id,
                     details={"amount": str(amount), "balance_after": str(after)})
    except (ValidationError, AccountNotActive, LimitExceeded, InsufficientFunds,
            NotFoundError) as exc:
        db.record_failure(account_id, "WITHDRAWAL", amount, reference, str(exc),
                          user_id=actor_user_id)
        raise

    return {"transaction_id": txn_id, "reference": reference,
            "balance_before": before, "balance_after": after}


def transfer(from_account_id, to_account_id, amount, reference=None,
             actor_user_id=None, risk_check=True, on_high_risk="flag") -> dict:
    amount = _require_positive(amount)
    reference = reference or new_reference("TRF")

    if from_account_id == to_account_id:
        db.record_failure(from_account_id, "TRANSFER_OUT", amount, reference,
                          "Cannot transfer to the same account", user_id=actor_user_id)
        raise ValidationError("Cannot transfer to the same account")

    lock_order = sorted([from_account_id, to_account_id])
    risk = {"risk_score": None, "risk_status": None, "risk_reason": None}

    try:
        with db.transaction() as cur:
            locked = {aid: lock_account(cur, aid) for aid in lock_order}
            source = locked[from_account_id]
            target = locked[to_account_id]

            _require_active(source)
            _require_active(target)
            _check_max_txn(source, amount)
            _check_daily_limit(cur, source, amount)

            if source["balance"] < amount:
                raise InsufficientFunds(
                    f"Balance {source['balance']} is less than requested {amount}"
                )

            if risk_check:
                risk = assess_risk(cur, from_account_id, amount)
                if (on_high_risk == "block"
                        and risk["risk_status"] == "HIGH"):
                    raise RiskBlocked(
                        f"Blocked by risk model (score {risk['risk_score']}): "
                        f"{risk['risk_reason']}"
                    )

            flagged = risk["risk_status"] == "HIGH"
            transfer_status = "FLAGGED" if flagged else "COMPLETED"
            txn_status = "FLAGGED" if flagged else "SUCCESS"

            cur.execute(
                """
                INSERT INTO transfers
                    (from_account_id, to_account_id, amount, status,
                     reference, initiated_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (from_account_id, to_account_id, amount, transfer_status,
                 reference, actor_user_id),
            )
            transfer_id = cur.lastrowid

            src_before = source["balance"]
            src_after = src_before - amount
            tgt_before = target["balance"]
            tgt_after = tgt_before + amount

            cur.execute("UPDATE accounts SET balance = %s WHERE account_id = %s",
                        (src_after, from_account_id))
            cur.execute("UPDATE accounts SET balance = %s WHERE account_id = %s",
                        (tgt_after, to_account_id))

            _insert_transaction(cur, from_account_id, "TRANSFER_OUT", amount,
                                src_before, src_after, f"{reference}-OUT",
                                transfer_id=transfer_id, status=txn_status,
                                **risk)
            _insert_transaction(cur, to_account_id, "TRANSFER_IN", amount,
                                tgt_before, tgt_after, f"{reference}-IN",
                                transfer_id=transfer_id, status=txn_status)

            db.audit(cur, "TRANSFER", user_id=actor_user_id,
                     account_id=from_account_id, entity_type="transfer",
                     entity_id=transfer_id,
                     details={"to_account_id": to_account_id,
                              "amount": str(amount),
                              "status": transfer_status,
                              "risk_score": str(risk["risk_score"])})
    except (ValidationError, AccountNotActive, LimitExceeded, InsufficientFunds,
            NotFoundError, RiskBlocked) as exc:
        db.record_failure(from_account_id, "TRANSFER_OUT", amount, reference,
                          str(exc), user_id=actor_user_id)
        raise

    return {"transfer_id": transfer_id, "reference": reference,
            "status": transfer_status, "amount": amount,
            "from_balance_after": src_after, "to_balance_after": tgt_after,
            **risk}


def assess_risk(cur, account_id, amount) -> dict:
    try:
        from ml.predict import score_transaction
    except ImportError:
        return {"risk_score": None, "risk_status": None, "risk_reason": None}

    try:
        return score_transaction(cur, account_id, amount)
    except FileNotFoundError:
        return {"risk_score": None, "risk_status": None, "risk_reason": None}


def get_balance(account_id) -> Decimal:
    row = db.query_one(
        "SELECT balance FROM accounts WHERE account_id = %s", (account_id,)
    )
    if row is None:
        raise NotFoundError(f"No account with id {account_id}")
    return row["balance"]


def transaction_history(account_id, limit=50, status=None) -> list[dict]:
    sql = """
        SELECT transaction_id, transaction_type, amount, balance_before,
               balance_after, status, reference, failure_reason,
               risk_score, risk_status, created_at
        FROM transactions
        WHERE account_id = %s
    """
    params = [account_id]
    if status:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY created_at DESC, transaction_id DESC LIMIT %s"
    params.append(int(limit))
    return db.query_all(sql, params)


def reconcile(account_id=None) -> list[dict]:
    sql = """
        SELECT a.account_id,
               a.account_number,
               a.balance AS stored_balance,
               COALESCE(SUM(CASE
                   WHEN t.transaction_type IN ('DEPOSIT','TRANSFER_IN')     THEN t.amount
                   WHEN t.transaction_type IN ('WITHDRAWAL','TRANSFER_OUT') THEN -t.amount
               END), 0) AS ledger_balance
        FROM accounts a
        LEFT JOIN transactions t
               ON t.account_id = a.account_id
              AND t.status IN ('SUCCESS','FLAGGED')
        {where}
        GROUP BY a.account_id, a.account_number, a.balance
        HAVING stored_balance <> ledger_balance
    """
    if account_id is None:
        return db.query_all(sql.format(where=""))
    return db.query_all(
        sql.format(where="WHERE a.account_id = %s"), (account_id,)
    )
