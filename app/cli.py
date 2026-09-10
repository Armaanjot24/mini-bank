import argparse
import sys
from decimal import Decimal

import pymysql

from app import analytics, auth, banking, config, customers, experiments
from app import database as db
from app.errors import BankingError


def _print_rows(rows, columns=None, limit=None):
    rows = list(rows)
    if not rows:
        print("  (no rows)")
        return
    if limit:
        rows = rows[:limit]
    columns = columns or list(rows[0].keys())
    widths = {c: max(len(str(c)), max(len(str(r.get(c, ""))) for r in rows))
              for c in columns}
    print("  " + "  ".join(str(c).ljust(widths[c]) for c in columns))
    print("  " + "  ".join("-" * widths[c] for c in columns))
    for row in rows:
        print("  " + "  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))


def _run_sql_file(path, database=True):
    """database=False connects to the server with no schema selected, which is how
    01_schema.sql can CREATE the database it later USEs."""
    statements = [s.strip() for s in path.read_text().split(";") if s.strip()]
    conn = (db.get_connection(autocommit=True) if database else
            pymysql.connect(host=config.MYSQL_HOST, user=config.MYSQL_USER,
                            password=config.MYSQL_PASSWORD, autocommit=True))
    try:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
    finally:
        conn.close()
    return len(statements)


def cmd_setup(args):
    sql_dir = config.BASE_DIR / "sql"
    files = ["01_schema.sql", "03_views.sql"]
    if args.with_indexes:
        files.append("04_indexes.sql")
    for name in files:
        count = _run_sql_file(sql_dir / name, database=(name != "01_schema.sql"))
        print(f"  applied {name} ({count} statements)")


def cmd_seed(args):
    people = [
        ("Asha Rao", "asha@example.com", "9000000001", "1995-04-12", "SAVINGS", 25000),
        ("Bilal Khan", "bilal@example.com", "9000000002", "1990-01-30", "CURRENT", 48000),
        ("Chitra Nair", "chitra@example.com", "9000000003", "1998-11-05", "SAVINGS", 12000),
    ]
    created = []
    for name, email, phone, dob, account_type, opening in people:
        customer_id = customers.create_customer(name, email, phone, dob)
        account = customers.create_account(customer_id, account_type, opening)
        username = email.split("@")[0]
        auth.register_user(username, "password123", customer_id=customer_id)
        created.append({"customer_id": customer_id, "name": name,
                        "account_id": account["account_id"],
                        "account_number": account["account_number"],
                        "balance": account["balance"], "login": username})
    auth.register_user("admin", "adminpass123", role="ADMIN")
    _print_rows(created)
    print("\n  all logins use password 'password123' (admin: 'adminpass123')")


def cmd_balance(args):
    account = customers.get_account(args.account_id)
    print(f"  account {account['account_number']} ({account['account_type']}, "
          f"{account['status']}): {account['currency']} {account['balance']}")


def _cmd_movement(verb, fn):
    def run(args):
        result = fn(args.account_id, args.amount)
        print(f"  {verb} {args.amount}: {result['balance_before']} -> "
              f"{result['balance_after']}  ref {result['reference']}")
    return run


cmd_deposit = _cmd_movement("deposited", banking.deposit)
cmd_withdraw = _cmd_movement("withdrew", banking.withdraw)


def cmd_transfer(args):
    result = banking.transfer(args.from_account, args.to_account, args.amount,
                              risk_check=not args.no_risk)
    print(f"  transfer {result['reference']} [{result['status']}]")
    print(f"    from balance -> {result['from_balance_after']}")
    print(f"    to   balance -> {result['to_balance_after']}")
    if result.get("risk_score") is not None:
        print(f"    risk {result['risk_score']} ({result['risk_status']}) "
              f"{result.get('risk_reason') or ''}")


def cmd_history(args):
    rows = banking.transaction_history(args.account_id, args.limit)
    _print_rows(rows, ["created_at", "transaction_type", "amount", "balance_after",
                       "status", "risk_status", "reference"])


REPORTS = [
    ("top customers by balance", lambda: analytics.top_customers_by_balance(10), None),
    ("transaction statistics", analytics.transaction_stats, None),
    ("monthly summary", analytics.monthly_summary, None),
    ("account ranking - window functions", lambda: analytics.account_ranking(8),
     ["account_number", "full_name", "total_volume", "volume_rank",
      "rank_in_type", "pct_of_total_volume"]),
    ("suspicious transactions", lambda: analytics.suspicious_report(8),
     ["created_at", "account_number", "amount", "risk_score", "risk_status",
      "is_fraud"]),
]


def cmd_report(args):
    for title, fetch, columns in REPORTS:
        print(f"\n[{title}]")
        _print_rows(fetch(), columns, limit=6)


def cmd_reconcile(args):
    mismatches = banking.reconcile()
    if not mismatches:
        print("  every account balance matches its ledger")
    else:
        print(f"  {len(mismatches)} MISMATCHED ACCOUNTS:")
        _print_rows(mismatches)


DEMOS = {
    "race":      ("Two concurrent withdrawals (800 and 700) against 1000",
                  lambda: experiments.lost_update_demo()),
    "rollback":  ("Crash injected after both balances were updated",
                  lambda: {"ROLLBACK": experiments.rollback_demo()}),
    "deadlock":  ("Simultaneous A->B and B->A transfers",
                  lambda: experiments.deadlock_demo()),
    "isolation": ("One session reads twice while another commits in between",
                  lambda: experiments.isolation_demo()),
    "limits":    ("Per-transaction and daily transfer limits",
                  lambda: {"LIMITS": experiments.limit_demo()}),
}


def cmd_demo(args):
    """Every demo returns {label: {field: value}}, so one printer serves them all.
    `outcomes` is the one list field and gets a line each."""
    caption, run = DEMOS[args.name]
    print(f"{caption}\n")
    for label, group in run().items():
        print(f"[{label}]")
        for outcome in group.pop("outcomes", []):
            print(f"    {outcome}")
        for field, value in group.items():
            print(f"    {field:24} {value}")
        print()


def cmd_benchmark(args):
    print(f"  benchmarking against account {args.account_id} "
          f"({args.repeats} runs per query)\n")
    results = analytics.index_benchmark(args.account_id, repeats=args.repeats)
    for name, phases in results.items():
        print(f"[{name}]")
        for phase in ("without_index", "with_index"):
            data = phases[phase]
            print(f"    {phase:14} {data['ms']:>8.4f} ms  "
                  f"type={str(data['type']):<6} key={str(data['key']):<26} "
                  f"rows={data['rows_examined']}")
            print(f"                   {data['tree'][:110]}")
        print(f"    speedup        {phases['speedup']}x\n")
    print("\n[table sizes]")
    _print_rows(analytics.table_sizes())


def cmd_ml(args):
    if args.action == "check":
        from ml.predict import score_account_amount
        print(f"  {score_account_amount(args.account_id, args.amount)}")
        return
    import importlib
    module, fn = {"generate": ("ml.generate_data", "generate"),
                  "train":    ("ml.train", "train"),
                  "evaluate": ("ml.evaluate", "evaluate"),
                  "score":    ("ml.predict", "backfill_scores")}[args.action]
    call = getattr(importlib.import_module(module), fn)
    call(n_customers=args.customers, days=args.days) if args.action == "generate" else call()


# name -> (help, [argparse arg specs], handler)
COMMANDS = {
    "setup":     ("create database, tables and views",
                  [("--with-indexes", {"action": "store_true"})], cmd_setup),
    "seed":      ("insert demo customers and accounts", [], cmd_seed),
    "balance":   ("show one account", [("account_id", {"type": int})], cmd_balance),
    "deposit":   ("pay money in",
                  [("account_id", {"type": int}), ("amount", {"type": Decimal})],
                  cmd_deposit),
    "withdraw":  ("take money out",
                  [("account_id", {"type": int}), ("amount", {"type": Decimal})],
                  cmd_withdraw),
    "transfer":  ("move money between two accounts",
                  [("from_account", {"type": int}), ("to_account", {"type": int}),
                   ("amount", {"type": Decimal}),
                   ("--no-risk", {"action": "store_true"})], cmd_transfer),
    "history":   ("recent transactions for an account",
                  [("account_id", {"type": int}),
                   ("--limit", {"type": int, "default": 15})], cmd_history),
    "report":    ("analytics views", [], cmd_report),
    "reconcile": ("check every balance against the ledger", [], cmd_reconcile),
    "demo":      ("concurrency and transaction experiments",
                  [("name", {"choices": list(DEMOS)})], cmd_demo),
    "benchmark": ("index EXPLAIN + timing comparison",
                  [("account_id", {"type": int}),
                   ("--repeats", {"type": int, "default": 30})], cmd_benchmark),
    "ml":        ("generate / train / evaluate / score the fraud model",
                  [("action", {"choices": ["generate", "train", "evaluate",
                                           "score", "check"]}),
                   ("--customers", {"type": int, "default": 60}),
                   ("--days", {"type": int, "default": 120}),
                   ("--account-id", {"type": int, "default": 1}),
                   ("--amount", {"type": Decimal, "default": Decimal("10000")})],
                  cmd_ml),
}


def build_parser():
    parser = argparse.ArgumentParser(prog="mini-bank")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, (help_text, arguments, func) in COMMANDS.items():
        child = sub.add_parser(name, help=help_text)
        for flag, options in arguments:
            child.add_argument(flag, **options)
        child.set_defaults(func=func)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except BankingError as exc:
        print(f"  {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
