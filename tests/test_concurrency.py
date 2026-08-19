from decimal import Decimal

from app import banking, experiments


def test_row_locking_prevents_the_lost_update(new_account):
    account_id = new_account(balance=1000)

    outcomes = experiments._run_concurrently(
        experiments.safe_withdraw, [(account_id, 800), (account_id, 700)]
    )

    succeeded = sum("succeeded" in outcome for outcome in outcomes)
    assert succeeded == 1, outcomes

    balance = banking.get_balance(account_id)
    assert balance in (Decimal("200.00"), Decimal("300.00")), balance
    assert banking.reconcile(account_id) == ()


def test_without_locking_the_race_actually_corrupts_the_ledger(new_account):
    account_id = new_account(balance=1000)

    experiments._run_concurrently(
        experiments.unsafe_withdraw, [(account_id, 800), (account_id, 700)]
    )

    mismatches = banking.reconcile(account_id)
    assert mismatches, "expected the unlocked race to break ledger consistency"


def test_concurrent_transfers_conserve_money(new_account):
    a = new_account(balance=5000)
    b = new_account(balance=5000)
    total_before = banking.get_balance(a) + banking.get_balance(b)

    def send(from_id, to_id, amount):
        try:
            banking.transfer(from_id, to_id, amount, risk_check=False)
            return "ok"
        except Exception as exc:
            return type(exc).__name__

    experiments._run_concurrently(
        send,
        [(a, b, 100), (b, a, 150), (a, b, 200), (b, a, 250), (a, b, 300)],
    )

    assert banking.get_balance(a) + banking.get_balance(b) == total_before
    assert banking.reconcile(a) == ()
    assert banking.reconcile(b) == ()


def test_ordered_locking_avoids_deadlocks():
    report = experiments.deadlock_demo()
    assert report["ORDERED BY account_id"]["deadlocks"] == 0


def test_repeatable_read_does_not_see_other_commits():
    report = experiments.isolation_demo()
    assert report["REPEATABLE READ"]["saw_other_commit"] is False
    assert report["READ COMMITTED"]["saw_other_commit"] is True


def test_many_concurrent_withdrawals_never_overdraw(new_account):
    account_id = new_account(balance=1000)

    experiments._run_concurrently(
        experiments.safe_withdraw,
        [(account_id, 100, 0.02) for _ in range(15)],
    )

    balance = banking.get_balance(account_id)
    assert balance >= 0
    assert balance == Decimal("0.00")
    assert banking.reconcile(account_id) == ()
