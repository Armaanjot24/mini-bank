import joblib
import pandas as pd

from app import config
from ml import features

_ARTIFACT = None

EXPLANATIONS = [
    ("amount_ratio", lambda v: v >= 3,
     lambda v: f"amount is {v:.1f}x this account's historical average"),
    ("amount_zscore", lambda v: v >= 3,
     lambda v: f"amount is {v:.1f} standard deviations above normal"),
    ("txn_count_1h", lambda v: v >= 3,
     lambda v: f"{int(v)} transactions already in the last hour"),
    ("txn_count_24h", lambda v: v >= 8,
     lambda v: f"{int(v)} transactions in the last 24 hours"),
    ("is_night", lambda v: v == 1,
     lambda v: "transaction between midnight and 6am"),
    ("seconds_since_prev", lambda v: v <= 120,
     lambda v: f"only {int(v)}s since the previous transaction"),
    ("prior_failed_count", lambda v: v >= 3,
     lambda v: f"{int(v)} earlier failed transactions on this account"),
    ("account_age_days", lambda v: v <= 30,
     lambda v: f"account is only {int(v)} days old"),
]


def load_model(force_reload=False):
    global _ARTIFACT
    if _ARTIFACT is None or force_reload:
        if not config.MODEL_PATH.exists():
            raise FileNotFoundError(
                f"No trained model at {config.MODEL_PATH}. Run: python -m ml.train"
            )
        _ARTIFACT = joblib.load(config.MODEL_PATH)
    return _ARTIFACT


def classify(score) -> str:
    if score >= config.RISK_HIGH_THRESHOLD:
        return "HIGH"
    if score >= config.RISK_FLAG_THRESHOLD:
        return "MEDIUM"
    return "LOW"


def explain(feature_values, limit=2) -> str:
    importance = getattr(load_model()["model"], "feature_importances_", None)
    weights = (dict(zip(load_model()["feature_columns"], importance))
               if importance is not None else {})

    triggered = []
    for name, condition, message in EXPLANATIONS:
        value = feature_values.get(name)
        if value is not None and condition(value):
            triggered.append((weights.get(name, 0.0), message(value)))

    if not triggered:
        return "no individual signal stood out"
    triggered.sort(reverse=True, key=lambda pair: pair[0])
    return "; ".join(message for _, message in triggered[:limit])


def score_transaction(cur, account_id, amount, transaction_type="TRANSFER_OUT",
                      now=None) -> dict:
    artifact = load_model()
    feature_values = features.features_for_pending(
        cur, account_id, amount, transaction_type, now
    )
    frame = pd.DataFrame([feature_values])[artifact["feature_columns"]].astype(float)
    score = float(artifact["model"].predict_proba(frame)[0, 1])
    status = classify(score)

    return {
        "risk_score": round(score, 4),
        "risk_status": status,
        "risk_reason": explain(feature_values) if status != "LOW" else None,
    }


def score_account_amount(account_id, amount, transaction_type="TRANSFER_OUT") -> dict:
    from app import database as db

    with db.read_cursor() as cur:
        return score_transaction(cur, account_id, amount, transaction_type)


def backfill_scores(limit=None, verbose=True) -> int:
    from app import database as db

    artifact = load_model()
    dataset = features.load_dataset()
    if limit:
        dataset = dataset.tail(limit)
    if dataset.empty:
        return 0

    X, _ = features.to_xy(dataset)
    scores = artifact["model"].predict_proba(X)[:, 1]

    updates = []
    for (_, row), score in zip(dataset.iterrows(), scores):
        status = classify(float(score))
        reason = explain(row.to_dict()) if status != "LOW" else None
        updates.append((round(float(score), 4), status, reason,
                        int(row["transaction_id"])))

    with db.transaction() as cur:
        for i in range(0, len(updates), 2000):
            cur.executemany(
                """
                UPDATE transactions
                SET risk_score = %s, risk_status = %s, risk_reason = %s
                WHERE transaction_id = %s
                """,
                updates[i:i + 2000],
            )
    if verbose:
        print(f"scored {len(updates)} outgoing transactions")
    return len(updates)


if __name__ == "__main__":
    backfill_scores()
