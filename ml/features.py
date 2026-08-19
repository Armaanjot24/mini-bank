import numpy as np
import pandas as pd

from app import database as db

OUTGOING = ("WITHDRAWAL", "TRANSFER_OUT")

FEATURE_COLUMNS = [
    "amount",
    "log_amount",
    "hour",
    "is_night",
    "is_weekend",
    "account_age_days",
    "prior_txn_count",
    "hist_avg_amount",
    "amount_ratio",
    "amount_zscore",
    "seconds_since_prev",
    "txn_count_1h",
    "txn_count_24h",
    "prior_failed_count",
    "is_transfer",
]

TRAINING_SQL = """
SELECT
    t.transaction_id,
    t.account_id,
    t.transaction_type,
    t.amount,
    t.created_at,
    t.is_fraud,
    a.opened_at,
    COUNT(*)  OVER w_prior                       AS prior_txn_count,
    AVG(t.amount)    OVER w_prior                AS hist_avg_amount,
    STDDEV_SAMP(t.amount) OVER w_prior           AS hist_std_amount,
    LAG(t.created_at) OVER w_seq                 AS prev_created_at,
    COUNT(*) OVER w_1h                           AS txn_count_1h_incl,
    COUNT(*) OVER w_24h                          AS txn_count_24h_incl,
    SUM(t.status = 'FAILED') OVER w_prior        AS prior_failed_count
FROM transactions t
JOIN accounts a ON a.account_id = t.account_id
WINDOW
    w_seq   AS (PARTITION BY t.account_id ORDER BY t.created_at, t.transaction_id),
    w_prior AS (PARTITION BY t.account_id ORDER BY t.created_at, t.transaction_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
    w_1h    AS (PARTITION BY t.account_id ORDER BY t.created_at
                RANGE BETWEEN INTERVAL 1 HOUR PRECEDING AND CURRENT ROW),
    w_24h   AS (PARTITION BY t.account_id ORDER BY t.created_at
                RANGE BETWEEN INTERVAL 24 HOUR PRECEDING AND CURRENT ROW)
ORDER BY t.created_at, t.transaction_id
"""


def load_raw() -> pd.DataFrame:
    return pd.DataFrame(db.query_all(TRAINING_SQL))


def engineer(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for column in ("amount", "hist_avg_amount", "hist_std_amount"):
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df["created_at"] = pd.to_datetime(df["created_at"])
    df["opened_at"] = pd.to_datetime(df["opened_at"])
    df["prev_created_at"] = pd.to_datetime(df["prev_created_at"])

    df["log_amount"] = np.log1p(df["amount"])
    df["hour"] = df["created_at"].dt.hour
    df["is_night"] = (df["hour"] < 6).astype(int)
    df["is_weekend"] = (df["created_at"].dt.dayofweek >= 5).astype(int)
    df["account_age_days"] = (
        (df["created_at"] - df["opened_at"]).dt.total_seconds() / 86400
    ).clip(lower=0)

    df["prior_txn_count"] = df["prior_txn_count"].fillna(0).astype(int)
    df["hist_avg_amount"] = df["hist_avg_amount"].fillna(df["amount"])
    df["hist_std_amount"] = df["hist_std_amount"].fillna(0.0)

    df["amount_ratio"] = df["amount"] / df["hist_avg_amount"].replace(0, np.nan)
    df["amount_ratio"] = df["amount_ratio"].fillna(1.0).clip(upper=100)

    df["amount_zscore"] = (
        (df["amount"] - df["hist_avg_amount"]) / df["hist_std_amount"].replace(0, np.nan)
    ).fillna(0.0).clip(-20, 20)

    df["seconds_since_prev"] = (
        (df["created_at"] - df["prev_created_at"]).dt.total_seconds()
    ).fillna(86400 * 7).clip(upper=86400 * 7)

    df["txn_count_1h"] = (df["txn_count_1h_incl"] - 1).clip(lower=0)
    df["txn_count_24h"] = (df["txn_count_24h_incl"] - 1).clip(lower=0)
    df["prior_failed_count"] = df["prior_failed_count"].fillna(0).astype(int)
    df["is_transfer"] = (df["transaction_type"] == "TRANSFER_OUT").astype(int)
    return df


def load_dataset(outgoing_only=True) -> pd.DataFrame:
    df = engineer(load_raw())
    if outgoing_only:
        df = df[df["transaction_type"].isin(OUTGOING)].copy()
    return df.reset_index(drop=True)


def split_by_time(df: pd.DataFrame, test_fraction=0.25):
    df = df.sort_values("created_at").reset_index(drop=True)
    cutoff = int(len(df) * (1 - test_fraction))
    return df.iloc[:cutoff].copy(), df.iloc[cutoff:].copy()


def to_xy(df: pd.DataFrame):
    return df[FEATURE_COLUMNS].astype(float), df["is_fraud"].astype(int)


LIVE_SQL = """
SELECT
    COUNT(*)                                                   AS prior_txn_count,
    AVG(amount)                                                AS hist_avg_amount,
    STDDEV_SAMP(amount)                                        AS hist_std_amount,
    MAX(created_at)                                            AS prev_created_at,
    SUM(created_at >= NOW(3) - INTERVAL 1 HOUR)                AS txn_count_1h,
    SUM(created_at >= NOW(3) - INTERVAL 24 HOUR)               AS txn_count_24h,
    SUM(status = 'FAILED')                                     AS prior_failed_count
FROM transactions
WHERE account_id = %s
"""


def features_for_pending(cur, account_id, amount, transaction_type="TRANSFER_OUT",
                         now=None) -> dict:
    import datetime as _dt

    now = now or _dt.datetime.now()
    amount = float(amount)

    cur.execute(LIVE_SQL, (account_id,))
    history = cur.fetchone()
    cur.execute("SELECT opened_at FROM accounts WHERE account_id = %s", (account_id,))
    opened_at = cur.fetchone()["opened_at"]

    prior_count = int(history["prior_txn_count"] or 0)
    hist_avg = float(history["hist_avg_amount"] or amount)
    hist_std = float(history["hist_std_amount"] or 0.0)
    prev_ts = history["prev_created_at"]

    seconds_since_prev = (
        (now - prev_ts).total_seconds() if prev_ts else 86400 * 7
    )

    return {
        "amount": amount,
        "log_amount": float(np.log1p(amount)),
        "hour": now.hour,
        "is_night": int(now.hour < 6),
        "is_weekend": int(now.weekday() >= 5),
        "account_age_days": max((now - opened_at).total_seconds() / 86400, 0),
        "prior_txn_count": prior_count,
        "hist_avg_amount": hist_avg,
        "amount_ratio": min(amount / hist_avg, 100) if hist_avg else 1.0,
        "amount_zscore": max(min((amount - hist_avg) / hist_std, 20), -20)
                         if hist_std else 0.0,
        "seconds_since_prev": min(max(seconds_since_prev, 0), 86400 * 7),
        "txn_count_1h": int(history["txn_count_1h"] or 0),
        "txn_count_24h": int(history["txn_count_24h"] or 0),
        "prior_failed_count": int(history["prior_failed_count"] or 0),
        "is_transfer": int(transaction_type == "TRANSFER_OUT"),
    }
