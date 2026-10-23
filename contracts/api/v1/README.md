# Business API v1, A-1
JSON Schema files here are the wire contract. Root /api/v1; health public, all others per-user Bearer token.
POST jobs (202 committed, 200 replay); GET jobs/{id} (status), jobs/{id}/result (409 pending, 410 expired), jobs/by-request/{clientRequestId}, jobs (own list); POST jobs/{id}/cancel (200 queued/terminal, 202 running).
Unknown/duplicate request keys rejected. 5MiB UTF-8 JSON, no compression/multipart. Receive deadline 120s. Text total 128KiB; field byte limits in schema descriptions.
PNG: at most one, raw 3MiB, long edge 1280, pixels 786432, no animation/resizing. Decode before budget check.
hashVersion=business-v1: sorted-key compact UTF-8 JSON excluding clientRequestId; optional omissions normalize to null/empty arrays, UUIDs canonicalize; text/array order preserved; dataBase64 replaced with decoded PNG SHA256. Image metadata remains in identity.
Existing key lookup precedes readiness/budget/limits. Effective profile/prepared materials are immutable snapshots.
Fake is Development only and ready says Fake. Fake byte budgeting is a test fixture, never real token counting. Unverified real input returns 503 INPUT_BUDGET_UNVERIFIED.
succeeded means complete text, never business/compilation validation.
Internal contract: server/src/aiserver/inference/contracts.py. B can wrap its experimental PreparedPrompt/Generation without modifying those interfaces.
