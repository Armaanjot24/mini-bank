import time
from pathlib import Path

from app import config
from app import database as db

INDEX_FILE = Path(config.BASE_DIR) / "sql" / "04_indexes.sql"

BENCHMARK_QUERIES = {
    "account_statement": (
        """
        SELECT transaction_id, transaction_type, amount, created_at
        FROM transactions
        WHERE account_id = %s AND created_at >= %s
        ORDER BY created_at DESC
        LIMIT 20
        """,
        None,
    ),
    "daily_transfer_total": (
        """
        SELECT COALESCE(SUM(amount), 0) AS spent
        FROM transactions
        WHERE account_id = %s
          AND transaction_type = 'TRANSFER_OUT'
          AND status IN ('SUCCESS','FLAGGED')
          AND created_at >= %s
        """,
        None,
    ),
    "fraud_scan": (
        """
        SELECT COUNT(*) AS n
        FROM transactions
        WHERE is_fraud = 1 AND created_at >= %s
        """,
        "fraud",
    ),
}


def customer_balance_summary(limit=20):
    return db.query_all(
        "SELECT * FROM v_customer_balance_summary "
        "ORDER BY total_balance DESC LIMIT %s", (limit,)
    )


def monthly_summary():
    return db.query_all(
        "SELECT * FROM v_monthly_transaction_summary ORDER BY month DESC"
    )


def transaction_stats():
    return db.query_all("SELECT * FROM v_transaction_stats")


def top_customers_by_balance(limit=10):
    return db.query_all(
        """
        SELECT customer_id, full_name, account_count, total_balance
        FROM v_customer_balance_summary
        WHERE account_count > 0
        ORDER BY total_balance DESC
        LIMIT %s
        """,
        (limit,),
    )


def account_ranking(limit=15):
    return db.query_all(
        "SELECT * FROM v_account_ranking ORDER BY volume_rank LIMIT %s", (limit,)
    )


def suspicious_report(limit=25):
    return db.query_all(
        "SELECT * FROM v_suspicious_transactions ORDER BY risk_score DESC, "
        "created_at DESC LIMIT %s", (limit,)
    )


def running_balance(account_id, limit=25):
    return db.query_all(
        """
        SELECT transaction_id, created_at, transaction_type, amount,
               SUM(CASE WHEN transaction_type IN ('DEPOSIT','TRANSFER_IN') THEN amount
                        ELSE -amount END)
                   OVER (ORDER BY created_at, transaction_id
                         ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_balance,
               LAG(amount) OVER (ORDER BY created_at, transaction_id) AS previous_amount
        FROM transactions
        WHERE account_id = %s AND status IN ('SUCCESS','FLAGGED')
        ORDER BY created_at, transaction_id
        LIMIT %s
        """,
        (account_id, limit),
    )


def _existing_indexes():
    rows = db.query_all(
        """
        SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s AND INDEX_NAME LIKE 'idx_%%'
        """,
        (config.MYSQL_DB,),
    )
    return {r["INDEX_NAME"] for r in rows}


def _index_statements():
    text = INDEX_FILE.read_text()
    return [s.strip() for s in text.split(";")
            if s.strip().upper().startswith("CREATE INDEX")]


def _first_column(table, index_name):
    return db.query_one(
        """
        SELECT COLUMN_NAME FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = %s
          AND SEQ_IN_INDEX = 1
        """,
        (config.MYSQL_DB, table, index_name),
    )["COLUMN_NAME"]


def drop_indexes():
    dropped, stand_ins = [], []
    for row in db.query_all(
        """
        SELECT DISTINCT TABLE_NAME, INDEX_NAME FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s AND INDEX_NAME LIKE 'idx\\_%%'
        """,
        (config.MYSQL_DB,),
    ):
        table, index_name = row["TABLE_NAME"], row["INDEX_NAME"]
        try:
            db.execute(f"DROP INDEX {index_name} ON {table}")
            dropped.append(index_name)
        except Exception as exc:
            if "needed in a foreign key constraint" not in str(exc):
                raise
            column = _first_column(table, index_name)
            stand_in = f"tmpfk_{table}_{column}"
            db.execute(f"CREATE INDEX {stand_in} ON {table} ({column})")
            db.execute(f"DROP INDEX {index_name} ON {table}")
            dropped.append(index_name)
            stand_ins.append(stand_in)
    return {"dropped": dropped, "foreign_key_stand_ins": stand_ins}


def drop_stand_ins():
    removed = []
    for row in db.query_all(
        """
        SELECT DISTINCT TABLE_NAME, INDEX_NAME FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = %s AND INDEX_NAME LIKE 'tmpfk\\_%%'
        """,
        (config.MYSQL_DB,),
    ):
        try:
            db.execute(f"DROP INDEX {row['INDEX_NAME']} ON {row['TABLE_NAME']}")
            removed.append(row["INDEX_NAME"])
        except Exception as exc:
            if "needed in a foreign key constraint" not in str(exc):
                raise
    return removed


def create_indexes():
    created = []
    for statement in _index_statements():
        name = statement.split()[2]
        try:
            db.execute(statement)
            created.append(name)
        except Exception as exc:
            if "Duplicate key name" not in str(exc):
                raise
    drop_stand_ins()
    return created


def explain(sql, params):
    with db.read_cursor() as cur:
        cur.execute("EXPLAIN FORMAT=TRADITIONAL " + sql, params)
        return cur.fetchall()


def explain_tree(sql, params):
    with db.read_cursor() as cur:
        cur.execute("EXPLAIN FORMAT=TREE " + sql, params)
        return cur.fetchone()["EXPLAIN"]


def time_query(sql, params, repeats=30):
    with db.read_cursor() as cur:
        cur.execute(sql, params)
        cur.fetchall()
        start = time.perf_counter()
        for _ in range(repeats):
            cur.execute(sql, params)
            cur.fetchall()
        elapsed = time.perf_counter() - start
    return (elapsed / repeats) * 1000


def index_benchmark(account_id, since="2000-01-01", repeats=30):
    results = {}

    def measure(phase):
        for name, (sql, kind) in BENCHMARK_QUERIES.items():
            params = (since,) if kind == "fraud" else (account_id, since)
            plan = explain(sql, params)[0]
            results.setdefault(name, {})[phase] = {
                "ms": round(time_query(sql, params, repeats), 4),
                "type": plan.get("type"),
                "key": plan.get("key"),
                "rows_examined": plan.get("rows"),
                "extra": plan.get("Extra"),
                "tree": explain_tree(sql, params).strip().splitlines()[-1].strip(),
            }

    drop_indexes()
    measure("without_index")
    create_indexes()
    measure("with_index")

    for name, phases in results.items():
        before = phases["without_index"]["ms"]
        after = phases["with_index"]["ms"]
        phases["speedup"] = round(before / after, 2) if after else None
    return results


def table_sizes():
    return db.query_all(
        """
        SELECT TABLE_NAME AS table_name, TABLE_ROWS AS approx_rows,
               ROUND(DATA_LENGTH / 1024, 1)  AS data_kb,
               ROUND(INDEX_LENGTH / 1024, 1) AS index_kb
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY DATA_LENGTH DESC
        """,
        (config.MYSQL_DB,),
    )
