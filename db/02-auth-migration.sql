CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS user_id INT REFERENCES users(id);

-- Bổ sung danh mục chi khác cho cả database đã được khởi tạo trước đây.
INSERT INTO categories (name, type)
VALUES ('Khác', 'expense')
ON CONFLICT (name) DO NOTHING;

CREATE INDEX IF NOT EXISTS transactions_user_date_idx
    ON transactions (user_id, tx_date DESC, id DESC);

REVOKE ALL ON users FROM PUBLIC;
SELECT format('GRANT SELECT, INSERT ON users TO %I', :'app_user') \gexec
SELECT format('GRANT USAGE, SELECT ON SEQUENCE users_id_seq TO %I', :'app_user') \gexec
