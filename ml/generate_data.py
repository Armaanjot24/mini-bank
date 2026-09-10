import random
from datetime import datetime, timedelta
from decimal import Decimal

from app import database as db

CENT = Decimal("0.01")

FIRST_NAMES = ["Asha", "Bilal", "Chitra", "Dev", "Esha", "Farid", "Gita", "Harsh",
               "Iqbal", "Jaya", "Kabir", "Lata", "Manav", "Nisha", "Omar", "Priya",
               "Rahul", "Sana", "Tarun", "Uma", "Vikram", "Wasim", "Yash", "Zoya"]
LAST_NAMES = ["Rao", "Khan", "Sharma", "Nair", "Iyer", "Patel", "Bose", "Gupta",
              "Menon", "Reddy", "Singh", "Das", "Joshi", "Mehta", "Kapoor", "Shah"]

FRAUD_PATTERNS = ("AMOUNT_SPIKE", "VELOCITY", "ODD_HOUR", "ACCOUNT_DRAIN")

NORMAL_HOUR_WEIGHTS = {h: (0.2 if h < 7 else 6.0 if 9 <= h <= 21 else 1.5)
                       for h in range(24)}


def _money(value) -> Decimal:
    return Decimal(str(round(float(value), 2))).quantize(CENT)


def _account_number(rng) -> str:
    return "".join(rng.choices("0123456789", k=12))


def _normal_hour(rng) -> int:
    hours = list(NORMAL_HOUR_WEIGHTS)
    return rng.choices(hours, weights=[NORMAL_HOUR_WEIGHTS[h] for h in hours])[0]


def wipe():
    with db.transaction() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for table in ("audit_logs", "transactions", "transfers", "beneficiaries",
                      "users", "accounts", "customers"):
            cur.execute(f"TRUNCATE TABLE {table}")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")


def create_population(n_customers, rng):
    customers, accounts = [], []
    used_numbers = set()

    for i in range(1, n_customers + 1):
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        customers.append((
            i, name, f"user{i}@example.com", f"9{i:09d}",
            f"{rng.randint(1960, 2004)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
        ))

    account_id = 0
    for customer_id in range(1, n_customers + 1):
        for _ in range(rng.choices([1, 2], weights=[7, 3])[0]):
            account_id += 1
            number = _account_number(rng)
            while number in used_numbers:
                number = _account_number(rng)
            used_numbers.add(number)
            accounts.append({
                "account_id": account_id,
                "customer_id": customer_id,
                "account_number": number,
                "account_type": rng.choices(["SAVINGS", "CURRENT"], weights=[7, 3])[0],
                "base_amount": rng.uniform(800, 9000),
                "daily_events": rng.uniform(0.4, 2.2),
                "opening": rng.uniform(50000, 250000),
            })

    with db.transaction() as cur:
        cur.executemany(
            """
            INSERT INTO customers (customer_id, full_name, email, phone, date_of_birth)
            VALUES (%s, %s, %s, %s, %s)
            """,
            customers,
        )
        cur.executemany(
            """
            INSERT INTO accounts
                (account_id, customer_id, account_number, account_type, balance,
                 daily_transfer_limit, max_txn_amount, opened_at)
            VALUES (%s, %s, %s, %s, 0.00, 500000.00, 200000.00, %s)
            """,
            [(a["account_id"], a["customer_id"], a["account_number"],
              a["account_type"], datetime.now() - timedelta(days=180))
             for a in accounts],
        )
    return accounts


def _normal_events(account, start, days, rng):
    events = []
    for day in range(days):
        n = 0
        expected = account["daily_events"]
        while rng.random() < expected:
            n += 1
            expected -= 1
        for _ in range(n):
            ts = start + timedelta(days=day, hours=_normal_hour(rng),
                                   minutes=rng.randint(0, 59),
                                   seconds=rng.randint(0, 59))
            kind = rng.choices(["DEPOSIT", "WITHDRAWAL", "TRANSFER"],
                               weights=[40, 38, 22])[0]
            amount = account["base_amount"] * rng.lognormvariate(0, 0.35)
            events.append({"ts": ts, "account_id": account["account_id"],
                           "kind": kind, "amount": amount, "is_fraud": 0,
                           "pattern": None})
    return events


def _fraud_episode(account, start, days, rng):
    pattern = rng.choice(FRAUD_PATTERNS)
    base = account["base_amount"]
    day = rng.randint(int(days * 0.3), days - 1)
    events = []

    if pattern == "AMOUNT_SPIKE":
        ts = start + timedelta(days=day, hours=_normal_hour(rng),
                               minutes=rng.randint(0, 59))
        events.append({"ts": ts, "kind": "WITHDRAWAL",
                       "amount": base * rng.uniform(9, 22)})

    elif pattern == "VELOCITY":
        ts = start + timedelta(days=day, hours=rng.randint(10, 20),
                               minutes=rng.randint(0, 40))
        for i in range(rng.randint(6, 12)):
            events.append({"ts": ts + timedelta(seconds=i * rng.randint(20, 70)),
                           "kind": "TRANSFER",
                           "amount": base * rng.uniform(0.9, 2.4)})

    elif pattern == "ODD_HOUR":
        ts = start + timedelta(days=day, hours=rng.choice([1, 2, 3, 4]),
                               minutes=rng.randint(0, 59))
        for i in range(rng.randint(2, 4)):
            events.append({"ts": ts + timedelta(minutes=i * rng.randint(3, 15)),
                           "kind": "WITHDRAWAL",
                           "amount": base * rng.uniform(2.5, 6.0)})

    else:
        ts = start + timedelta(days=day, hours=rng.randint(0, 23),
                               minutes=rng.randint(0, 59))
        amount = base * rng.uniform(3, 6)
        for i in range(rng.randint(4, 8)):
            events.append({"ts": ts + timedelta(minutes=i * rng.randint(2, 9)),
                           "kind": "TRANSFER", "amount": amount})
            amount *= rng.uniform(1.15, 1.6)

    for event in events:
        event.update({"account_id": account["account_id"], "is_fraud": 1,
                      "pattern": pattern})
    return events


def generate(n_customers=60, days=120, fraud_account_ratio=0.40, seed=42,
             reset=True, verbose=True):
    rng = random.Random(seed)
    if reset:
        wipe()

    accounts = create_population(n_customers, rng)
    start = datetime.now() - timedelta(days=days)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)

    events = []
    for account in accounts:
        events.append({"ts": start - timedelta(hours=1),
                       "account_id": account["account_id"], "kind": "DEPOSIT",
                       "amount": account["opening"], "is_fraud": 0,
                       "pattern": "OPENING"})
        events.extend(_normal_events(account, start, days, rng))

    n_fraud_accounts = max(1, int(len(accounts) * fraud_account_ratio))
    for account in rng.sample(accounts, n_fraud_accounts):
        for _ in range(rng.randint(1, 3)):
            events.extend(_fraud_episode(account, start, days, rng))

    events.sort(key=lambda e: e["ts"])

    balances = {a["account_id"]: Decimal("0.00") for a in accounts}
    account_ids = list(balances)
    transfer_rows, txn_rows = [], []
    transfer_id = 0
    skipped = 0

    for seq, event in enumerate(events):
        account_id = event["account_id"]
        amount = _money(max(event["amount"], 10))
        ts = event["ts"]
        kind = event["kind"]
        balance = balances[account_id]

        if kind == "DEPOSIT":
            after = balance + amount
            balances[account_id] = after
            txn_rows.append((account_id, None, "DEPOSIT", amount, balance, after,
                             "SUCCESS", f"GEN-{seq}-D", None, None, None,
                             event["is_fraud"], ts))

        elif kind == "WITHDRAWAL":
            if amount > balance:
                skipped += 1
                continue
            after = balance - amount
            balances[account_id] = after
            txn_rows.append((account_id, None, "WITHDRAWAL", amount, balance, after,
                             "SUCCESS", f"GEN-{seq}-W", None, None, None,
                             event["is_fraud"], ts))

        else:
            if amount > balance:
                skipped += 1
                continue
            target = rng.choice(account_ids)
            while target == account_id:
                target = rng.choice(account_ids)

            transfer_id += 1
            transfer_rows.append((transfer_id, account_id, target, amount,
                                  "COMPLETED", f"GEN-{seq}-T", ts))

            src_after = balance - amount
            balances[account_id] = src_after
            tgt_before = balances[target]
            tgt_after = tgt_before + amount
            balances[target] = tgt_after

            txn_rows.append((account_id, transfer_id, "TRANSFER_OUT", amount,
                             balance, src_after, "SUCCESS", f"GEN-{seq}-TO",
                             None, None, None, event["is_fraud"], ts))
            txn_rows.append((target, transfer_id, "TRANSFER_IN", amount,
                             tgt_before, tgt_after, "SUCCESS", f"GEN-{seq}-TI",
                             None, None, None, 0, ts))

    with db.transaction() as cur:
        cur.executemany(
            """
            INSERT INTO transfers (transfer_id, from_account_id, to_account_id,
                                   amount, status, reference, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            transfer_rows,
        )
        for i in range(0, len(txn_rows), 5000):
            cur.executemany(
                """
                INSERT INTO transactions
                    (account_id, transfer_id, transaction_type, amount,
                     balance_before, balance_after, status, reference,
                     risk_score, risk_status, risk_reason, is_fraud, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                txn_rows[i:i + 5000],
            )
        cur.executemany(
            "UPDATE accounts SET balance = %s WHERE account_id = %s",
            [(balances[a], a) for a in account_ids],
        )

    summary = {
        "customers": n_customers,
        "accounts": len(accounts),
        "transfers": len(transfer_rows),
        "transactions": len(txn_rows),
        "fraud_rows": sum(r[11] for r in txn_rows),
        "fraud_rate": round(sum(r[11] for r in txn_rows) / len(txn_rows), 4),
        "skipped_insufficient_funds": skipped,
        "days": days,
    }
    if verbose:
        for key, value in summary.items():
            print(f"  {key:28} {value}")
    return summary


if __name__ == "__main__":
    generate()
