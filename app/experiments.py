import threading
import time
from decimal import Decimal

import pymysql

from app import banking, customers
from app import database as db
from app.errors import BankingError, InsufficientFunds


def _fresh_account(balance) -> int:
    suffix = int(time.time() * 1000) % 100000000
    cust = customers.create_customer(
        f"Race Subject {suffix}", f"race{suffix}@example.com",
        f"9{suffix:09d}"[:10], "1990-01-01",
    )
    return customers.create_account(cust, "SAVINGS", balance)["account_id"]


def unsafe_withdraw(account_id, amount, delay=0.20) -> str:
    amount = banking.money(amount)
    conn = db.get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM accounts WHERE account_id = %s",
                        (account_id,))
            before = cur.fetchone()["balance"]

            time.sleep(delay)

            if before < amount:
                conn.rollback()
                return f"rejected (read {before})"
            after = before - amount
            cur.execute("UPDATE accounts SET balance = %s WHERE account_id = %s",
                        (after, account_id))
            cur.execute(
                """
                INSERT INTO transactions
                    (account_id, transaction_type, amount, balance_before,
                     balance_after, reference)
                VALUES (%s, 'WITHDRAWAL', %s, %s, %s, %s)
                """,
                (account_id, amount, before, after,
                 banking.new_reference("UNSAFE")),
            )
        conn.commit()
        return f"succeeded (read {before} -> wrote {after})"
    except pymysql.MySQLError as exc:
        conn.rollback()
        return f"db error: {exc.args[1] if len(exc.args) > 1 else exc}"
    finally:
        conn.close()


def safe_withdraw(account_id, amount, delay=0.20) -> str:
    amount = banking.money(amount)
    conn = db.get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            account = banking.lock_account(cur, account_id)
            before = account["balance"]

            time.sleep(delay)

            if before < amount:
                conn.rollback()
                return f"rejected (read {before})"
            after = before - amount
            cur.execute("UPDATE accounts SET balance = %s WHERE account_id = %s",
                        (after, account_id))
            cur.execute(
                """
                INSERT INTO transactions
                    (account_id, transaction_type, amount, balance_before,
                     balance_after, reference)
                VALUES (%s, 'WITHDRAWAL', %s, %s, %s, %s)
                """,
                (account_id, amount, before, after, banking.new_reference("SAFE")),
            )
        conn.commit()
        return f"succeeded (read {before} -> wrote {after})"
    except pymysql.MySQLError as exc:
        conn.rollback()
        return f"db error: {exc.args[1] if len(exc.args) > 1 else exc}"
    finally:
        conn.close()


def _run_concurrently(fn, jobs) -> list[str]:
    results = [None] * len(jobs)

    def worker(index, args):
        results[index] = fn(*args)

    threads = [threading.Thread(target=worker, args=(i, a))
               for i, a in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def lost_update_demo(start_balance=1000, amounts=(800, 700)) -> dict:
    report = {}
    for label, fn in (("NO LOCK", unsafe_withdraw), ("FOR UPDATE", safe_withdraw)):
        account_id = _fresh_account(start_balance)
        outcomes = _run_concurrently(fn, [(account_id, a) for a in amounts])

        stored = banking.get_balance(account_id)
        rows = db.query_all(
            """
            SELECT COALESCE(SUM(amount), 0) AS withdrawn, COUNT(*) AS n
            FROM transactions
            WHERE account_id = %s AND transaction_type = 'WITHDRAWAL'
              AND status = 'SUCCESS'
            """,
            (account_id,),
        )[0]
        report[label] = {
            "account_id": account_id,
            "outcomes": outcomes,
            "start_balance": Decimal(str(start_balance)).quantize(Decimal("0.01")),
            "stored_balance": stored,
            "successful_withdrawals": rows["n"],
            "total_withdrawn": Decimal(str(rows["withdrawn"])),
            "reconciles": not banking.reconcile(account_id),
        }
    return report


def rollback_demo(amount=300) -> dict:
    a = _fresh_account(1000)
    b = _fresh_account(500)
    before = (banking.get_balance(a), banking.get_balance(b))

    class Boom(Exception):
        pass

    error = None
    try:
        with db.transaction() as cur:
            for aid in sorted([a, b]):
                banking.lock_account(cur, aid)
            cur.execute("UPDATE accounts SET balance = balance - %s WHERE account_id = %s",
                        (amount, a))
            cur.execute("UPDATE accounts SET balance = balance + %s WHERE account_id = %s",
                        (amount, b))
            cur.execute(
                """
                INSERT INTO transfers (from_account_id, to_account_id, amount,
                                       status, reference)
                VALUES (%s, %s, %s, 'COMPLETED', %s)
                """,
                (a, b, amount, banking.new_reference("ROLLBACK")),
            )
            raise Boom("simulated crash after both balances were updated")
    except Boom as exc:
        error = str(exc)

    after = (banking.get_balance(a), banking.get_balance(b))
    transfers = db.query_all(
        "SELECT COUNT(*) AS n FROM transfers WHERE from_account_id = %s", (a,)
    )[0]["n"]
    return {"error": error, "before": before, "after": after,
            "unchanged": before == after, "transfer_rows_left": transfers}


def _ordered_transfer(a, b, amount, order, delay=0.20) -> str:
    amount = banking.money(amount)
    conn = db.get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            first, second = order
            locked = {first: banking.lock_account(cur, first)}
            time.sleep(delay)
            locked[second] = banking.lock_account(cur, second)

            reference = banking.new_reference("DLK")
            cur.execute(
                """
                INSERT INTO transfers (from_account_id, to_account_id, amount,
                                       status, reference)
                VALUES (%s, %s, %s, 'COMPLETED', %s)
                """,
                (a, b, amount, reference),
            )
            transfer_id = cur.lastrowid

            src_before = locked[a]["balance"]
            tgt_before = locked[b]["balance"]
            cur.execute("UPDATE accounts SET balance = %s WHERE account_id = %s",
                        (src_before - amount, a))
            cur.execute("UPDATE accounts SET balance = %s WHERE account_id = %s",
                        (tgt_before + amount, b))
            banking._insert_transaction(cur, a, "TRANSFER_OUT", amount, src_before,
                                        src_before - amount, f"{reference}-OUT",
                                        transfer_id=transfer_id)
            banking._insert_transaction(cur, b, "TRANSFER_IN", amount, tgt_before,
                                        tgt_before + amount, f"{reference}-IN",
                                        transfer_id=transfer_id)
        conn.commit()
        return "committed"
    except pymysql.err.OperationalError as exc:
        conn.rollback()
        return f"DEADLOCK ({exc.args[0]}: {exc.args[1][:40]})"
    except pymysql.MySQLError as exc:
        conn.rollback()
        return f"error: {exc}"
    finally:
        conn.close()


def deadlock_demo() -> dict:
    report = {}
    for label, ordered in (("UNORDERED LOCKS", False), ("ORDERED BY account_id", True)):
        a = _fresh_account(1000)
        b = _fresh_account(1000)
        if ordered:
            jobs = [(a, b, 100, sorted([a, b])), (b, a, 100, sorted([a, b]))]
        else:
            jobs = [(a, b, 100, [a, b]), (b, a, 100, [b, a])]
        outcomes = _run_concurrently(_ordered_transfer, jobs)
        report[label] = {
            "outcomes": outcomes,
            "deadlocks": sum("DEADLOCK" in o for o in outcomes),
            "balances": (banking.get_balance(a), banking.get_balance(b)),
        }
    return report


def isolation_demo() -> dict:
    account_id = _fresh_account(1000)
    report = {}

    for level in ("REPEATABLE READ", "READ COMMITTED"):
        reader = db.get_connection(isolation_level=level)
        try:
            reader.begin()
            with reader.cursor() as cur:
                cur.execute("SELECT balance FROM accounts WHERE account_id = %s",
                            (account_id,))
                first_read = cur.fetchone()["balance"]

            banking.deposit(account_id, 100)

            with reader.cursor() as cur:
                cur.execute("SELECT balance FROM accounts WHERE account_id = %s",
                            (account_id,))
                second_read = cur.fetchone()["balance"]
            reader.commit()
        finally:
            reader.close()

        report[level] = {
            "first_read": first_read,
            "second_read": second_read,
            "saw_other_commit": first_read != second_read,
        }
    return report


def limit_demo() -> dict:
    a = _fresh_account(200000)
    b = _fresh_account(0)
    db.execute(
        "UPDATE accounts SET daily_transfer_limit = 10000, max_txn_amount = 8000 "
        "WHERE account_id = %s", (a,)
    )
    outcomes = []
    for amount in (8000, 8000, 9000):
        try:
            banking.transfer(a, b, amount, risk_check=False)
            outcomes.append(f"{amount}: allowed")
        except BankingError as exc:
            outcomes.append(f"{amount}: {type(exc).__name__} - {exc}")
    return {"account_id": a, "outcomes": outcomes,
            "final_balance": banking.get_balance(a)}
