# Minimal companion Agent design

User selected station tools plus web search on 2026-10-25. Existing DeepSeek development stays in the primary checkout; this feature is isolated on `codex/agent-capabilities`.

## Behavior

Optional native Chat Completions tool loop reuses the current dialogue provider/model. Tools: search_memory, search_stickers, send_sticker, web_search. Scope comes from the accepted generation. No shell, filesystem, autonomous scheduling, memory mutation or arbitrary MCP execution.

Agent is off by default. Enabled runs support at most 3 tool rounds / 6 calls, 15 seconds per tool, 90 seconds total by default, configured within hard bounds. At the budget boundary perform one tools-disabled final answer. Intermediate tool planning text stays out of chat; real reasoning and tool outcomes use existing log events. Final text uses existing split/image delivery. Preserve cancellation, epoch/revision validation and failure cleanup.

Settings page: total switch, independent tool toggles, rounds/calls/timeouts/result size, search backend and credentials. Search supports administrator-configured SearXNG JSON or fixed Brave API endpoint. Search is unavailable until configured; do not silently choose a public instance. Search returns source URLs/snippets (not downloaded full pages). Secrets stay in SecretStore. Data returned by tools is untrusted evidence, never privileged instructions.

Enabling Agent explicitly opts into native `tool_calls`. Existing text-only/fake providers keep their original path while Agent is off; unsupported Agent providers fail clearly. Agent rounds use bounded non-streaming native responses, preserving complete IDs/arguments and reasoning_content within the same temporary tool loop when required. Never reinsert archived reasoning into future turns. Do not silently enable beta strict mode or change API URL. Final Agent text appears once the final native round completes; ordinary mode retains streaming.

Each send_sticker uses an owned active sticker selected earlier in the same turn, at most once, then existing transactional attachment delivery. Avoid duplicate automatic sticker selection on Agent turns. Memory search remains read-only and scope filtered; revalidate before tool work and output commit.

Memory search content is supplied only to temporary model history. Persisted tool outcomes include count/source IDs, not an untracked second copy of source text. Serialize Agent partial read/modify/persist/replace and shared settings-file writes with one process lock. Separate SearXNG/Brave secret references.

## Isolation/integration

Baseline d22b02f is a private snapshot of current uncommitted work, not authored feature work. Feature commits must be separable from that snapshot. Do not merge the snapshot commit wholesale into DeepSeek's checkout. After confirmed DeepSeek completion, compare its final files against snapshot and integrate feature-only changes; test combined result before runtime restart. A dirty/stable file tree alone is not proof that DeepSeek has finished.

## Evidence

Native calls: https://api-docs.deepseek.com/guides/tool_calls/
Thinking continuity: https://api-docs.deepseek.com/guides/thinking_mode/
Bounded ReAct reference: https://github.com/agentscope-ai/agentscope
Search: https://docs.searxng.org/dev/search_api.html and https://api-dashboard.search.brave.com/app/documentation/web-search/get-started

Use local fake providers/HTTP transports and isolated PostgreSQL schemas for tests. No production messages, paid keys or main service restart during parallel development.
