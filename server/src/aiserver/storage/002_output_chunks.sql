CREATE TABLE output_chunks(
 job_id TEXT NOT NULL REFERENCES job_payloads(job_id) ON DELETE CASCADE,
 sequence INTEGER NOT NULL,
 text TEXT NOT NULL,
 PRIMARY KEY(job_id,sequence)
);
PRAGMA user_version=2;
