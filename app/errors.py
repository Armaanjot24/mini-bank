class BankingError(Exception):
    pass


class ValidationError(BankingError):
    pass


class NotFoundError(BankingError):
    pass


class InsufficientFunds(BankingError):
    pass


class AccountNotActive(BankingError):
    pass


class LimitExceeded(BankingError):
    pass


class AuthError(BankingError):
    pass


class RiskBlocked(BankingError):
    pass
