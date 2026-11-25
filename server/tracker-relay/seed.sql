-- Antigravity Token Tracker ingest seed
-- Safe to re-run: every statement is idempotent.

-- 1) Virtual account: holds the ingest sink identity. Never scheduled as upstream.
INSERT INTO accounts (name, platform, type, credentials, extra, status, schedulable, notes, rate_multiplier)
SELECT 'Antigravity IDE (Tracker)', 'antigravity', 'oauth',
       jsonb_build_object('virtual', true, 'source', 'antigravity-token-tracker'),
       jsonb_build_object('tracker', true), 'active', false,
       'Virtual sink for Antigravity Token Tracker ingest; never scheduled', 1.0
WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE name = 'Antigravity IDE (Tracker)');

-- 2) Dedicated API key for the tracker relay (user 2 = owner account).
INSERT INTO api_keys (user_id, key, name, status)
SELECT 2, '__API_KEY__', 'antigravity-tracker-ingest', 'active'
WHERE NOT EXISTS (SELECT 1 FROM api_keys WHERE name = 'antigravity-tracker-ingest');

SELECT 'account' AS kind, id, name, platform, schedulable::text AS flag
FROM accounts WHERE name = 'Antigravity IDE (Tracker)'
UNION ALL
SELECT 'api_key', id, name, status, left(key, 18)
FROM api_keys WHERE name = 'antigravity-tracker-ingest';
