# WorldMonitor Tier-0 Absorption Pattern

Date: 2026-06-25
Candidate: `worldmonitor/20260625-source-inventory-codex`

## Problem

koala73/worldmonitor is a 100MB AGPLv3 TypeScript/Next.js full-stack global intelligence dashboard with 500+ news feeds, 29 exchanges, commodity/crypto radar, Docker/Tauri deployment, Clerk auth, Convex backend, and Dodo payments. Deploying the full platform is operationally expensive and analytically unnecessary. But its data layer (FRED macro, GDELT events, COMTRADE trade flows, source-tiering design) has real analytical value for DSA macro context and NPL valuation.

## Solution: Three-Tier Absorption

### Tier-0: Report-Only Source Inventory

- No deployment, no formal skill write, no live scraping.
- Codex produces a `REPORT.md` identifying which data sources have value.
- Hermes annotates business value per source.
- Claude Code verifies technical feasibility.

Source valuation matrix:

| Source | Value | Treatment |
|--------|-------|-----------|
| FRED macro data | HIGH | P0 — direct macro regime, rates, credit stress, NPL discount rate |
| GDELT event/news | MEDIUM-HIGH | P1 — bounded smoke only; geopolitical/commodity disruption context |
| COMTRADE trade flows | MEDIUM | Hold — defer until concrete business question |
| Source tiers / cross-source signals | MEDIUM | Methodology reference only |
| Finance radar / OHLCV | LOW | Exclude — existing local stack covers this |
| Frontend / Tauri / maps | LOW | Exclude |
| SaaS auth / payment / backend | NEGATIVE | Exclude |

### Tier-1: Lightweight MCP (after Tier-0 stable + user approval)

Claude built `worldmonitor-lite` MCP server with 4 tools:
- `get_fred_series(series_id, start, end)` — FRED macro data via keyless fredgraph.csv
- `macro_snapshot()` — current macro basket snapshot
- `macro_stress_snapshot()` — composite stress score for NPL/valuation
- `gdelt_event_count(query, timespan)` — bounded GDELT event smoke

Key implementation decisions:
- FastMCP stdio transport (no port, no Docker)
- Isolated venv, local cache in `~/.hermes/cache/worldmonitor-lite/`
- Daily cache with expiry; cache-first, fetch-on-expiry
- No Redis/Convex/Upstash/Docker/Tauri
- FRED: keyless CSV endpoint, no API key
- GDELT: DOC 2.0 query, ≥6s rate limit
- COMTRADE: deferred, not connected

### NPL Business Integration

| MCP Tool | NPL Use |
|----------|---------|
| `macro_stress_snapshot()` | npl-valuation discount rate input (rate/credit spread stress → recovery discount) |
| `get_fred_series(series_id)` | DD Step 2 macro context (debtor industry repayment capacity) |
| `gdelt_event_count(query)` | DD Step 5 external events (commodity/supply chain disruption → construction material cost → builder solvency) |

## Multi-Agent Coordination Pattern

This absorption demonstrates the standard collaborative workflow:

1. **Hermes** evaluates external project (clone, structure, README, size/deps)
2. **Claude Code** does deep technical inspection (source code, architecture, data endpoints)
3. **Codex** produces boundary report (source inventory, inclusion/exclusion, risk matrix)
4. **Hermes** annotates business value and NPL integration paths
5. **Claude Code** builds lightweight MCP implementation
6. **Codex** audits boundaries and verifies disk evidence
7. **Hermes** integrates MCP into existing config, verifies stdio, reports NPL landing path

## Key Design Principles

1. **Don't deploy the platform** — extract only the data layer.
2. **Tier-0 first** — prove value with a read-only report before any code.
3. **MCP as delivery mechanism** — stdio transport, no port, no Docker, no external dependencies.
4. **Cache-first, stateless** — daily cache, no persistence requirements.
5. **Keyless where possible** — FRED uses public CSV endpoint, no API key needed.
6. **Bounded GDELT** — narrow queries, rate-limited, no broad scraping.
7. **COMTRADE deferred** — only when a concrete business question demands it.
8. **No sublation promotion** — this is a runtime tool install, not a formal skill write.

## Pitfalls

- Don't clone AGPLv3 code into the skill tree — keep the MCP implementation clean-room.
- Don't let "500+ feeds" create pressure to add more data sources.
- GDELT shared-IP rate limiting means it may only work from the user's home IP.
- TimesFM integration revealed that the model works far better on structural macro series (CPI MAPE 1.04% vs naive 1.77%) than on stock prices (茅台 MAPE 4.50% vs naive 4.11%), confirming FRED macro data as the right first target.
