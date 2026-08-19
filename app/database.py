import json
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from app import config


def get_connection(autocommit: bool = False, isolation_level: str | None = None):
    conn = pymysql.connect(
        host=config.MYSQL_HOST,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DB,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=autocommit,
    )
    if isolation_level:
        with conn.cursor() as cur:
            cur.execute(f"SET SESSION TRANSACTION ISOLATION LEVEL {isolation_level}")
    return conn


@contextmanager
def transaction(conn=None, isolation_level: str | None = None):
    owns_connection = conn is None
    if owns_connection:
        conn = get_connection(isolation_level=isolation_level)
    try:
        conn.begin()
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if owns_connection:
            conn.close()


@contextmanager
def read_cursor():
    conn = get_connection(autocommit=True)
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def query_all(sql: str, params=None) -> list[dict]:
    with read_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(sql: str, params=None) -> dict | None:
    with read_cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params=None) -> int:
    with transaction() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def audit(cur, action, user_id=None, account_id=None, entity_type=None,
          entity_id=None, details=None, ip_address=None) -> None:
    cur.execute(
        """
        INSERT INTO audit_logs
            (user_id, account_id, action, entity_type, entity_id, details, ip_address)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (user_id, account_id, action, entity_type, entity_id,
         json.dumps(details, default=str) if details else None, ip_address),
    )


def record_failure(account_id, transaction_type, amount, reference, reason,
                   user_id=None, transfer_id=None) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO transactions
                    (account_id, transfer_id, transaction_type, amount,
                     status, reference, failure_reason)
                VALUES (%s, %s, %s, %s, 'FAILED', %s, %s)
                """,
                (account_id, transfer_id, transaction_type, amount, reference, reason),
            )
            audit(cur, "FAILED_TRANSACTION", user_id=user_id, account_id=account_id,
                  entity_type="transaction", entity_id=cur.lastrowid,
                  details={"reason": reason, "amount": str(amount),
                           "type": transaction_type})
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


def ping() -> dict:
    row = query_one("SELECT DATABASE() AS db, VERSION() AS version, @@transaction_isolation AS isolation")
    return row
