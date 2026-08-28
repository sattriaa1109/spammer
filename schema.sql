-- DDL for Facebook Auto-Like Bot

CREATE TABLE IF NOT EXISTS accounts (
    id SERIAL PRIMARY KEY,
    username VARCHAR(255) NOT NULL,
    session_cookies JSONB NOT NULL,
    proxy VARCHAR(255),
    user_agent TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS targets (
    id SERIAL PRIMARY KEY,
    url_post TEXT NOT NULL,
    status VARCHAR(50) DEFAULT 'pending', -- pending, in_progress, success, failed
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS logs (
    id SERIAL PRIMARY KEY,
    account_id INT REFERENCES accounts(id) ON DELETE SET NULL,
    target_id INT REFERENCES targets(id) ON DELETE SET NULL,
    action_time TIMESTAMPTZ DEFAULT NOW(),
    message TEXT NOT NULL
);

-- Index for efficient queue fetching
CREATE INDEX IF NOT EXISTS idx_targets_status ON targets(status);
CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts(is_active);
