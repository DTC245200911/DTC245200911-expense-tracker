#!/bin/sh
set -e

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 \
    --set app_user="$APP_DB_USER" --set app_password="$APP_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'app_user') \gexec

REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE categories (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) UNIQUE NOT NULL,
    type VARCHAR(10) NOT NULL CHECK (type IN ('income', 'expense'))
);
CREATE TABLE transactions (
    id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(id),
    category_id INT NOT NULL REFERENCES categories(id),
    amount NUMERIC(14, 2) NOT NULL CHECK (amount > 0),
    note TEXT,
    tx_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO categories (name, type) VALUES
    ('Lương', 'income'),
    ('Ăn uống', 'expense'),
    ('Đi lại', 'expense'),
    ('Nhà cửa', 'expense'),
    ('Giải trí', 'expense'),
    ('Học tập', 'expense'),
    ('Khác', 'expense');

CREATE INDEX transactions_user_date_idx ON transactions (user_id, tx_date DESC, id DESC);

SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_user') \gexec
SELECT format('GRANT SELECT ON categories TO %I', :'app_user') \gexec
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON transactions TO %I', :'app_user') \gexec
SELECT format('GRANT USAGE, SELECT ON SEQUENCE transactions_id_seq TO %I', :'app_user') \gexec
SELECT format('GRANT SELECT, INSERT ON users TO %I', :'app_user') \gexec
SELECT format('GRANT USAGE, SELECT ON SEQUENCE users_id_seq TO %I', :'app_user') \gexec
SQL
