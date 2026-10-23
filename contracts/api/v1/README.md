# Business API v1, A-1
JSON Schema files here are the wire contract. Root /api/v1; health public, all others per-user Bearer token.
POST jobs (202 committed, 200 replay); GET jobs/{id} (status), jobs/{id}/result (409 pending, 410 expired), jobs/by-request/{clientRequestId}, jobs (own list); POST jobs/{id}/cancel (200 queued/terminal, 202 running).
Unknown/duplicate request keys rejected. 5MiB UTF-8 JSON, no compression/multipart. Receive deadline 120s. Text total 128KiB; field byte limits in schema descriptions.
PNG: at most one, raw 3MiB, long edge 1280, pixels 786432, no animation/resizing. Decode before budget check.
hashVersion=business-v1: sorted-key compact UTF-8 JSON excluding clientRequestId; optional omissions normalize to null/empty arrays, UUIDs canonicalize; text/array order preserved; dataBase64 replaced with decoded PNG SHA256. Image metadata remains in identity.
Existing key lookup precedes readiness/budget/limits. Effective profile/prepared materials are immutable snapshots.
Fake is Development only and ready says Fake. Fake byte budgeting is a test fixture, never real token counting. Unverified real input returns 503 INPUT_BUDGET_UNVERIFIED.
succeeded means complete text, never business/compilation validation.
Internal contract: server/src/aiserver/inference/contracts.py. Real implementation: inference/runtime.py; historical A/B ownership no longer applies.

Production v0.2 uses conservative-estimate-v1 capacity admission (not exact tokenization). ready.capacityMethod identifies the policy; acceptedInputTypes means supported bounded inputs. Results expose generation.estimatedInputTokens separately from actual inputTokens/outputTokens and generation.capacityMethod. Pending/failed-before-response actual counts remain null. Estimates never claim renderer/tokenizer equivalence. UnverifiedBudget remains a development rejection fixture, not the production admission path.

The engine always receives truncate=false and shift=false. Admission overflow is 422 CONTEXT_TOO_LARGE; engine overflow becomes a failed task with that code. OUTPUT_TRUNCATED is failed with available incomplete text, never succeeded. Per-user request/submission rates produce 429 RATE_LIMITED with Retry-After. Cancellation holds the generation slot until the managed engine tree is reset and model identity rechecked; failed recovery blocks admission but leaves historical queries available.
