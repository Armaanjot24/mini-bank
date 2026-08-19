from decimal import Decimal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import analytics, auth, banking, customers
from app.errors import (
    AccountNotActive,
    AuthError,
    InsufficientFunds,
    LimitExceeded,
    NotFoundError,
    RiskBlocked,
    ValidationError,
)

app = FastAPI(
    title="Mini Banking System",
    description="MySQL-backed banking core with ACID transfers and an ML fraud-risk layer",
    version="1.0.0",
)

STATUS_FOR = {
    NotFoundError: 404,
    ValidationError: 400,
    AccountNotActive: 409,
    InsufficientFunds: 409,
    LimitExceeded: 409,
    RiskBlocked: 403,
    AuthError: 401,
}


def _handle(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except tuple(STATUS_FOR) as exc:
        raise HTTPException(status_code=STATUS_FOR[type(exc)], detail=str(exc)) from exc


class CustomerIn(BaseModel):
    full_name: str
    email: str
    phone: str
    date_of_birth: str


class AccountIn(BaseModel):
    customer_id: int
    account_type: str = "SAVINGS"
    initial_deposit: Decimal = Decimal("0.00")


class AmountIn(BaseModel):
    amount: Decimal = Field(gt=0)


class TransferIn(BaseModel):
    from_account_id: int
    to_account_id: int
    amount: Decimal = Field(gt=0)
    risk_check: bool = True


class LoginIn(BaseModel):
    username: str
    password: str


@app.get("/health")
def health():
    from app import database as db
    return db.ping()


@app.post("/customers", status_code=201)
def create_customer(payload: CustomerIn):
    customer_id = _handle(customers.create_customer, payload.full_name,
                          payload.email, payload.phone, payload.date_of_birth)
    return _handle(customers.get_customer, customer_id)


@app.get("/customers/{customer_id}")
def get_customer(customer_id: int):
    return _handle(customers.get_customer, customer_id)


@app.get("/customers/{customer_id}/accounts")
def list_accounts(customer_id: int):
    return _handle(customers.list_accounts, customer_id)


@app.post("/accounts", status_code=201)
def create_account(payload: AccountIn):
    return _handle(customers.create_account, payload.customer_id,
                   payload.account_type, payload.initial_deposit)


@app.get("/accounts/{account_id}")
def get_account(account_id: int):
    return _handle(customers.get_account, account_id)


@app.post("/accounts/{account_id}/deposit")
def deposit(account_id: int, payload: AmountIn):
    return _handle(banking.deposit, account_id, payload.amount)


@app.post("/accounts/{account_id}/withdraw")
def withdraw(account_id: int, payload: AmountIn):
    return _handle(banking.withdraw, account_id, payload.amount)


@app.get("/accounts/{account_id}/transactions")
def transactions(account_id: int, limit: int = 25):
    _handle(customers.get_account, account_id)
    return banking.transaction_history(account_id, limit)


@app.post("/transfers")
def transfer(payload: TransferIn):
    return _handle(banking.transfer, payload.from_account_id, payload.to_account_id,
                   payload.amount, risk_check=payload.risk_check)


@app.post("/login")
def login(payload: LoginIn):
    return _handle(auth.login, payload.username, payload.password)


@app.get("/transactions/suspicious")
def suspicious(limit: int = 25):
    return analytics.suspicious_report(limit)


@app.get("/reports/customer-balances")
def customer_balances(limit: int = 20):
    return analytics.customer_balance_summary(limit)


@app.get("/reports/monthly")
def monthly():
    return analytics.monthly_summary()


@app.get("/reports/account-ranking")
def account_ranking(limit: int = 15):
    return analytics.account_ranking(limit)


@app.get("/reports/reconcile")
def reconcile():
    mismatches = banking.reconcile()
    return {"consistent": not mismatches, "mismatches": mismatches}


@app.get("/risk/score")
def risk_score(account_id: int, amount: Decimal):
    from ml.predict import score_account_amount
    try:
        return score_account_amount(account_id, amount)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
