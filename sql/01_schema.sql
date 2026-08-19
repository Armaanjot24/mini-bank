DROP DATABASE IF EXISTS mini_bank;
CREATE DATABASE mini_bank CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE mini_bank;


CREATE TABLE customers (
    customer_id   INT UNSIGNED NOT NULL AUTO_INCREMENT,
    full_name     VARCHAR(100) NOT NULL,
    email         VARCHAR(120) NOT NULL,
    phone         VARCHAR(15)  NOT NULL,
    date_of_birth DATE         NOT NULL,
    status        ENUM('ACTIVE','INACTIVE','CLOSED') NOT NULL DEFAULT 'ACTIVE',
    created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    PRIMARY KEY (customer_id),
    UNIQUE KEY uq_customers_email (email),
    UNIQUE KEY uq_customers_phone (phone),

    CONSTRAINT chk_customers_email CHECK (email LIKE '%_@_%.__%'),
    CONSTRAINT chk_customers_name  CHECK (CHAR_LENGTH(TRIM(full_name)) >= 2),
    CONSTRAINT chk_customers_phone CHECK (phone REGEXP '^[0-9]{10,15}$')
) ENGINE=InnoDB;


CREATE TABLE users (
    user_id               INT UNSIGNED NOT NULL AUTO_INCREMENT,
    customer_id           INT UNSIGNED NULL,
    username              VARCHAR(50)  NOT NULL,
    password_hash         VARCHAR(255) NOT NULL,
    role                  ENUM('CUSTOMER','TELLER','ADMIN') NOT NULL DEFAULT 'CUSTOMER',
    status                ENUM('ACTIVE','LOCKED','DISABLED') NOT NULL DEFAULT 'ACTIVE',
    failed_login_attempts TINYINT UNSIGNED NOT NULL DEFAULT 0,
    last_login_at         DATETIME(3)  NULL,
    created_at            DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    PRIMARY KEY (user_id),
    UNIQUE KEY uq_users_username (username),
    UNIQUE KEY uq_users_customer (customer_id),

    CONSTRAINT fk_users_customer FOREIGN KEY (customer_id)
        REFERENCES customers(customer_id) ON DELETE CASCADE,

    CONSTRAINT chk_users_role CHECK (
        (role  = 'CUSTOMER' AND customer_id IS NOT NULL) OR
        (role <> 'CUSTOMER')
    ),
    CONSTRAINT chk_users_username CHECK (CHAR_LENGTH(username) >= 3)
) ENGINE=InnoDB;


CREATE TABLE accounts (
    account_id           INT UNSIGNED  NOT NULL AUTO_INCREMENT,
    customer_id          INT UNSIGNED  NOT NULL,
    account_number       CHAR(12)      NOT NULL,
    account_type         ENUM('SAVINGS','CURRENT') NOT NULL,
    balance              DECIMAL(15,2) NOT NULL DEFAULT 0.00,
    currency             CHAR(3)       NOT NULL DEFAULT 'INR',
    status               ENUM('ACTIVE','FROZEN','CLOSED') NOT NULL DEFAULT 'ACTIVE',
    daily_transfer_limit DECIMAL(15,2) NOT NULL DEFAULT 100000.00,
    max_txn_amount       DECIMAL(15,2) NOT NULL DEFAULT 50000.00,
    opened_at            DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    PRIMARY KEY (account_id),
    UNIQUE KEY uq_accounts_number (account_number),

    CONSTRAINT fk_accounts_customer FOREIGN KEY (customer_id)
        REFERENCES customers(customer_id) ON DELETE RESTRICT,

    CONSTRAINT chk_accounts_balance CHECK (balance >= 0),
    CONSTRAINT chk_accounts_number  CHECK (account_number REGEXP '^[0-9]{12}$'),
    CONSTRAINT chk_accounts_limits  CHECK (daily_transfer_limit > 0 AND max_txn_amount > 0)
) ENGINE=InnoDB;


CREATE TABLE transfers (
    transfer_id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    from_account_id INT UNSIGNED    NOT NULL,
    to_account_id   INT UNSIGNED    NOT NULL,
    amount          DECIMAL(15,2)   NOT NULL,
    status          ENUM('COMPLETED','FAILED','FLAGGED','REVERSED') NOT NULL,
    reference       VARCHAR(64)     NOT NULL,
    failure_reason  VARCHAR(255)    NULL,
    initiated_by    INT UNSIGNED    NULL,
    created_at      DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    PRIMARY KEY (transfer_id),
    UNIQUE KEY uq_transfers_reference (reference),

    CONSTRAINT fk_transfers_from FOREIGN KEY (from_account_id)
        REFERENCES accounts(account_id) ON DELETE RESTRICT,
    CONSTRAINT fk_transfers_to   FOREIGN KEY (to_account_id)
        REFERENCES accounts(account_id) ON DELETE RESTRICT,
    CONSTRAINT fk_transfers_user FOREIGN KEY (initiated_by)
        REFERENCES users(user_id) ON DELETE SET NULL,

    CONSTRAINT chk_transfers_amount CHECK (amount > 0),
    CONSTRAINT chk_transfers_self   CHECK (from_account_id <> to_account_id)
) ENGINE=InnoDB;


CREATE TABLE transactions (
    transaction_id   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    account_id       INT UNSIGNED    NOT NULL,
    transfer_id      BIGINT UNSIGNED NULL,
    transaction_type ENUM('DEPOSIT','WITHDRAWAL','TRANSFER_IN','TRANSFER_OUT') NOT NULL,
    amount           DECIMAL(15,2)   NOT NULL,
    balance_before   DECIMAL(15,2)   NULL,
    balance_after    DECIMAL(15,2)   NULL,
    status           ENUM('SUCCESS','FAILED','FLAGGED') NOT NULL DEFAULT 'SUCCESS',
    reference        VARCHAR(64)     NOT NULL,
    failure_reason   VARCHAR(255)    NULL,
    risk_score       DECIMAL(5,4)    NULL,
    risk_status      ENUM('LOW','MEDIUM','HIGH') NULL,
    risk_reason      VARCHAR(255)    NULL,
    is_fraud         TINYINT UNSIGNED NOT NULL DEFAULT 0,
    created_at       DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    PRIMARY KEY (transaction_id),
    UNIQUE KEY uq_transactions_reference (reference),

    CONSTRAINT fk_txn_account  FOREIGN KEY (account_id)
        REFERENCES accounts(account_id) ON DELETE RESTRICT,
    CONSTRAINT fk_txn_transfer FOREIGN KEY (transfer_id)
        REFERENCES transfers(transfer_id) ON DELETE RESTRICT,

    CONSTRAINT chk_txn_amount CHECK (amount > 0),

    CONSTRAINT chk_txn_transfer_link CHECK (
        (transaction_type IN ('TRANSFER_IN','TRANSFER_OUT') AND transfer_id IS NOT NULL) OR
        (transaction_type IN ('DEPOSIT','WITHDRAWAL')       AND transfer_id IS NULL)
    ),

    CONSTRAINT chk_txn_settled CHECK (
        status = 'FAILED' OR (balance_before IS NOT NULL AND balance_after IS NOT NULL)
    ),

    CONSTRAINT chk_txn_balance_math CHECK (
        balance_before IS NULL OR
        (transaction_type IN ('DEPOSIT','TRANSFER_IN')
             AND balance_after = balance_before + amount) OR
        (transaction_type IN ('WITHDRAWAL','TRANSFER_OUT')
             AND balance_after = balance_before - amount)
    ),

    CONSTRAINT chk_txn_balances_nonneg CHECK (
        (balance_before IS NULL OR balance_before >= 0) AND
        (balance_after  IS NULL OR balance_after  >= 0)
    ),

    CONSTRAINT chk_txn_risk_score CHECK (
        risk_score IS NULL OR (risk_score >= 0 AND risk_score <= 1)
    ),

    CONSTRAINT chk_txn_is_fraud CHECK (is_fraud IN (0,1))
) ENGINE=InnoDB;


CREATE TABLE beneficiaries (
    beneficiary_id INT UNSIGNED NOT NULL AUTO_INCREMENT,
    customer_id    INT UNSIGNED NOT NULL,
    account_id     INT UNSIGNED NOT NULL,
    nickname       VARCHAR(50)  NOT NULL,
    created_at     DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    PRIMARY KEY (beneficiary_id),
    UNIQUE KEY uq_benef_customer_account (customer_id, account_id),
    UNIQUE KEY uq_benef_customer_nickname (customer_id, nickname),

    CONSTRAINT fk_benef_customer FOREIGN KEY (customer_id)
        REFERENCES customers(customer_id) ON DELETE CASCADE,
    CONSTRAINT fk_benef_account  FOREIGN KEY (account_id)
        REFERENCES accounts(account_id) ON DELETE CASCADE
) ENGINE=InnoDB;


CREATE TABLE audit_logs (
    log_id      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id     INT UNSIGNED    NULL,
    account_id  INT UNSIGNED    NULL,
    action      ENUM('LOGIN','LOGIN_FAILED','LOGOUT','ACCOUNT_CREATED',
                     'DEPOSIT','WITHDRAWAL','TRANSFER','FAILED_TRANSACTION',
                     'BENEFICIARY_ADDED','ACCOUNT_FROZEN') NOT NULL,
    entity_type VARCHAR(32)     NULL,
    entity_id   BIGINT UNSIGNED NULL,
    details     JSON            NULL,
    ip_address  VARCHAR(45)     NULL,
    created_at  DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    PRIMARY KEY (log_id),

    CONSTRAINT fk_audit_user    FOREIGN KEY (user_id)
        REFERENCES users(user_id) ON DELETE SET NULL,
    CONSTRAINT fk_audit_account FOREIGN KEY (account_id)
        REFERENCES accounts(account_id) ON DELETE SET NULL
) ENGINE=InnoDB;
