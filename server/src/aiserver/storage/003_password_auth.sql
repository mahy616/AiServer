ALTER TABLE users ADD COLUMN password_hash TEXT;
PRAGMA user_version=3;
