# Memory Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Preserve existing uncommitted work. Execute continuously within the user's authorized scope.

**Goal:** Complete the six memory gaps approved by the user, preserving original events, portable authority and model independence.

**Architecture:** PostgreSQL events, facts and provenance remain authoritative. Local CPU semantic search is rebuildable, isolated from speech GPU work. Background extraction, consolidation and indexing have bounded work and persisted budgets; ambiguous contradictions require review.

**Tech Stack:** Python 3.12, PostgreSQL/SQLAlchemy/Alembic, FastEmbed Chinese BGE small on CPU, existing FastAPI/JS client and outbox lease worker.

**Spec:** User-approved six-area audit and existing docs/development/2026-10-25-long-term-memory-phase1.md. Current source is authoritative where older documentation differs.

## Global Constraints

- Work in C:/Users/wind/Desktop/AGI.Wind on codex/v7-visual-stickers; preserve unrelated changes; no blanket commit/reset/merge.
- Visible, same-account, same-companion source references and source hashes are mandatory. Fiction and assistant guesses must not silently become user facts.
- Hidden/deleted/expired/superseded facts cannot reappear through vector retrieval, summaries, backup restoration or caches.
- Added authoritative columns must export/restore and migrate old archives with defaults. Derived vectors and operational budgets are not archive authority.
- Keep provider/NAS/DNS credentials and private conversation text out of reports and test output. CPU memory models only; do not occupy the speech GPU.
- Tests use isolated real PostgreSQL schemas. Do not insert synthetic dialogue into the user's live archive.

## Task 1: Structured facts, temporal validity, pinning and retention

- [x] Add migration 0012 and memory_items columns subject_key/predicate/value (non-null empty defaults), valid_from/valid_to (nullable timezone timestamps), is_pinned (false), summary_level (empty default).
- [x] Store extracted semantic keys. Merge same subject/predicate/value with valid provenance; preserve hidden/superseded suppression.
- [x] Explicit user corrections supersede older same-slot facts with temporal closure and a relation. Ambiguous conflicting values remain disputed and linked; assistant/inferred/fictional statements never replace user facts automatically.
- [x] Pinned memories do not expire or decay but explicit correction/hide/delete still works; configurable per-kind retention applies at write and maintenance boundaries.
- [x] Verify source quote checks, same-slot updates, conflicts, pin/unpin and old-archive defaults.

## Task 2: Local semantic retrieval and incremental indexing

- [x] Add rebuildable vector tables for memory_items and retrieval_chunks, keyed by embedding-space fingerprint and content hash, with FK cascade deletion.
- [x] Deploy Chinese FastEmbed BGE small via isolated loopback authenticated CPU service. Record exact package/model identity. No automatic model download on a normal chat request.
- [x] Bounded incremental indexing including edited text; skip empty events. Build vectors outside DB transactions, recheck source/hash/visibility before writing.
- [x] Fuse lexical and semantic candidates; re-rank using similarity plus explicit importance/freshness signals. Clearly describe this as deterministic ranking, not a deployed neural cross-encoder.
- [x] Enforce status, expiry, user, account, companion and provenance checks when fetching vector candidates and again before context injection.
- [x] Verify Chinese paraphrase retrieval, lexical fallback, stale hashes, cross-account isolation and deletion.

## Task 3: Consolidation, summaries and budgets

- [x] Produce daily/weekly derived summaries through the strict source-quoted extraction writer, with source IDs and bounded input/output; never overwrite persona or user facts.
- [x] Coalesce duplicate pending automatic extraction jobs. Reserve daily estimated spend/tokens atomically before vendor calls, settle usage even if a later context check fails; expose estimates and configured rates honestly.
- [x] Persist operational usage with no dialogue content. Exhausted budget defers work without blocking chat or advancing its extraction cursor.
- [x] Verify concurrent reservation limits, retries, dedupe, summary provenance and deletion/restore behavior.

## Task 4: Independent worker, settings and management UI

- [x] Filter worker claims by event type; allow memory work in a separate process while generation remains in Gateway.
- [x] Share configuration/extractor setup, reload persisted memory settings, add managed start/stop and health/heartbeat information.
- [x] Extend settings API/UI for semantic switch, retention, pinning, summaries and budgets; show conflicts and source-backed history. Preserve existing page IDs and interactions.
- [x] Verify no duplicate job claims, no calls under locks, pending-job cancellation/lease behavior and live settings refresh.

## Task 5: Acceptance and deployment

- [x] Run focused tests, protocol drift checks if DTO contracts change, isolated export/restore/delete coverage, Chinese quality fixtures and a 10k-item synthetic timing check.
- [x] Deploy the local CPU model, migrations and memory worker; run one small read-only semantic query on current data without printing conversation content.
- [x] Update progress/acceptance documentation with actual evidence and remaining physical/UI/quality boundaries. Complete one final AOCI maintenance attempt; do not resolve unrelated governance debt by guessing.

## Review Focus

False supersession; hidden-fact vector resurrection; summary provenance deletion; budget races and stale retry billing; archive backwards compatibility; CPU search work blocking realtime calls.

Final validation: memory96passed/1skipped; full313passed/3skipped/1failed (pre-existing call session contract incomplete). Runtime backlog zero; live bounded summary stored2 from32sources. See docs/development/2026-10-26-memory-completion.md. AOCI maintenance is a final attempt, not a claim of governance alignment.
