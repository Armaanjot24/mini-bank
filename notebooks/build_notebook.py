import json
from pathlib import Path

CELLS = [
    ("md", """# Fraud Detection Experiments

Exploration and model comparison for the Mini Banking System.

Pipeline: **MySQL transaction history -> SQL window functions -> pandas -> scikit-learn**.

Run `python -m ml.generate_data` first if the database has no transaction history."""),

    ("code", """import sys, os
sys.path.insert(0, os.path.abspath(".."))

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from app import database as db
from ml import features

pd.set_option("display.width", 140)
db.ping()"""),

    ("md", "## 1. What is in the database?"),

    ("code", """db.query_all('''
    SELECT transaction_type, COUNT(*) AS n, ROUND(AVG(amount),2) AS avg_amount,
           SUM(is_fraud) AS fraud_rows
    FROM transactions GROUP BY transaction_type
''')"""),

    ("md", """## 2. Feature extraction

`ml/features.py` does the heavy lifting in **SQL**, using window functions so that every
row only ever sees its own past:

```sql
AVG(amount) OVER (PARTITION BY account_id ORDER BY created_at
                  ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
```

The `1 PRECEDING` bound is what prevents label leakage: the current transaction is
excluded from its own historical average."""),

    ("code", """df = features.load_dataset()
print(f"{len(df)} outgoing transactions, {df.is_fraud.sum()} fraudulent "
      f"({df.is_fraud.mean():.2%})")
df[features.FEATURE_COLUMNS + ["is_fraud"]].head()"""),

    ("md", "## 3. Class imbalance - why accuracy is the wrong metric"),

    ("code", """counts = df.is_fraud.value_counts()
print(counts)
print(f"\\nA model that always predicts NORMAL gets "
      f"{(1 - df.is_fraud.mean()):.2%} accuracy and catches zero fraud.")"""),

    ("md", "## 4. How do fraudulent transactions differ?"),

    ("code", """comparison = df.groupby("is_fraud")[
    ["amount", "amount_ratio", "amount_zscore", "txn_count_1h",
     "txn_count_24h", "seconds_since_prev", "hour", "is_night"]
].mean().round(2)
comparison.T"""),

    ("code", """fig, axes = plt.subplots(2, 2, figsize=(12, 7))

axes[0,0].hist([np.log10(df[df.is_fraud==0].amount), np.log10(df[df.is_fraud==1].amount)],
               bins=30, label=["normal","fraud"], density=True)
axes[0,0].set(title="log10(amount)"); axes[0,0].legend()

axes[0,1].hist([df[df.is_fraud==0].amount_ratio.clip(0,15),
                df[df.is_fraud==1].amount_ratio.clip(0,15)],
               bins=30, label=["normal","fraud"], density=True)
axes[0,1].set(title="amount / account historical average"); axes[0,1].legend()

axes[1,0].hist([df[df.is_fraud==0].hour, df[df.is_fraud==1].hour],
               bins=24, label=["normal","fraud"], density=True)
axes[1,0].set(title="hour of day"); axes[1,0].legend()

axes[1,1].hist([df[df.is_fraud==0].txn_count_1h.clip(0,10),
                df[df.is_fraud==1].txn_count_1h.clip(0,10)],
               bins=11, label=["normal","fraud"], density=True)
axes[1,1].set(title="transactions in the previous hour"); axes[1,1].legend()

plt.tight_layout()"""),

    ("md", "## 5. Feature correlation with the label"),

    ("code", """correlations = (df[features.FEATURE_COLUMNS + ["is_fraud"]]
                .corr()["is_fraud"].drop("is_fraud")
                .sort_values(ascending=False))
correlations.round(3)"""),

    ("md", """## 6. Train and compare models

The split is **by time**, not random. In production you only ever have past transactions,
so a random split would let the model peek at the future and inflate every metric."""),

    ("code", """from ml.train import train
artifact = train(save=False)"""),

    ("md", """## 7. Evaluation

`precision` = of the transactions we flagged, how many were really fraud (cost: annoyed customers).
`recall`    = of the real fraud, how much did we catch (cost: money lost).

These trade off against each other, which is what the threshold sweep below shows."""),

    ("code", """from ml.evaluate import evaluate
report = evaluate(verbose=False)
report["metrics"]"""),

    ("code", """sweep = report["sweep"]
fig, axis = plt.subplots(figsize=(8, 4.5))
axis.plot(sweep.threshold, sweep.precision, marker="o", label="precision")
axis.plot(sweep.threshold, sweep.recall, marker="o", label="recall")
axis.plot(sweep.threshold, sweep.f1, marker="o", label="F1")
axis.set(xlabel="decision threshold", ylabel="score",
         title="Choosing the threshold is a business decision")
axis.grid(alpha=0.3); axis.legend()
sweep"""),

    ("md", "## 8. Which features drive the decision?"),

    ("code", """importance = report["importance"]
fig, axis = plt.subplots(figsize=(7, 4.5))
top = importance.head(10).iloc[::-1]
axis.barh(top.feature, top.importance)
axis.set(title="Feature importance")
axis.grid(alpha=0.3, axis="x")
importance"""),

    ("md", """## 9. Explainability in practice

For any account and amount, the model returns a score, a bucket, and the reason."""),

    ("code", """from ml.predict import score_account_amount

account = db.query_one('''
    SELECT a.account_id, ROUND(AVG(t.amount)) AS avg_amount
    FROM accounts a JOIN transactions t ON t.account_id = a.account_id
    WHERE t.transaction_type IN ('WITHDRAWAL','TRANSFER_OUT')
    GROUP BY a.account_id ORDER BY COUNT(*) DESC LIMIT 1
''')
average = float(account["avg_amount"])

for multiplier in (1, 3, 10, 25):
    print(f"{multiplier:>3}x average ({average*multiplier:>10,.0f}): "
          f"{score_account_amount(account['account_id'], average*multiplier)}")"""),

    ("md", """## 10. Does the risk bucket actually correspond to fraud?

Note these scores are in-sample for rows the model trained on, so treat them as a sanity
check rather than a performance claim. The honest out-of-sample numbers are in section 7."""),

    ("code", """db.query_all('''
    SELECT risk_status, COUNT(*) AS n, SUM(is_fraud) AS actually_fraud,
           ROUND(100*SUM(is_fraud)/COUNT(*), 1) AS pct_fraud
    FROM transactions WHERE risk_status IS NOT NULL
    GROUP BY risk_status ORDER BY FIELD(risk_status,'LOW','MEDIUM','HIGH')
''')"""),

    ("md", """## Conclusions

1. **Random Forest clearly beats Logistic Regression here.** The fraud patterns are
   *interactions* (large amount **and** high velocity **and** odd hour), and a linear model
   cannot represent an interaction without being told to.
2. **Accuracy is meaningless at a ~2-3% fraud rate.** Predicting "normal" always scores
   ~97%. PR-AUC and the precision/recall pair are the metrics that mean anything.
3. **Behavioural features beat raw amount.** `seconds_since_prev`, `txn_count_1h` and
   `amount_ratio` outrank `amount` itself - what matters is not how big a transaction is,
   but how unusual it is *for that account*.
4. **The model is advisory only.** In `app/banking.py` the risk score can flag or block a
   transfer, but balance, account status and limit checks all still run and still win."""),
]


def build():
    cells = []
    for kind, source in CELLS:
        lines = source.split("\n")
        if kind == "md":
            cells.append({"cell_type": "markdown", "metadata": {},
                          "source": source})
        else:
            cells.append({"cell_type": "code", "execution_count": None,
                          "metadata": {}, "outputs": [], "source": source})

    notebook = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python",
                           "name": "python3"},
            "language_info": {"name": "python", "version": "3.14"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    path = Path(__file__).parent / "ml_experiments.ipynb"
    path.write_text(json.dumps(notebook, indent=1))
    return path


if __name__ == "__main__":
    print(f"wrote {build()}")
