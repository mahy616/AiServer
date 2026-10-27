-- Credentials and task ownership stay intact; password login is retired.
ALTER TABLE users DROP COLUMN password_hash;
PRAGMA user_version=5;
