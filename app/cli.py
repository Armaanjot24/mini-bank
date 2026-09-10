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


def cmd_report(args):
    print("\n[top customers by balance]")
    _print_rows(analytics.top_customers_by_balance(10))
    print("\n[transaction statistics]")
    _print_rows(analytics.transaction_stats())
    print("\n[monthly summary]")
    _print_rows(analytics.monthly_summary(), limit=6)
    print("\n[account ranking - window functions]")
    _print_rows(analytics.account_ranking(8),
                ["account_number", "full_name", "total_volume", "volume_rank",
                 "rank_in_type", "pct_of_total_volume"])
    print("\n[suspicious transactions]")
    _print_rows(analytics.suspicious_report(8),
                ["created_at", "account_number", "amount", "risk_score",
                 "risk_status", "is_fraud"])


def cmd_reconcile(args):
    mismatches = banking.reconcile()
    if not mismatches:
        print("  every account balance matches its ledger")
    else:
        print(f"  {len(mismatches)} MISMATCHED ACCOUNTS:")
        _print_rows(mismatches)


def cmd_demo(args):
    if args.name == "race":
        print("Two concurrent withdrawals (800 and 700) against a balance of 1000\n")
        for label, report in experiments.lost_update_demo().items():
            print(f"[{label}]")
            for outcome in report["outcomes"]:
                print(f"    {outcome}")
            print(f"    stored balance {report['stored_balance']}, "
                  f"{report['successful_withdrawals']} succeeded, "
                  f"total withdrawn {report['total_withdrawn']}")
            print(f"    ledger reconciles: {report['reconciles']}\n")

    elif args.name == "rollback":
        report = experiments.rollback_demo()
        print(f"  raised           : {report['error']}")
        print(f"  balances before  : {report['before']}")
        print(f"  balances after   : {report['after']}")
        print(f"  unchanged        : {report['unchanged']}")
        print(f"  transfer rows    : {report['transfer_rows_left']}")

    elif args.name == "deadlock":
        for label, report in experiments.deadlock_demo().items():
            print(f"[{label}] deadlocks={report['deadlocks']}")
            for outcome in report["outcomes"]:
                print(f"    {outcome}")

    elif args.name == "isolation":
        for level, report in experiments.isolation_demo().items():
            print(f"  {level:18} first={report['first_read']} "
                  f"second={report['second_read']} "
                  f"saw other commit={report['saw_other_commit']}")

    elif args.name == "limits":
        for outcome in experiments.limit_demo()["outcomes"]:
            print(f"    {outcome}")


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
    if args.action == "generate":
        from ml.generate_data import generate
        generate(n_customers=args.customers, days=args.days)
    elif args.action == "train":
        from ml.train import train
        train()
    elif args.action == "evaluate":
        from ml.evaluate import evaluate
        evaluate()
    elif args.action == "score":
        from ml.predict import backfill_scores
        backfill_scores()
    elif args.action == "check":
        from ml.predict import score_account_amount
        print(f"  {score_account_amount(args.account_id, args.amount)}")


def build_parser():
    parser = argparse.ArgumentParser(prog="mini-bank")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="create database, tables and views")
    setup.add_argument("--with-indexes", action="store_true")
    setup.set_defaults(func=cmd_setup)

    sub.add_parser("seed", help="insert demo customers and accounts").set_defaults(
        func=cmd_seed)

    balance = sub.add_parser("balance")
    balance.add_argument("account_id", type=int)
    balance.set_defaults(func=cmd_balance)

    deposit = sub.add_parser("deposit")
    deposit.add_argument("account_id", type=int)
    deposit.add_argument("amount", type=Decimal)
    deposit.set_defaults(func=cmd_deposit)

    withdraw = sub.add_parser("withdraw")
    withdraw.add_argument("account_id", type=int)
    withdraw.add_argument("amount", type=Decimal)
    withdraw.set_defaults(func=cmd_withdraw)

    transfer = sub.add_parser("transfer")
    transfer.add_argument("from_account", type=int)
    transfer.add_argument("to_account", type=int)
    transfer.add_argument("amount", type=Decimal)
    transfer.add_argument("--no-risk", action="store_true")
    transfer.set_defaults(func=cmd_transfer)

    history = sub.add_parser("history")
    history.add_argument("account_id", type=int)
    history.add_argument("--limit", type=int, default=15)
    history.set_defaults(func=cmd_history)

    sub.add_parser("report").set_defaults(func=cmd_report)
    sub.add_parser("reconcile").set_defaults(func=cmd_reconcile)

    demo = sub.add_parser("demo", help="concurrency and transaction experiments")
    demo.add_argument("name", choices=["race", "rollback", "deadlock",
                                       "isolation", "limits"])
    demo.set_defaults(func=cmd_demo)

    benchmark = sub.add_parser("benchmark", help="index EXPLAIN + timing comparison")
    benchmark.add_argument("account_id", type=int)
    benchmark.add_argument("--repeats", type=int, default=30)
    benchmark.set_defaults(func=cmd_benchmark)

    ml = sub.add_parser("ml")
    ml.add_argument("action", choices=["generate", "train", "evaluate", "score",
                                       "check"])
    ml.add_argument("--customers", type=int, default=60)
    ml.add_argument("--days", type=int, default=120)
    ml.add_argument("--account-id", type=int, default=1)
    ml.add_argument("--amount", type=Decimal, default=Decimal("10000"))
    ml.set_defaults(func=cmd_ml)

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
