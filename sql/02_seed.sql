USE mini_bank;

INSERT INTO customers (customer_id, full_name, email, phone, date_of_birth) VALUES
    (1, 'Asha Rao',    'asha@example.com',   '9000000001', '1995-04-12'),
    (2, 'Bilal Khan',  'bilal@example.com',  '9000000002', '1990-01-30'),
    (3, 'Chitra Nair', 'chitra@example.com', '9000000003', '1998-11-05');

INSERT INTO users (customer_id, username, password_hash, role) VALUES
    (1, 'asha',   '$2b$12$CrnpyryJkaIphIZcROnua.JYOKTPhmGGm60VV5ohBWo8YvJw8jtLu', 'CUSTOMER'),
    (2, 'bilal',  '$2b$12$CrnpyryJkaIphIZcROnua.JYOKTPhmGGm60VV5ohBWo8YvJw8jtLu', 'CUSTOMER'),
    (3, 'chitra', '$2b$12$CrnpyryJkaIphIZcROnua.JYOKTPhmGGm60VV5ohBWo8YvJw8jtLu', 'CUSTOMER'),
    (NULL, 'admin', '$2b$12$hxJe9RBAKIqoUbZWN/dWcueg7IastgaFYt57buxFRbqdTJ7xc/2Ka', 'ADMIN');

INSERT INTO accounts (account_id, customer_id, account_number, account_type, balance) VALUES
    (1, 1, '100000000001', 'SAVINGS', 25000.00),
    (2, 2, '100000000002', 'CURRENT', 48000.00),
    (3, 3, '100000000003', 'SAVINGS', 12000.00),
    (4, 1, '100000000004', 'CURRENT',  5000.00);

INSERT INTO transactions
    (account_id, transaction_type, amount, balance_before, balance_after, reference) VALUES
    (1, 'DEPOSIT', 25000.00, 0.00, 25000.00, 'SEED-OPEN-1'),
    (2, 'DEPOSIT', 48000.00, 0.00, 48000.00, 'SEED-OPEN-2'),
    (3, 'DEPOSIT', 12000.00, 0.00, 12000.00, 'SEED-OPEN-3'),
    (4, 'DEPOSIT',  5000.00, 0.00,  5000.00, 'SEED-OPEN-4');

INSERT INTO beneficiaries (customer_id, account_id, nickname) VALUES
    (1, 2, 'Bilal'),
    (1, 3, 'Chitra'),
    (2, 1, 'Asha');
