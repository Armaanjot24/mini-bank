USE mini_bank;

DROP VIEW IF EXISTS v_customer_balance_summary;
CREATE VIEW v_customer_balance_summary AS
SELECT
    c.customer_id,
    c.full_name,
    c.email,
    c.status                                   AS customer_status,
    COUNT(a.account_id)                        AS account_count,
    COALESCE(SUM(a.balance), 0)                AS total_balance,
    COALESCE(MAX(a.balance), 0)                AS largest_account_balance,
    MIN(a.opened_at)                           AS first_account_opened
FROM customers c
LEFT JOIN accounts a ON a.customer_id = c.customer_id
GROUP BY c.customer_id, c.full_name, c.email, c.status;


DROP VIEW IF EXISTS v_monthly_transaction_summary;
CREATE VIEW v_monthly_transaction_summary AS
SELECT
    DATE_FORMAT(created_at, '%Y-%m')                                        AS month,
    COUNT(*)                                                               AS total_transactions,
    SUM(transaction_type = 'DEPOSIT')                                      AS deposit_count,
    SUM(CASE WHEN transaction_type = 'DEPOSIT'      THEN amount ELSE 0 END) AS deposit_amount,
    SUM(transaction_type = 'WITHDRAWAL')                                   AS withdrawal_count,
    SUM(CASE WHEN transaction_type = 'WITHDRAWAL'   THEN amount ELSE 0 END) AS withdrawal_amount,
    SUM(transaction_type = 'TRANSFER_OUT')                                 AS transfer_count,
    SUM(CASE WHEN transaction_type = 'TRANSFER_OUT' THEN amount ELSE 0 END) AS transfer_amount,
    SUM(status = 'FAILED')                                                 AS failed_count
FROM transactions
GROUP BY DATE_FORMAT(created_at, '%Y-%m');


DROP VIEW IF EXISTS v_transaction_stats;
CREATE VIEW v_transaction_stats AS
SELECT
    transaction_type,
    COUNT(*)                                     AS transaction_count,
    ROUND(AVG(amount), 2)                        AS avg_amount,
    MIN(amount)                                  AS min_amount,
    MAX(amount)                                  AS max_amount,
    ROUND(STDDEV_SAMP(amount), 2)                AS stddev_amount,
    SUM(amount)                                  AS total_amount
FROM transactions
WHERE status IN ('SUCCESS', 'FLAGGED')
GROUP BY transaction_type;


DROP VIEW IF EXISTS v_account_ranking;
CREATE VIEW v_account_ranking AS
WITH volume AS (
    SELECT
        a.account_id,
        a.account_number,
        a.account_type,
        c.full_name,
        a.balance,
        COUNT(t.transaction_id)                                      AS transaction_count,
        COALESCE(SUM(CASE WHEN t.status IN ('SUCCESS','FLAGGED')
                          THEN t.amount ELSE 0 END), 0)              AS total_volume
    FROM accounts a
    JOIN customers c    ON c.customer_id = a.customer_id
    LEFT JOIN transactions t ON t.account_id = a.account_id
    GROUP BY a.account_id, a.account_number, a.account_type, c.full_name, a.balance
)
SELECT
    account_id,
    account_number,
    full_name,
    account_type,
    balance,
    transaction_count,
    total_volume,
    RANK()       OVER (ORDER BY total_volume DESC)                        AS volume_rank,
    RANK()       OVER (PARTITION BY account_type ORDER BY total_volume DESC) AS rank_in_type,
    DENSE_RANK() OVER (ORDER BY balance DESC)                             AS balance_rank,
    ROUND(100 * total_volume / NULLIF(SUM(total_volume) OVER (), 0), 2)   AS pct_of_total_volume,
    ROUND(AVG(total_volume) OVER (PARTITION BY account_type), 2)          AS avg_volume_for_type
FROM volume;


DROP VIEW IF EXISTS v_suspicious_transactions;
CREATE VIEW v_suspicious_transactions AS
SELECT
    t.transaction_id,
    t.created_at,
    a.account_number,
    c.full_name,
    t.transaction_type,
    t.amount,
    t.status,
    t.risk_score,
    t.risk_status,
    t.risk_reason,
    t.is_fraud
FROM transactions t
JOIN accounts  a ON a.account_id  = t.account_id
JOIN customers c ON c.customer_id = a.customer_id
WHERE t.risk_status IN ('MEDIUM', 'HIGH')
   OR t.status = 'FLAGGED'
   OR t.is_fraud = 1;


DROP VIEW IF EXISTS v_daily_account_activity;
CREATE VIEW v_daily_account_activity AS
SELECT
    account_id,
    DATE(created_at)                                    AS activity_date,
    COUNT(*)                                            AS transaction_count,
    SUM(amount)                                         AS total_amount,
    ROUND(AVG(amount), 2)                               AS avg_amount,
    SUM(status = 'FAILED')                              AS failed_count,
    SUM(is_fraud)                                       AS fraud_count
FROM transactions
GROUP BY account_id, DATE(created_at);
