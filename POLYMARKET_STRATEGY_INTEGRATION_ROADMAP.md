# Polymarket Bot — Existing Repository Strategy Integration Roadmap

**Purpose:** Replace the current strategy behaviour in an already-working Polymarket repository with the rules in `STRATEGY.md`, while preserving the repository's existing infrastructure and unrelated functionality.

**Target mode:** Local paper trading only. No live order placement, wallet signing, private-key handling, or live-trading switch.

**Execution principle:** Audit first, plan second, implement in small verified phases. Do not rebuild the project from scratch.

---

## 1. Project objective and strategy to integrate

The existing repository is already functional. The task is to change its strategy behaviour, not to create a second bot or replace working components unnecessarily.

The strategy specification defines a 300-second Polymarket market cycle:

- **0–179 seconds:** observe the market, but do not initiate paper orders.
- **180–299 seconds:** strategy evaluation and paper entries are allowed.
- **At 300 seconds:** stop initiating entries for that market.
- **Initial entry:** when either `UP` or `DOWN` outcome probability is inclusively between **89% and 92%**, create a **$1 paper order** in that outcome.
- **Reversal:** after an initial order, monitor the opposite outcome. When its probability is inclusively between **85% and 90%**, create an opposite-outcome paper order for **10× the associated initial amount** — normally **$10**.
- Monitor incoming data and process qualifying events with the lowest practical delay. Record data-receipt, signal-detection, and simulated-execution timestamps.
- Keep an auditable local paper-trade ledger and a live local dashboard with market state, probabilities, activity, latency, and hourly/daily/lifetime performance.

The specification explicitly prohibits live trading. It also says not to invent missing strategy, probability-source, settlement, fee, slippage, or repeated-entry behaviour. Those unresolved points are release gates, not invitations for Antigravity to guess.

---

## 2. Non-negotiable repository-preservation rules

Antigravity must follow these rules throughout the work:

1. **Do not rebuild the application.** Reuse the current entry points, market-data adapters, WebSocket clients, order-book handling, persistence, backtesting, logging, and UI wherever they already perform the required job.
2. **Do not delete or rename existing files just to make the new design cleaner.** Any file removal or structural refactor requires evidence that it is necessary and a documented migration plan.
3. **Do not overwrite unrelated features.** Keep the current operational behaviour available as a baseline until the new strategy has passed regression tests.
4. **Do not edit before the repository audit is complete.** First report the architecture, current strategy path, dependencies, data flows, and proposed file changes.
5. **Protect local work.** Inspect `git status`, current branch, uncommitted changes, and recent commits. Never discard or reset user changes. Create a safe branch or backup before implementation if the repository workflow permits it.
6. **Change the smallest possible surface area.** Prefer adapting an existing strategy/configuration module over duplicating the bot or creating parallel data clients.
7. **Keep paper-only guarantees at the execution boundary.** The new strategy must not call any live order-submission endpoint or wallet-signing path.
8. **No hidden assumptions.** If a required behaviour is unresolved, document it and stop only the dependent feature. Do not silently choose a value.
9. **Regression-test after every phase.** A phase is not complete merely because the code runs; its exit criteria and existing tests must pass.
10. **Do not claim profitability from implementation tests.** Profitability can only be assessed from properly labelled, sufficiently representative paper/replay observations with explicit fill and settlement assumptions.

---

## 3. Phase-by-phase implementation roadmap

## Phase 0 — Safe preflight and baseline protection

**Goal:** Establish a recoverable starting point before touching the working repository.

**Tasks:**
- Confirm the repository root and inspect the complete directory tree, including hidden configuration files where safe.
- Run `git status --short`, identify the active branch, inspect recent commits, and identify uncommitted/untracked user work.
- Do not stash, reset, clean, checkout over, or delete user changes.
- Identify the normal local startup command, test command, and shutdown procedure from the project's own documentation/configuration.
- Record Python/runtime versions and installed dependencies.
- Run the existing test suite and, if practical, a short baseline paper/demo run without changing code.
- Save the baseline test output and note any pre-existing failures separately.

**Deliverables:**
- Baseline repository status report.
- Existing run/test commands.
- List of pre-existing failures or warnings.
- Safe implementation branch/backup reference, where available.

**Exit gate:** No source code has been changed. The existing working state is recoverable and the baseline is documented.

## Phase 1 — Full repository architecture audit

**Goal:** Trace how the current bot works before deciding what to modify.

**Inspect and map:**
- Application entry points and process lifecycle (`main.py`, `bot.py`, or their actual equivalents).
- Strategy decision path: where current entry thresholds, direction selection, timing, stake size, and reversal/exit behaviour are evaluated.
- Market discovery and market-session identification.
- Read-only market data sources, including Gamma/CLOB/RTDS/WebSocket clients or equivalent components already present.
- UP/DOWN token mapping and how the repository currently derives its probability/price values.
- Order-book snapshots, best bid/ask, depth, and the existing paper-fill simulation method.
- Existing live-order code paths, if any, and how paper mode currently prevents them from running.
- Persistence and data formats under the existing `data/` or other storage directories.
- Backtesting/replay implementation.
- Tests, fixtures, and mock data.
- Existing local dashboard or API, if one exists; do not assume a new dashboard is needed.
- Logging, configuration, deployment scripts, and process-management conventions.

**Create a component map** with columns: responsibility, current module/file, current behaviour, reusable as-is, required change, tests affected.

**Important screenshot observation to verify against the actual current files:** the provided editor screenshot shows `bot.py` constants resembling `ENTRY_START=180`, `ENTRY_END=285`, `PRICE_MIN=0.93`, `PRICE_MAX=0.96`, `STAKE_USD=5.0`, and `POLL_SEC=1.0`, plus Gamma/CLOB endpoints and a note about paper fills walking the real order book. Treat these only as clues from the screenshot. Read the actual files and trace every reference before changing them. The target specification differs on several of those values.

**Deliverables:**
- Architecture and data-flow map.
- Current strategy call graph.
- List of modules that can be reused unchanged.
- Minimal proposed change list, with exact filenames and reasons.
- Description of current paper/live execution boundary.

**Exit gate:** Antigravity can explain exactly where a market-data event enters, where the current strategy makes a decision, how a paper fill is simulated, and where the resulting trade is stored. No code changes yet.

## Phase 2 — Strategy reconciliation and decision register

**Goal:** Translate the specification into precise implementation behaviour without inventing missing rules.

**Implement as confirmed requirements:**
- Market duration: 300 seconds.
- No entries before elapsed time 180 seconds.
- Entry window: `180 <= elapsed_seconds < 300`.
- Initial trigger range: `89 <= outcome_probability <= 92` percent, inclusive.
- Initial paper amount: `$1`.
- Reversal target: opposite outcome to the qualifying initial trade.
- Reversal trigger range: `85 <= opposite_probability <= 90` percent, inclusive.
- Reversal amount: `10 × linked_initial_amount` (normally `$10`).
- No new entries at or after 300 seconds.
- Paper trading only.

**Record and raise these unresolved decisions before they affect behaviour:**
1. Can a market produce multiple initial $1 orders, or is there at most one initial order per market?
2. Can one initial order produce multiple reversal orders, or at most one reversal per initial order/market?
3. Does “touches the range” mean trigger whenever an observation is within the range, or only on a transition from outside to inside? What debounce/re-arm behaviour is intended?
4. Is the $10 reversal a new additional opposite position, or does it close/replace the original $1 position?
5. Are positions held until market resolution, or is there an exit rule? No early exit rule is currently specified.
6. What probability value should drive the trigger: best ask, best bid, midpoint, last-traded price, or another explicit probability field? Do not substitute bid–ask spread or an unrelated BTC price gap.
7. What exact market family/asset and market-selection rule should be used? The strategy document refers to 5-minute UP/DOWN markets but does not fully specify market discovery scope or simultaneous-market limits.
8. What fill model should be used for paper orders, including order-book depth, partial fills, spread, and slippage?
9. What fee schedule and settlement/payout calculation should the simulator use?
10. Which timezone defines hourly and daily dashboard reporting? Make the selected timezone visible.

**Deliverables:**
- Strategy requirements traceability table.
- Open-decision register with owner/status and which phase each decision blocks.
- A clear state-transition description for initial and reversal orders.

**Exit gate:** Every confirmed rule is unambiguous. Unresolved items are visibly marked and are not silently hard-coded. Independent work may continue only where it does not depend on an unanswered decision.

## Phase 3 — Market-data and market-clock adaptation

**Goal:** Reuse the existing data infrastructure and make its output suitable for the new strategy.

**Tasks:**
- Prefer the existing market-data adapter and connections; do not create a second client if the current one can be extended safely.
- Verify the source of the UP and DOWN outcome probabilities and document the exact field and mapping to tokens/outcomes.
- Preserve raw source values, source timestamps, local receipt timestamps, and market identifiers.
- Verify whether the feed is streaming or polled, its actual update cadence, reconnection behaviour, rate limits, and timestamp semantics.
- Track market start/end using market metadata, not process start time.
- Use monotonic elapsed-time measurement so wall-clock adjustments cannot extend the 300-second market window.
- Handle a bot started midway through a market correctly: wait if elapsed time is below 180 seconds, evaluate only the remaining eligible window if between 180 and 300 seconds, and do nothing if the market has ended.
- Reject or quarantine missing, malformed, stale, out-of-order, or mismapped observations.
- Surface data connection and freshness state to logs and the dashboard API.

**Tests:**
- Market opens at 180 seconds exactly.
- Market closes for new entries at 300 seconds exactly.
- No entry at 179.999 seconds or 300.000 seconds.
- Restart midway through a market.
- Reconnect after feed interruption.
- Stale, duplicate, out-of-order, malformed, and missing observations.
- Correct UP/DOWN mapping for representative markets.

**Exit gate:** Strategy inputs are trustworthy, timestamped, and correctly mapped. No strategy thresholds or orders have been implemented yet unless needed for isolated tests.

## Phase 4 — New strategy engine integration

**Goal:** Replace the old decision logic with the new rules while retaining the current application lifecycle and data path.

**Tasks:**
- Put the new thresholds and amounts in one canonical configuration location, with defaults matching the specification.
- Implement the market state gate: `WAITING` before 180 seconds, `ACTIVE` during 180–299 seconds, and `CLOSED` at/after 300 seconds.
- Implement the initial trigger for either UP or DOWN at 89%–92%, inclusive, with a $1 paper-order intent.
- After an initial intent is accepted by the paper engine, monitor the opposite outcome for the 85%–90%, inclusive, reversal trigger.
- Link every reversal intent to the initial order that caused its monitoring state; use 10× that initial order amount.
- Keep strategy evaluation separate from order simulation, persistence, and dashboard rendering.
- Prevent duplicated signals caused by repeated delivery of the same data event. The exact re-arm policy for later distinct signals must follow the decision register, not an invented debounce rule.
- Remove or disable the old strategy logic only after references and tests prove the new path is the single active strategy path. Do not leave two strategy engines able to submit duplicate paper orders.
- Do not add unrequested technical indicators, confirmation filters, stop-losses, take-profits, or other entry rules.

**Tests:**
- Both initial directions.
- Probability values immediately below, at the lower bound, inside, at the upper bound, and immediately above each range.
- Reversal in the correct opposite direction only.
- Reversal amount is exactly 10× the associated initial amount.
- No reversal before an initial trade exists.
- No entry outside the active market window.
- Duplicate event delivery does not duplicate a paper order.
- Multiple-entry tests match the decisions explicitly approved in Phase 2.

**Exit gate:** Unit and integration tests demonstrate that the new rules are the only strategy rules driving paper-order intents and all boundary tests pass.

## Phase 5 — Paper execution, order-book simulation, and ledger

**Goal:** Integrate the new strategy with the repository's existing simulator and produce auditable paper trades.

**Tasks:**
- Reuse the existing paper execution engine and its order-book-walking logic if Phase 1 confirms it is correct and suitable. Do not replace it just to match a new architecture diagram.
- Ensure the paper executor is the only destination for strategy intents in this version.
- Explicitly block live order submission, wallet signing, and private-key loading/use in the running paper configuration. Do not add a dormant live switch.
- Simulate fills using the approved fill model. Record partial/unfilled outcomes rather than pretending every order fills instantly.
- Persist order ID, market ID, order type, outcome, stake, trigger probability, counterpart probability, source timestamp, receipt/detection/execution timestamps, simulated price, shares/quantity, fill status, and linked initial order ID for reversals.
- Make order creation idempotent so reconnect/replay does not create duplicate trades.
- Preserve existing data compatibility. If the schema changes, use a safe, reversible migration and retain historical records.
- Store enough raw event detail to reconstruct how each simulated order was triggered.

**Latency instrumentation:**
- Record data receipt → signal detection latency.
- Record signal detection → simulated execution latency.
- Record end-to-end local processing latency.
- Use a monotonic high-resolution clock for durations and wall-clock timestamps for audit display.
- Do not claim exact-millisecond market reaction if the external feed updates less frequently.

**Exit gate:** Triggered intents create paper records only; fills and assumptions are inspectable; historical data is preserved; tests prove no live order path can be reached.

## Phase 6 — Settlement and P&L accounting

**Goal:** Make performance figures mathematically correct and traceable.

**Tasks:**
- Implement the position model only after the Phase 2 decisions define whether reversal orders are additional or replacing positions and how positions close.
- Use the approved share-price/fill calculation and binary-market settlement rules. Do not assume the stake itself equals the profit or loss.
- Apply approved fees, spread, and slippage assumptions consistently.
- Keep unresolved positions separate from resolved results.
- Distinguish at least: gross profit, gross loss, net P&L, realized P&L, unrealized/mark-to-market value (if implemented), and unresolved exposure.
- Calculate hourly, daily, and lifetime totals from the persisted ledger, not counters that exist only in memory.
- Make timezone and reporting-window boundaries explicit.
- Ensure totals reconcile to individual trade/settlement records and survive application restarts.

**Tests:**
- Manually calculated sample positions.
- Winning and losing outcomes.
- Fee/slippage application.
- Partial fills, if supported by the chosen model.
- Unresolved markets and later resolution.
- Reversal pair accounting.
- Hour/day boundary and timezone tests.
- Restart/reload reconciliation.

**Exit gate:** Every displayed P&L number can be reproduced from persisted trade and resolution records. If key accounting decisions remain unresolved, show the affected figures as pending or estimated—not as realized profit.

## Phase 7 — Dashboard integration or adaptation

**Goal:** Provide a working local interface without duplicating an existing dashboard unnecessarily.

**Tasks:**
- First determine whether the repo already contains a dashboard or local API. Extend it if suitable; create a new HTML/CSS/JS interface only if no existing interface meets the specification.
- Use a professional, fully light-themed trading-terminal layout.
- Show bot state (running/paused/stopped), current market, elapsed/remaining time, WAITING/ACTIVE/CLOSED state, UP/DOWN probabilities, data freshness, and current trigger eligibility.
- Show initial and reversal paper-order activity, recent events, trade history, execution timestamps, and measured latency.
- Show number of markets, total orders, initial orders, reversal orders, reversal events, wins/losses, and hourly/daily/lifetime gross profit, gross loss, and net P&L.
- Clearly label unresolved, estimated, replayed, simulated, and realized values.
- Connect the page to backend state through the repository's established local API/event mechanism. Prefer WebSocket or server-sent events if already supported; otherwise use a documented refresh cadence.
- Keep UI rendering off the latency-critical strategy path. Dashboard updates must not block signal evaluation or paper execution.
- Controls may start/stop/pause the paper bot only. They must not expose live trading.
- Make empty, disconnected, and stale-data states truthful. Never display fabricated trades or decorative P&L.

**Tests:**
- Dashboard values reconcile with backend API and ledger.
- Live updates without full-page reload.
- Browser refresh/reconnect.
- Backend disconnect and recovery.
- Start/stop/pause controls.
- Rapid event updates without freezing the interface.

**Exit gate:** The dashboard is a live view of the actual paper bot, not a mock-up, and all visible metrics match backend records.

## Phase 8 — Regression, replay, and performance validation

**Goal:** Prove the integrated strategy works and the existing repository still works.

**Tasks:**
- Run all existing tests and compare against the Phase 0 baseline.
- Add focused tests for all new strategy rules and boundary cases.
- Run deterministic market-data replays to verify repeatability.
- Test process restart, missed market starts, API/WebSocket outage, rate limiting, stale data, malformed messages, clock changes, duplicate events, and local dashboard reconnection.
- Test that the old strategy path cannot run in parallel with the new one.
- Test paper-only safeguards at the execution boundary and search the code paths for any unintended live submission call.
- Measure latency distributions (for example p50/p95/p99), feed freshness, dropped events, and processing backlog. Optimise only after correctness is demonstrated.
- Compare paper fills under the selected order-book simulation model with the underlying recorded snapshots.

**Exit gate:** Existing functionality remains intact except for the specifically intended strategy change; all required new tests pass; no live orders are possible; latency and data limitations are measured and reported.

## Phase 9 — Local demo deployment and observation

**Goal:** Run the integrated system locally and collect credible evidence about the strategy's behaviour.

**Tasks:**
- Update installation/run documentation using the repo's existing conventions.
- Preserve existing commands and environment variables where possible; document any changed defaults.
- Run a short smoke test, then a longer local paper-trading observation period.
- Persist data across restarts and provide a backup/export option for the paper ledger.
- Report sample size, markets observed, trades, initial/reversal counts, rejected/unfilled paper intents, fees/slippage model, realized/unresolved P&L, latency, outages, and data gaps.
- Keep replay/backtest results clearly separate from live-feed paper-trading results.
- Do not make a profitability claim without a representative sample and transparent assumptions.

**Exit gate:** User can run the app locally, inspect all paper activity, restart without losing history, and evaluate performance from a reproducible ledger.

---

## 4. Known configuration differences to investigate

The editor screenshot supplied with the request appears to show the following values in `bot.py`. Antigravity must verify the actual current source and every use of each value before changing it.

| Screenshot value | New strategy requirement | Investigation required |
|---|---|---|
| `ENTRY_START = 180` | Keep 180 seconds | Verify this is measured from the actual market start, not bot startup. |
| `ENTRY_END = 285` | New entries allowed until just before 300 seconds | Determine why the old strategy stops at 285; do not retain the 285 cutoff unless the user explicitly wants a 15-second buffer. |
| `PRICE_MIN = 0.93` | Initial probability lower bound 0.89 | Confirm the old variable represents the same probability concept and is not a different price/side metric. |
| `PRICE_MAX = 0.96` | Initial probability upper bound 0.92 | Verify the exact input field before replacing the range. |
| `STAKE_USD = 5.0` | Initial amount $1; reversal amount $10 | Separate initial and reversal stake configuration rather than reusing one stake for both. |
| `POLL_SEC = 1.0` | Low-latency updates preferred | Determine whether a supported streaming feed already exists; do not increase API load blindly. |
| `FEE_RATE = 0.0` | Fee model unresolved | Do not report realistic net P&L until fee assumptions are confirmed. |

Also verify the screenshot's note that the current paper simulator walks the real order book. This may be reusable and valuable, but its fill assumptions, data freshness, and treatment of partial fills must be inspected.

---

## 5. Required Antigravity working protocol

Antigravity should execute the roadmap using this control loop:

1. **Inspect:** Read the relevant files and trace callers/callees before editing.
2. **Report:** State the intended change, exact files, expected impact, and tests.
3. **Implement:** Make the smallest focused change for the current phase only.
4. **Verify:** Run targeted tests, then relevant regression tests.
5. **Summarise:** List changed files, behaviour changed, tests run/results, and remaining risks.
6. **Checkpoint:** Create a reviewable commit/checkpoint where the user's Git workflow permits it.
7. **Proceed:** Move to the next phase only after the current phase's exit gate is met.

Do not implement all phases in one large unreviewable change. Do not modify unrelated modules to make the code conform to a preferred architecture. If actual code differs from the screenshot or this roadmap's assumptions, trust the inspected repository and update the file plan before editing.

---

## 6. Final acceptance checklist

- [ ] Existing repository architecture and baseline behaviour were documented before edits.
- [ ] Existing features and user changes were preserved.
- [ ] Only the necessary modules were modified.
- [ ] Actual market start time drives the 300-second market clock.
- [ ] No entries before 180 seconds or at/after 300 seconds.
- [ ] Initial trigger is 89%–92%, inclusive, and initial amount is $1.
- [ ] Reversal monitors the opposite outcome and triggers at 85%–90%, inclusive.
- [ ] Reversal amount is 10× the associated initial amount ($10 for a $1 initial order).
- [ ] All unresolved strategy and accounting decisions are answered or visibly left pending without guessed behaviour.
- [ ] Read-only market data is correctly mapped to UP/DOWN outcomes and carries timestamps/freshness state.
- [ ] Paper fills use documented and tested assumptions.
- [ ] Persistent ledger records enough information to audit and reproduce each trade.
- [ ] Hourly, daily, and lifetime metrics reconcile to the ledger.
- [ ] Dashboard is connected to the actual local bot and accurately displays live state.
- [ ] Latency is measured, not merely claimed.
- [ ] Existing tests pass, new tests pass, and regression checks show no unintended breakage.
- [ ] No live order submission, wallet signing, private-key handling, or hidden live mode exists.
- [ ] Local installation, run, stop, and recovery instructions are documented.
- [ ] Paper results are labelled honestly and not presented as proof of future profitability.

**Definition of done:** The existing Polymarket application continues to run on its established architecture, but its active paper-trading strategy follows the new specification, all trade records and dashboard metrics are auditable, and the application has no live-order capability in this version.
