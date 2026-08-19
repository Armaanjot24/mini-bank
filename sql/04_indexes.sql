USE mini_bank;

CREATE INDEX idx_txn_account_created  ON transactions (account_id, created_at);
CREATE INDEX idx_txn_type_created     ON transactions (transaction_type, created_at);
CREATE INDEX idx_txn_status_created   ON transactions (status, created_at);
CREATE INDEX idx_txn_fraud            ON transactions (is_fraud, created_at);

CREATE INDEX idx_transfers_from_created ON transfers (from_account_id, created_at);
CREATE INDEX idx_transfers_to_created   ON transfers (to_account_id, created_at);

CREATE INDEX idx_accounts_customer_status ON accounts (customer_id, status);

CREATE INDEX idx_audit_action_created ON audit_logs (action, created_at);
