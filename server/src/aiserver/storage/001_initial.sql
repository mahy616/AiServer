CREATE TABLE users(user_id TEXT PRIMARY KEY,display_name TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),created_at_ms INTEGER NOT NULL);
CREATE TABLE api_keys(key_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(user_id),token_sha256 TEXT NOT NULL,created_at_ms INTEGER NOT NULL,expires_at_ms INTEGER,revoked_at_ms INTEGER);
CREATE TABLE jobs(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT UNIQUE NOT NULL,user_id TEXT NOT NULL REFERENCES users(user_id),
 client_request_id TEXT NOT NULL,request_hash TEXT NOT NULL,hash_version TEXT NOT NULL,operation TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','running','cancelling','succeeded','failed','cancelled')),
 stop_reason TEXT,inference_stop_confirmed INTEGER CHECK(inference_stop_confirmed IN (0,1)),
 created_at_ms INTEGER NOT NULL,started_at_ms INTEGER,finished_at_ms INTEGER,profile_id TEXT NOT NULL,error_code TEXT,
 payload_expires_at_ms INTEGER,payload_purged_at_ms INTEGER,tombstone_expires_at_ms INTEGER,
 UNIQUE(user_id,client_request_id));
CREATE TABLE job_payloads(job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,request_json TEXT NOT NULL,effective_settings_json TEXT NOT NULL,output_json TEXT,metrics_json TEXT);
CREATE TABLE job_events(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,at_ms INTEGER NOT NULL,event_type TEXT NOT NULL,reason TEXT);
CREATE TABLE service_state(id INTEGER PRIMARY KEY CHECK(id=1),recovery_required INTEGER NOT NULL CHECK(recovery_required IN (0,1)),recovery_reason TEXT,recovery_job_id TEXT,updated_at_ms INTEGER NOT NULL,recovered_at_ms INTEGER,recovery_note TEXT);
INSERT INTO service_state(id,recovery_required,updated_at_ms) VALUES(1,0,0);
CREATE INDEX ix_status_sequence ON jobs(status,sequence);
CREATE INDEX ix_user_sequence ON jobs(user_id,sequence);
CREATE INDEX ix_status_created ON jobs(status,created_at_ms);
PRAGMA user_version=1;
