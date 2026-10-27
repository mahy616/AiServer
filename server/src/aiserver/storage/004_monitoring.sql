-- Metadata only. Deliberately independent of jobs/payload retention.
CREATE TABLE task_performance(
 job_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,status TEXT NOT NULL,error_code TEXT,
 received_at_ms INTEGER,created_at_ms INTEGER NOT NULL,started_at_ms INTEGER,
 first_output_at_ms INTEGER,finished_at_ms INTEGER,
 input_tokens INTEGER,output_tokens INTEGER,
 engine_total_ms REAL,load_ms REAL,prompt_eval_ms REAL,eval_ms REAL);
CREATE INDEX ix_performance_created ON task_performance(created_at_ms DESC);
CREATE INDEX ix_performance_finished ON task_performance(finished_at_ms);
CREATE INDEX ix_performance_user_created ON task_performance(user_id,created_at_ms DESC);
INSERT INTO task_performance(job_id,user_id,status,error_code,created_at_ms,started_at_ms,finished_at_ms,input_tokens,output_tokens)
 SELECT j.job_id,j.user_id,j.status,j.error_code,j.created_at_ms,j.started_at_ms,j.finished_at_ms,
        json_extract(p.metrics_json,'$.inputTokens'),json_extract(p.metrics_json,'$.outputTokens')
 FROM jobs j LEFT JOIN job_payloads p USING(job_id)
 WHERE j.finished_at_ms IS NULL OR j.finished_at_ms > CAST(strftime('%s','now') AS INTEGER)*1000-604800000;
PRAGMA user_version=4;
