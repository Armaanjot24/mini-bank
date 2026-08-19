# Mini Banking System — MySQL + Python + ML

A small banking backend where **the database enforces the banking rules**, transfers are
genuinely ACID, concurrent access is made safe with row-level locking, and a machine
learning layer scores transactions for fraud risk without ever being allowed to override
the banking safeguards.

No frontend, no ORM, no Docker. Raw SQL, ~3,100 lines of Python across 19 modules
(including 34 tests), 354 lines of SQL, MySQL 9.

---

## 1. The problem

A bank transfer looks trivial — subtract from one account, add to another — and is not.
Three things can go wrong, and all three are silent:

1. **Partial failure.** The debit succeeds, the process dies, the credit never happens.
   Money disappears.
2. **Concurrency.** Two withdrawals read the same balance at the same moment and both are
   approved. An account with ₹1,000 pays out ₹1,500.
3. **Silent corruption.** A bug writes a balance that does not match the transaction
   history, and nobody notices until an audit.

This project demonstrates the defences against each, and measures that they work.

---

## 2. Architecture

```
       CLI (app/cli.py)            FastAPI (app/api.py)          Jupyter
              |                            |                        |
              +------------+---------------+                        |
                           v                                        |
              Banking services (app/banking.py, customers.py, auth.py)
                           |                                        |
              +------------+------------+                           |
              v                         v                           v
       MySQL 9 (InnoDB)  <-------  ML risk layer (ml/)  <-----  training data
       7 tables, 16 CHECKs         RandomForest + LogisticRegression
       6 views, ACID transfers     features built with SQL window functions
```

The ML layer reads the same ledger the banking core writes. It is a **consumer** of the
database, never a gatekeeper in front of it.

---

## 3. Database schema

Seven tables. Nothing was added to make the diagram look impressive.

```
customers ──1:1(optional)── users
    │                          │ initiated_by
    │ 1:N                      v
    v                      transfers ──┐
 accounts ────────────────────┘        │ 1:N (two ledger legs per transfer)
    │  │                               v
    │  └───────1:N───────────────> transactions
    │ 1:N
    v
beneficiaries                    audit_logs
```

| table | role |
|---|---|
| `customers` | the person; `email`/`phone` UNIQUE |
| `users` | login; `customer_id` is NULLable **and** UNIQUE = optional 1:1 |
| `accounts` | the money; balance, status, per-account limits |
| `transfers` | one row per transfer event |
| `transactions` | **append-only ledger**, two legs per transfer |
| `beneficiaries` | saved payees; composite UNIQUE keys |
| `audit_logs` | append-only security trail with a JSON payload |

### The central idea

> **`accounts` holds state. `transactions` holds history. History is never updated.**

Balances change; ledger rows do not. That is what makes the history trustworthy — and it
is also why the ML model has anything to learn from.

### Design decisions worth defending

**`DECIMAL(15,2)` for money, never `FLOAT`.** Binary floating point cannot represent `0.10`
exactly (`0.1 + 0.2 != 0.3`). In a ledger that drift accumulates into money nobody can
account for. `DECIMAL` is exact base-10.

**`accounts.balance` is deliberately denormalized.** It is derivable by summing the ledger,
so storing it is redundant. It stays for two reasons, and the second is the important one:

1. O(1) reads instead of an aggregate that grows forever;
2. **it is the lock target.** You cannot `SELECT ... FOR UPDATE` a `SUM()`. You can only
   lock a physical row.

Redundancy is acceptable when you can name what it buys. Correctness is *proved*, not
assumed — `banking.reconcile()` compares every stored balance against its ledger sum, and
that query is a test.

**Every `ON DELETE` is a different, deliberate choice.**
`RESTRICT` on `accounts.customer_id` (never orphan money) · `CASCADE` on `users.customer_id`
· `SET NULL` on audit FKs (an audit trail you can erase by deleting a row is not an audit
trail).

### Rules MySQL enforces by itself

```sql
CHECK (balance >= 0)                          -- overdraft is unrepresentable
CHECK (from_account_id <> to_account_id)      -- self-transfer is impossible
CHECK (amount > 0)                            -- no zero or negative movements

CHECK (status <> 'SUCCESS' OR                 -- a settled row must record both balances
       (balance_before IS NOT NULL AND balance_after IS NOT NULL))

CHECK (balance_before IS NULL OR              -- the ledger cannot contradict itself
       (transaction_type IN ('DEPOSIT','TRANSFER_IN')
            AND balance_after = balance_before + amount) OR
       (transaction_type IN ('WITHDRAWAL','TRANSFER_OUT')
            AND balance_after = balance_before - amount))
```

Even with the Python layer bypassed entirely, MySQL refuses the write. 16 CHECK
constraints are active.

**Why two constraints instead of one combined check:** in SQL a `CHECK` passes unless it
evaluates to `FALSE`. An expression containing `NULL` evaluates to `NULL`, which is not
`FALSE` — so it passes. A single combined constraint would be silently bypassed by NULL
balances. `chk_txn_settled` closes that hole.

### What the database *cannot* enforce (and why)

- **"Customer must be 18+"** — `CHECK` forbids non-deterministic functions like `CURDATE()`,
  because a constraint must give the same answer forever. Lives in `customers.py`.
- **"You cannot add your own account as a beneficiary"** — `CHECK` cannot contain
  subqueries. Lives in `customers.py`.

Knowing where a constraint cannot go matters as much as knowing where it can.

---

## 4. Transfers and ACID

`app/banking.py::transfer` is the heart of the project.

```python
with db.transaction() as cur:                       # BEGIN
    locked = {aid: lock_account(cur, aid)           # SELECT ... FOR UPDATE
              for aid in sorted([from_id, to_id])}  # ordered! see below
    _require_active(source); _require_active(target)
    _check_max_txn(source, amount)
    _check_daily_limit(cur, source, amount)
    if source["balance"] < amount: raise InsufficientFunds
    risk = assess_risk(cur, from_id, amount)        # ML: advisory only
    INSERT INTO transfers ...                       # one event
    UPDATE accounts SET balance = ... (both)        # two balances
    INSERT INTO transactions ... (TRANSFER_OUT)     # two ledger legs
    INSERT INTO transactions ... (TRANSFER_IN)
    audit(...)
# COMMIT on success, ROLLBACK on any exception
```

**Why the transaction is needed:** the operation touches four tables. Without atomicity a
crash between the debit and the credit destroys money. `db.transaction()` is a context
manager that commits on clean exit and rolls back on *any* exception.

**Why `FOR UPDATE`:** a plain `SELECT` takes no lock, so two concurrent transfers can both
read a balance of ₹1,000 and both decide ₹800 is affordable. `FOR UPDATE` takes an
exclusive row lock held until COMMIT, forcing the second transaction to wait and then
re-read the *committed* value.

**Why locks are acquired in `sorted()` order:** if transfer A→B locks A then B, while B→A
locks B then A, each holds what the other needs and MySQL kills one with error 1213.
Sorting by `account_id` means every transaction acquires locks in the same global order,
which makes that cycle impossible. Measured below.

**Failed transactions need a second connection.** A failed transfer rolls back — which
would erase the `FAILED` ledger row too. `db.record_failure()` therefore writes on a
separate connection that commits independently.

---

## 5. Measured results

All numbers below were produced by the commands in section 8 on this repository.

### 5.1 The race condition is real

`python -m app.cli demo race` — two concurrent withdrawals (₹800 and ₹700) against ₹1,000:

| | outcome | stored balance | withdrawals recorded | total withdrawn | ledger reconciles |
|---|---|---|---|---|---|
| **no lock** | both read 1000, both succeed | ₹300.00 | 2 | **₹1,500.00** | **False** |
| **`FOR UPDATE`** | one succeeds, one re-reads and is refused | ₹200.00 | 1 | ₹800.00 | True |

Without the lock, ₹1,500 leaves a ₹1,000 account and the stored balance no longer matches
the ledger. This is the entire justification for the project.

### 5.2 Rollback is atomic

`python -m app.cli demo rollback` — crash injected *after* both balances were updated and
the transfer row inserted:

```
balances before   : (1000.00, 500.00)
balances after    : (1000.00, 500.00)     unchanged: True
transfer rows left: 0
```

### 5.3 Lock ordering eliminates deadlocks

`python -m app.cli demo deadlock` — simultaneous A→B and B→A:

| lock order | deadlocks |
|---|---|
| each transaction locks its own source first | **1** (`ERROR 1213`) |
| both sorted by `account_id` | **0** |

### 5.4 Isolation levels behave as documented

`python -m app.cli demo isolation` — read a balance, let another session commit, read again
inside the same transaction:

| isolation level | first read | second read | saw the other commit |
|---|---|---|---|
| `REPEATABLE READ` (MySQL default) | 1000.00 | 1000.00 | No |
| `READ COMMITTED` | 1100.00 | 1200.00 | Yes (non-repeatable read) |

### 5.5 Indexes

`python -m app.cli benchmark <account_id>`. Indexes are dropped, measured, recreated and
measured again, 40 runs each. Where an index is required by a foreign key, a single-column
stand-in is substituted so the "without" baseline is realistic rather than impossible.

| query | without | with | plan change | speedup |
|---|---|---|---|---|
| `fraud_scan` (`is_fraud=1 AND created_at >= ?`) | 3.21 ms | 0.21 ms | `ALL` (13,101 rows) → covering index range scan (177 rows) | **15.4×** |
| `account_statement` (`account_id=? ORDER BY created_at`) | 0.89 ms | 0.57 ms | FK index `ref` → composite `range` | 1.57× |
| `daily_transfer_total` | 0.54 ms | 0.43 ms | FK index `ref` → composite `range` | 1.27× |

(Absolute timings vary a few tenths of a millisecond between runs; the plan change and the
order of magnitude are the stable part.)

**The honest reading:** an index on a previously unindexed column (`is_fraud`) is
transformative — it turns a full table scan into a covering index scan. Adding
`created_at` to a column that *already* had a foreign-key index is a modest 25–30% gain,
because the FK index had already narrowed the search to 114 rows. Reporting both prevents
the usual "indexes made everything 100× faster" overclaim.

Note MySQL 9 changed `EXPLAIN`'s default output to the tree format; `analytics.py` asks for
`EXPLAIN FORMAT=TRADITIONAL` to read `type`/`key`/`rows`.

### 5.6 Test suite

```
34 passed in 6.5s
```

Covering deposits, withdrawals, insufficient funds, limits, frozen accounts, transfers,
money conservation, rollback, idempotency, DB-level constraint rejection, the lost-update
race with and without locking, deadlock avoidance, and isolation-level behaviour.

**Expected side effect:** after running the test suite or `demo race`, `python -m app.cli
reconcile` reports exactly one mismatched account — stored balance ₹200.00 against a ledger
balance of −₹500.00. That is the account the unlocked-race test *deliberately* corrupts, and
`test_without_locking_the_race_actually_corrupts_the_ledger` asserts the corruption is
there. It is the demonstration succeeding, not a bug. Every other account reconciles.

---

## 6. The ML layer

### Task

Binary classification: is this outgoing transaction suspicious? Trained on
`job history -> is_fraud`, which in a real bank comes from confirmed fraud reports and
chargebacks. Here it comes from a controlled synthetic generator so the ground truth is
known.

### Synthetic data

`ml/generate_data.py` gives each account a behavioural profile (typical amount, daily
frequency, active hours), then injects fraud episodes with **four explicit patterns**:

| pattern | what it looks like |
|---|---|
| `AMOUNT_SPIKE` | a single withdrawal 9–22× the account's normal amount |
| `VELOCITY` | 6–12 transfers within a few minutes |
| `ODD_HOUR` | 2–4 withdrawals between 1am and 4am on an account that never acts at night |
| `ACCOUNT_DRAIN` | 4–8 escalating transfers draining the balance |

The data is not noise — there is a real, describable function to learn. Generated dataset:
**13,552 transactions, 77 accounts, 120 days, 2.64% fraud among outgoing transactions.**
The generator maintains running balances so every row satisfies the ledger CHECK
constraints, and `reconcile()` returns zero mismatches afterwards.

### Features — built in SQL

Feature engineering happens in the database using window functions:

```sql
AVG(amount) OVER (PARTITION BY account_id ORDER BY created_at, transaction_id
                  ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)   -- historical average
COUNT(*)    OVER (PARTITION BY account_id ORDER BY created_at
                  RANGE BETWEEN INTERVAL 1 HOUR PRECEDING AND CURRENT ROW)  -- velocity
LAG(created_at) OVER (PARTITION BY account_id ORDER BY created_at)    -- time since previous
```

**`1 PRECEDING` is the anti-leakage bound**: a transaction is excluded from its own
historical average. 15 features total — amount, log amount, hour, is_night, is_weekend,
account age, prior transaction count, historical average, amount ratio, amount z-score,
seconds since previous, 1h and 24h velocity, prior failures, is_transfer.

### Results (out-of-sample, time-based split)

| model | threshold | precision | recall | F1 | ROC-AUC | PR-AUC | accuracy |
|---|---|---|---|---|---|---|---|
| Logistic Regression | 0.5 | 0.159 | 0.737 | 0.261 | 0.884 | 0.521 | 0.858 |
| Logistic Regression | tuned | 0.397 | 0.544 | 0.459 | 0.884 | 0.521 | 0.956 |
| **Random Forest** | **0.5** | **0.785** | **0.895** | **0.836** | **0.985** | **0.856** | **0.988** |
| *always predict NORMAL* | — | 0.000 | 0.000 | 0.000 | 0.500 | — | **0.966** |

Confusion matrix (test set, 1,674 rows, 57 fraudulent): `tn=1603 fp=14 fn=6 tp=51`.

**Why accuracy is not sufficient — the point of the last row.** A model that predicts
"normal" for every transaction scores **96.6% accuracy** and catches **zero fraud**. The
Random Forest's 98.8% looks only marginally better on that scale while being a completely
different product. Precision, recall and PR-AUC are the metrics that carry information at
a 3% positive rate.

**Why Random Forest beats Logistic Regression so decisively.** The injected fraud patterns
are *interactions*: a large amount **and** high velocity **and** an odd hour. A linear model
cannot represent an interaction unless you hand-build the cross terms. Trees find them for
free. This is a property of the problem, not evidence that "ensembles are better".

**Threshold selection is a business decision, not an ML one.** From the sweep in
`results/threshold_sweep.csv`:

| threshold | precision | recall | false alarms | missed fraud |
|---|---|---|---|---|
| 0.20 | 0.495 | 0.912 | 53 | 5 |
| 0.50 | 0.785 | 0.895 | 14 | 6 |
| 0.70 | 0.949 | 0.649 | 2 | 20 |

Whether 53 annoyed customers is worth catching one more fraud is a question for the bank,
not the model.

**Feature importance** (`results/feature_importance.png`):

| feature | importance |
|---|---|
| `seconds_since_prev` | 0.240 |
| `txn_count_1h` | 0.147 |
| `amount_zscore` | 0.117 |
| `amount_ratio` | 0.100 |
| `account_age_days` | 0.088 |
| `amount` | 0.057 |

Behavioural features beat raw amount. What matters is not how large a transaction is, but
how unusual it is **for that account** — which is exactly what the window functions compute.

### Integration — and its hard limit

Risk scoring runs inside `transfer()` after every banking validation has already passed:

```
transfer request
  -> lock accounts (ordered)  -> active? -> per-txn limit? -> daily limit? -> funds?
  -> ML risk score
  -> LOW: proceed   MEDIUM: proceed + record   HIGH: mark FLAGGED (or block, by policy)
  -> COMMIT
```

**The model can never approve anything.** It can only add suspicion. A `LOW` score does not
skip the balance check, the account-status check, or the limits — verified by a test that
asks the model to bless a transfer that then gets refused for insufficient funds anyway.

Every flagged transaction stores `risk_score`, `risk_status` and a human-readable
`risk_reason`:

```
amount is 8.6 standard deviations above normal; amount is 18.8x this account's historical average
only 13s since the previous transaction; 5 transactions already in the last hour
```

---

## 7. SQL analytics

Six views in `sql/03_views.sql`:

| view | demonstrates |
|---|---|
| `v_customer_balance_summary` | `LEFT JOIN` + `GROUP BY` aggregation |
| `v_monthly_transaction_summary` | conditional aggregation (`SUM(CASE WHEN ...)`) |
| `v_transaction_stats` | `AVG`/`MIN`/`MAX`/`STDDEV_SAMP` |
| `v_account_ranking` | **window functions**: `RANK()`, `RANK() OVER (PARTITION BY ...)`, `DENSE_RANK()`, `SUM() OVER ()` for percent-of-total |
| `v_suspicious_transactions` | the fraud report, joining ledger → account → customer |
| `v_daily_account_activity` | per-account daily rollup |

`analytics.running_balance()` also uses a windowed running total and `LAG()`.

---

## 8. How to run

### Requirements
MySQL 8.0+ (developed on 9.7), Python 3.11+.

```bash
pip install -r requirements.txt
```

### Configure

Copy `.env.example` to `.env` and fill it in:

```
MYSQL_HOST=localhost
MYSQL_USER=root
MYSQL_PASSWORD=your_password
MYSQL_DB=mini_bank
```

`.env` is gitignored. Nothing in the codebase hardcodes credentials — `app/config.py`
loads them with `python-dotenv` and raises a clear error if any are missing.

### Build the database

```bash
mysql -u root -p < sql/01_schema.sql      # 7 tables, 16 CHECK constraints
mysql -u root -p < sql/03_views.sql       # 6 analytics views
mysql -u root -p < sql/02_seed.sql        # optional: 3 demo customers
```

or, equivalently, from Python:

```bash
python -m app.cli setup --with-indexes
python -m app.cli seed
```

### Use it

```bash
python -m app.cli balance 1
python -m app.cli deposit 1 5000
python -m app.cli withdraw 1 1200
python -m app.cli transfer 1 2 750
python -m app.cli history 1
python -m app.cli report
python -m app.cli reconcile
```

### Run the DBMS experiments

```bash
python -m app.cli demo race          # lost update, with and without FOR UPDATE
python -m app.cli demo rollback      # atomicity under a simulated crash
python -m app.cli demo deadlock      # unordered vs ordered locking
python -m app.cli demo isolation     # REPEATABLE READ vs READ COMMITTED
python -m app.cli demo limits        # per-transaction and daily limits
python -m app.cli benchmark 35       # EXPLAIN + timing, with and without indexes
```

### Train the ML layer

```bash
python -m ml.generate_data     # ~13k synthetic transactions with labelled fraud
python -m ml.train             # LogisticRegression vs RandomForest
python -m ml.evaluate          # metrics, threshold sweep, plots -> results/
python -m ml.predict           # backfill risk scores onto historical transactions
python -m app.cli ml check --account-id 35 --amount 50000
```

### Tests

```bash
python -m pytest tests/ -q
```

### API

```bash
uvicorn app.api:app --reload
```

Then open <http://127.0.0.1:8000/docs> for Swagger. Endpoints: `POST /customers`,
`POST /accounts`, `POST /accounts/{id}/deposit`, `POST /accounts/{id}/withdraw`,
`POST /transfers`, `GET /accounts/{id}`, `GET /accounts/{id}/transactions`,
`GET /transactions/suspicious`, `GET /risk/score`, `GET /reports/*`, `POST /login`.

### Notebook

```bash
jupyter notebook notebooks/ml_experiments.ipynb
```

(Regenerate it with `python notebooks/build_notebook.py`.)

---

## 9. Project layout

```
.
├── sql/
│   ├── 01_schema.sql        7 tables, 16 CHECK constraints, no perf indexes yet
│   ├── 02_seed.sql          demo customers, accounts, logins
│   ├── 03_views.sql         6 analytics views incl. window functions
│   └── 04_indexes.sql       8 indexes, added in the benchmark phase
├── app/
│   ├── config.py            .env loading, thresholds, paths
│   ├── database.py          connections, transaction context manager, audit, failures
│   ├── errors.py            the banking exception hierarchy
│   ├── customers.py         customers, accounts, beneficiaries
│   ├── auth.py              bcrypt hashing, login, lockout
│   ├── banking.py           deposit, withdraw, transfer, reconcile
│   ├── analytics.py         reports, EXPLAIN, index benchmark
│   ├── experiments.py       race / rollback / deadlock / isolation demos
│   ├── cli.py               command-line interface
│   └── api.py               FastAPI
├── ml/
│   ├── generate_data.py     synthetic transactions with 4 fraud patterns
│   ├── features.py          SQL window-function feature extraction
│   ├── train.py             LogisticRegression vs RandomForest
│   ├── evaluate.py          metrics, threshold sweep, plots
│   └── predict.py           live scoring + explanation + backfill
├── tests/                   34 tests
├── notebooks/               ml_experiments.ipynb
├── data/                    trained model artifact
└── results/                 plots and CSVs
```

Deliberately flat. There is no `models/`, `services/`, `repositories/` three-layer split,
because at this size that would be three files of indirection for every one file of logic.

**No ORM.** Every query is visible SQL. For a project whose subject *is* the database,
hiding the SQL behind an ORM would hide the entire point.

**Threads, not processes, for the concurrency demos.** Each thread opens its own MySQL
connection, so the concurrency being tested is real database concurrency; the GIL is
irrelevant because the threads are blocked on socket I/O inside MySQL.

---

## 10. Limitations

Stated plainly, because a project that claims no limitations is not credible.

- **The fraud labels are synthetic.** Metrics measure how well the model recovers a
  generator I wrote. Real fraud is adversarial and drifts; these numbers would not survive
  contact with production.
- **The backfilled `risk_score` values on historical rows are in-sample** for the training
  portion. The honest out-of-sample numbers are the ones in section 6.
- **No authorised overdrafts.** `CHECK (balance >= 0)` means CURRENT accounts cannot go
  negative, which real current accounts can.
- **Session-less authentication.** `login()` verifies a bcrypt hash and writes an audit
  row; there are no tokens, and the API endpoints are unauthenticated.
- **Single currency in practice.** There is a `currency` column but no FX conversion, so
  cross-currency transfers are not handled.
- **Daily limits use server date**, with no timezone handling.
- **The benchmark dataset is ~13k rows.** Index effects at that size are directional;
  conclusions would sharpen at millions of rows.
- **No connection pooling.** Each operation opens a connection — fine for a CLI, wasteful
  under real API load.

## 11. Possible extensions

Scheduled transfers · statement generation · interest accrual · JWT sessions and
per-endpoint authorisation · connection pooling · partitioning `transactions` by month ·
a `REVERSED` flow for disputes · model retraining on a schedule with drift monitoring ·
comparing a `SELECT ... FOR UPDATE NOWAIT` fail-fast policy against the current blocking one.

---

## 12. What this project demonstrates

**DBMS** — normalization to 3NF with one defended denormalization · primary/foreign/unique
keys · 16 CHECK constraints · deliberate `ON DELETE` semantics per relationship · views ·
window functions · aggregation · indexing measured with `EXPLAIN`.

**Transactions and concurrency** — ACID in practice · `SELECT ... FOR UPDATE` · a
demonstrated and measured lost-update race · deadlock reproduction and prevention by lock
ordering · isolation-level comparison · rollback verified by test.

**Python** — a small flat codebase, raw SQL, context managers, a clean exception
hierarchy, bcrypt password hashing, argparse CLI, FastAPI, pytest.

**ML** — feature engineering in SQL with explicit anti-leakage bounds · time-based
splitting · imbalanced classification · precision/recall/F1/ROC-AUC/PR-AUC · threshold
selection as a business trade-off · explainable flags · and an integration where the model
is strictly advisory.
