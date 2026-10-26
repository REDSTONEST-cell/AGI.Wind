# Companion Agent Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement task-by-task, with test-first verification.

**Goal:** bounded station/web tools and a persistent management page.
**Architecture:** current dialogue adapter plus small tool runner; reuse archive/SSE/auth/settings and active sticker delivery. No external agent framework dependency.
**Tech Stack:** Python3.12/FastAPI/Pydantic/httpx/PostgreSQL, existing plain JS client.
**Spec:** ../specs/2026-10-25-agent-capabilities-design.md

## Global constraints

Work only in isolated worktree, preserve primary checkout. Off by default; 3 rounds, 6 calls, 15s tool, 90s total. No arbitrary local execution. Intermediate reasoning/tools only in logs. Feature-only integration after DeepSeek completion.

## Review focus

- Fragmented tool IDs/arguments, malformed schema, excessive output: bounded parser tests.
- Disabled/unconfigured capabilities: settings and runner tests reject execution.
- Tool-loop cancellation/time budget and repeated calls: runner tests verify no late side effects.
- Scope/source changes and sticker ownership: PostgreSQL integration tests.
- Saved secrets and page reload: authenticated settings tests and browser checks.

## Task 1: protocol and policy

- [x] Write failing tests for native tool-call IDs/arguments, reasoning continuity and unchanged text stream.
- [x] Add agent/settings.py, protocol ToolCall/ToolTurn and provider tool_turn.
- [x] Verify unit/provider tests.

Ruling: use bounded non-streaming native tool turns rather than a second incremental tool-call parser. This preserves the legacy streaming adapter and avoids leaking intermediate planning into chat. Cost: Agent-mode final text waits for the final native response; ordinary chat remains streamed.

## Task 2: bounded runner and real tools

- [x] Write failing tests for tools-disabled final answer, schema validation, timeout, call budgets and duplicate send.
- [x] Add agent/runner.py, tools.py, search.py and delivery adapter.
- [x] Integrate GenerationService via a small optional branch; verify PostgreSQL generation/SSE/cancel behavior.

## Task 3: management API and UI

- [x] Write failing auth/CSRF/persistence/secret tests.
- [x] Add agent settings routes and settings serialization; page module and navigation.
- [x] Verify setting restoration and browser save interactions without real chat.

## Task 4: verification and integration preparation

- [x] Run relevant tests then full backend regression and client checks (final 176 passed, 1 skipped; browser and splitter passed).
- [x] Independent review, red/green fixes, documentation and separate feature commits.
- [ ] AOCI alignment: attempted maintenance stopped on inherited Observe/orphan/scope blockers; deferred at user's explicit quota-saving direction. Do not claim aligned.
- [ ] Inspect primary changes; integrate only when DeepSeek completion is confirmed. Otherwise preserve branch and report exact pending boundary.
