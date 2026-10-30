# Polymarket 5-Minute UP/DOWN Strategy

**Document type:** Strategy and implementation specification  
**Trading mode:** Local paper-trading simulation only  
**Status:** Strategy rules captured; unresolved decisions are explicitly listed and must not be guessed by the implementation.

---

## 1. Project objective

Build a fast, locally running paper-trading system for Polymarket 5-minute UP/DOWN markets. The system must monitor outcome probabilities, apply the time-window and entry rules in this document, simulate trades without placing real orders, record every event, and present the bot's activity and performance in a live local web dashboard.

The immediate objective is to evaluate whether the strategy is profitable over an extended demo period. **Real-money trading is out of scope.** Do not implement, expose, or enable live order placement in this version.

## 2. Non-negotiable constraints

1. **Paper trading only.** All entries and exits in this version must be simulated. No real orders, wallet signing, private-key handling, deposits, withdrawals, or live-order execution code.
2. **Local operation.** The bot and dashboard must be runnable on the user's own PC.
3. **Five-minute market cycle.** The strategy is designed for a 300-second market.
4. **180-second waiting period.** Do not initiate any trade during the first 180 seconds of a market.
5. **120-second operation window.** Strategy entries may be initiated only during the final 120 seconds of that market.
6. **Initial paper stake.** Every initial order uses a $1 paper amount.
7. **Reversal paper stake.** A qualifying reversal order uses 10 times the initial order amount: $10 when the initial amount is $1.
8. **Speed matters.** Detect qualifying probability changes and record/execute the paper order with the lowest practical processing delay. Capture timestamps at millisecond precision where the data source and runtime permit.
9. **Do not silently change the strategy.** Do not add indicators, confirmation filters, stop-losses, take-profits, martingale steps, or other trading conditions unless the user later specifies them.
10. **No invented market data.** Clearly distinguish live read-only market observations from replayed, delayed, simulated, or unavailable data.

## 3. Market and timing model

Each market is treated as a five-minute (300-second) round.

| Market elapsed time | Market phase | Bot behaviour |
|---|---|---|
| 0–179 seconds | Waiting phase | Observe and record market data; do not place paper trades. |
| 180–299 seconds | Trading phase | Evaluate initial-entry and reversal conditions; paper orders are allowed when their conditions are met. |
| At/after 300 seconds | Market ended | Do not initiate any further trades for that market. Finalize the market's paper-trade result when the outcome is available. |

### Timing requirements

- The 180-second wait is measured from the **actual market start time**, not from when the bot process happens to start.
- Determine market start and end times from trustworthy market metadata or another explicitly configured market-time source.
- Do not assume that starting the application means a new market has just started.
- If the bot starts partway through a market, calculate the current elapsed time. If the market is still in its first 180 seconds, continue waiting. If it is already in the final 120 seconds, it may evaluate the strategy for the remaining time, subject to the entry rules. If the market has ended, do not trade it.
- At 300 seconds, stop initiating orders even if a probability condition is still true.
- Use a monotonic clock for measuring elapsed processing time and a synchronized wall clock for event timestamps. Handle clock corrections without extending the market window.

### Timing illustration

For a market that starts at `12:00:00`:

- `12:00:00` to `12:02:59.999…`: waiting phase; no orders.
- `12:03:00` to just before `12:05:00`: trading phase; evaluate the strategy.
- `12:05:00` onward: no new entries for that market.

The exact boundary must be handled consistently: the trading window opens at elapsed second 180 and closes at elapsed second 300.

## 4. Probability inputs

The strategy monitors the probability of both outcomes:

- `UP` probability
- `DOWN` probability

The probability values are interpreted as outcome probabilities expressed in percentage points from 0% to 100% (for example, `89%`). They are **not** the order-book bid–ask spread.

The implementation must identify and document the exact read-only data field or market-data feed used for these probabilities. Preserve the source value and its source timestamp for each observation. Do not silently substitute an unrelated price, spread, indicator, or estimated value.

If the feed is disconnected, stale, malformed, or missing either outcome value, do not trigger a paper order from invalid data. Show the data-quality state on the dashboard and log the condition.

## 5. Initial-entry rule

### Rule

During the 120-second trading window, continuously monitor UP and DOWN probabilities.

When either outcome's probability enters the inclusive range **89% to 92%**, trigger an initial paper order for that outcome.

- Initial paper order amount: **$1**.
- Direction: the outcome whose probability qualified (`UP` or `DOWN`).
- Trigger range: `89% <= outcome_probability <= 92%`.
- No initial order is permitted before elapsed market time reaches 180 seconds.
- No initial order is permitted at or after elapsed market time reaches 300 seconds.

### Initial-entry pseudocode

```text
IF market_elapsed_time >= 180 seconds
AND market_elapsed_time < 300 seconds
AND valid_probability_data_is_available:
    FOR outcome IN [UP, DOWN]:
        IF 89 <= outcome_probability(outcome) <= 92:
            trigger an initial paper order:
                outcome = qualifying outcome
                amount = $1
                type = INITIAL
```

This pseudocode expresses the stated trigger. It does not resolve whether multiple initial orders may be placed in the same market; see [Section 12](#12-unresolved-decisions--do-not-guess).

## 6. Reversal rule

### Rule

After an initial paper order has been placed, monitor the market actively for a reversal during the remaining trading window.

A reversal trade is intended to take the **opposite outcome** to the initial trade. When the opposite outcome's probability enters the inclusive range **85% to 90%**, trigger a reversal paper order.

- Reversal order amount: **10 times the initial order amount**.
- With a $1 initial order, the reversal paper order amount is **$10**.
- Reversal direction: opposite the initial order's outcome.
- Reversal trigger range: `85% <= opposite_outcome_probability <= 90%`.
- The reversal order must still fall within the market's 120-second trading window.
- Continue monitoring promptly after the initial entry; do not pause monitoring while waiting for the initial trade to settle.

### Reversal examples

| Initial paper order | Reversal outcome monitored | Reversal trigger | Reversal paper amount |
|---|---|---:|---:|
| Buy/choose UP | DOWN | DOWN probability is 85%–90% inclusive | $10 |
| Buy/choose DOWN | UP | UP probability is 85%–90% inclusive | $10 |

### Reversal pseudocode

```text
IF an initial paper order exists for the market
AND the market is still inside the 120-second trading window
AND valid_probability_data_is_available:
    opposite_outcome = opposite_of(initial_order.outcome)

    IF 85 <= probability(opposite_outcome) <= 90:
        trigger a reversal paper order:
            outcome = opposite_outcome
            amount = 10 * initial_order.amount
            type = REVERSAL
```

### Meaning of “reversal” in this specification

The strategy notes describe a reversal as the opposite outcome becoming sufficiently likely. For implementation purposes, the specified measurable reversal trigger is therefore the opposite outcome entering the 85%–90% probability range. No additional trend, candle, momentum, order-book, or technical-indicator confirmation has been supplied. Do not invent one.

## 7. Order simulation requirements

All orders must be paper orders. The system should record a simulated order event as soon as its trigger is detected.

Each paper-order record should include, at minimum:

- Unique order ID.
- Market ID and market start/end timestamps.
- Order type: `INITIAL` or `REVERSAL`.
- Outcome: `UP` or `DOWN`.
- Paper amount in USD.
- Trigger probability and the other outcome's probability, where available.
- Probability source and source timestamp.
- Trigger-detection timestamp.
- Paper execution timestamp, recorded to milliseconds where available.
- Simulated execution price or probability used by the simulator.
- Execution assumptions used (for example, whether spread, slippage, and fees are modelled).
- Order status: triggered, simulated/filled, rejected by simulation safeguards, or otherwise clearly defined.
- Associated initial order ID for a reversal order.

### Execution speed and measurement

- Process each incoming market-data update promptly; avoid unnecessary polling delays when a supported streaming feed is available.
- Keep the signal-evaluation path lightweight. Do not block it on dashboard rendering, slow disk operations, or unrelated calculations.
- Record separate timestamps for data receipt, signal detection, and simulated execution so latency can be measured.
- Display measured processing latency. Do not claim zero latency or guaranteed exact-millisecond execution.
- If the data source itself updates less frequently than milliseconds, state that limitation accurately. Millisecond timestamps do not make the underlying data millisecond-fresh.

## 8. Paper-trading accounting and performance

The demo system must maintain an auditable paper-trading ledger. Every simulated order, market result, and P&L calculation must be traceable to stored records.

The dashboard must show the following metrics:

### Activity metrics

- Total number of markets observed.
- Total number of trades/orders taken.
- Number of initial trades.
- Number of reversal trades.
- Number of markets with at least one trade.
- Number of reversal events detected.
- Number of winning and losing resolved trades, once a resolution method is defined and market outcomes are available.

### Performance metrics

Show each metric for:

- The current hour.
- The current day.
- Lifetime since the local paper-trading ledger began.

Required financial metrics:

- Gross profit.
- Gross loss.
- Net profit/loss (P&L).
- Number of profitable and unprofitable trades, where determinable.
- Win rate, where determinable.

Use clear labels and consistent USD formatting. Do not label unrealized, unresolved, or estimated results as realized profit. Make the selected time basis and timezone visible; the dashboard is intended for local use, and the user's timezone should be configurable.

### Accounting integrity

- Store raw trade events and market-resolution records; calculate dashboard totals from these records rather than relying only on in-memory counters.
- Persist data across application restarts.
- Avoid double-counting duplicate market-data events or duplicate trigger notifications.
- Make it possible to inspect the individual paper trades behind an aggregate metric.
- Clearly separate realized P&L, unrealized/mark-to-market P&L, and unresolved exposure if those are implemented.

The exact position accounting, settlement calculation, fee model, and slippage model have not yet been specified. See Section 12. Do not invent financial results or assume a $1/$10 stake automatically equals a $1/$10 profit or loss.

## 9. Local web dashboard requirements

Build a local dashboard using **HTML, CSS, and JavaScript**. It must use a clean, fully light-themed, professional financial/trading-terminal aesthetic inspired by a modern Wall Street trading workstation.

The dashboard is a functional interface to the running paper bot, not a static mock-up.

### Required dashboard information

At a glance, the user must be able to understand:

- Whether the bot is running, paused, disconnected, or stopped.
- Which 5-minute market is currently being observed.
- Market elapsed time and remaining time.
- Whether the bot is in the 180-second waiting phase or 120-second trading phase.
- Current UP and DOWN probabilities.
- Whether the initial-entry range is currently met.
- Whether the reversal range is currently met.
- Whether an initial paper order or reversal paper order has been triggered.
- Recent trade/order activity and execution timestamps.
- Total trades, initial trades, reversal trades, and reversal events.
- Hourly, daily, and lifetime profit, loss, and net P&L.
- Data-feed connection and freshness status.
- Measured event-processing latency.

### Interactivity

- Provide working controls for starting and stopping the **paper-trading** process, where technically supported.
- Make the current strategy state visible without requiring a page refresh.
- Update displayed market values and bot state as new data arrives. Use event-driven updates where possible; otherwise use a sensible refresh interval and state its actual cadence.
- Provide a live activity/event log.
- Provide a trade-history view with enough detail to inspect individual initial and reversal paper orders.
- Provide a way to inspect hourly, daily, and lifetime statistics.
- Clearly indicate when data is stale, unavailable, replayed, or simulated.
- Ensure the interface remains usable during rapid updates; avoid unnecessary full-page rerenders.

### Dashboard presentation

- Light theme throughout.
- Professional, restrained typography and spacing.
- Clear visual hierarchy for market state, probabilities, trades, and P&L.
- Use green/red financial indicators carefully and consistently, with text labels so meaning is not conveyed by colour alone.
- Responsive layout suitable for the user's local PC display.
- Do not use decorative values or fake trades to make the dashboard appear active. Empty states must be honest.

## 10. Suggested system components

The implementation may choose its framework and internal architecture, but it must keep the following responsibilities clearly separated:

1. **Market-data adapter** — obtains read-only UP/DOWN probability observations and market timing metadata. It must not place orders.
2. **Market clock/session manager** — tracks each market's start, elapsed time, trading-window boundaries, and end.
3. **Strategy engine** — evaluates only the rules in this document and emits trade signals.
4. **Paper execution engine** — simulates orders and records execution assumptions and timestamps. It must not call a live order endpoint.
5. **Trade ledger and persistence** — stores market observations needed for audit, paper orders, simulated fills, resolutions, and performance records.
6. **Statistics/P&L service** — calculates hourly, daily, and lifetime metrics from persisted records.
7. **Local dashboard/API layer** — safely exposes bot state, market observations, trades, logs, and statistics to the browser.
8. **Monitoring and logging** — records connection state, stale data, errors, event timing, and application lifecycle events.

These are architectural responsibilities, not permission to add extra trading signals or risk rules.

## 11. Development and verification phases

Antigravity should implement the system in controlled phases. Do not skip verification between phases.

### Phase 1 — Inspect and plan

- Inspect the existing project and runtime environment before making changes.
- Identify the intended read-only market-data source and how market start/end times and UP/DOWN probabilities are obtained.
- Document any API limitations, update frequency, rate limits, and timestamp semantics.
- Present the proposed architecture and file plan.
- Identify every unresolved strategy/accounting decision from Section 12.

**Exit criteria:** The data source and system design are documented; unresolved decisions are visible rather than guessed.

### Phase 2 — Market data and timing

- Implement read-only market-data ingestion.
- Implement market-session tracking for 300-second markets.
- Enforce the first-180-second no-trade period and final-120-second trading window.
- Detect stale, missing, invalid, and disconnected data.
- Log receipt timestamps and data freshness.

**Exit criteria:** Automated tests prove that no trade signal can be emitted before elapsed second 180 or at/after second 300.

### Phase 3 — Strategy engine

- Implement the initial 89%–92% inclusive probability trigger and $1 paper amount.
- Implement post-initial-entry monitoring of the opposite outcome.
- Implement the 85%–90% inclusive reversal trigger and 10× paper amount ($10 when the initial amount is $1).
- Keep conditions and thresholds in one clearly defined configuration location while preserving the specified defaults.
- Do not introduce unrequested indicators or additional entry filters.

**Exit criteria:** Unit tests cover threshold boundaries, both directions, time-window boundaries, missing data, and reversal association with the initial order.

### Phase 4 — Paper execution and ledger

- Implement simulated order creation and fill recording.
- Store all relevant probability, timestamp, market, and order metadata.
- Persist paper-trading records locally.
- Prevent duplicate processing of the same triggering event.
- Make the simulation assumptions explicit and visible.

**Exit criteria:** Tests prove that triggers generate paper records only and that no live-order capability exists in the application.

### Phase 5 — Performance and P&L

- Implement the paper ledger's accounting calculations once the unresolved settlement and cost-model decisions are defined.
- Calculate hourly, daily, and lifetime statistics.
- Test calculations against manually verified sample ledgers.
- Distinguish unresolved, estimated, and realized figures.

**Exit criteria:** All figures reconcile to the underlying trade and resolution records.

### Phase 6 — Local dashboard

- Build the light-themed HTML/CSS/JavaScript dashboard.
- Connect it to the running local bot through a local interface.
- Implement live state, market timer, probabilities, activity feed, trade history, latency, and performance views.
- Implement working paper-bot controls and clear feed/status indicators.
- Test rapid updates and browser reconnection behaviour.

**Exit criteria:** Dashboard values match the backend and update without manual refresh; controls affect the paper bot as expected.

### Phase 7 — Testing and paper simulation

- Test market-start and market-end boundaries.
- Test probability threshold boundaries and direction changes.
- Test missed market starts, process restarts, disconnections, stale data, duplicate events, and dashboard reconnects.
- Run replay/backtest scenarios where suitable historical data is available; label replay results separately from live paper observations.
- Run the local demo in paper mode and retain a persistent audit trail.
- Report observed performance honestly, including sample size, data limitations, latency, and assumptions.

**Exit criteria:** The bot remains paper-only, its outputs are reproducible from the ledger, and strategy behaviour passes the documented tests.

## 12. Unresolved decisions — do not guess

The following details were not specified in the supplied strategy. Antigravity must not silently choose values or behaviour. Surface these questions and, where implementation can proceed independently, isolate the decision behind a clearly labelled configuration setting.

1. **“12-seconds” wording:** One note says “during this 12-seconds,” while the main rule says the final 120 seconds (2 minutes). This document interprets the intended window as **120 seconds**, based on the explicit 180-second wait and the repeated 2-minute description. Confirm if that interpretation is incorrect.
2. **Multiple initial entries:** Is the bot allowed to place more than one $1 initial order in the same 5-minute market if a qualifying signal occurs again, or is there a maximum of one initial order per market?
3. **Multiple reversal entries:** Is only one $10 reversal order allowed per initial order/market, or can repeated opposite-probability signals trigger additional $10 reversal orders?
4. **Reversal trigger semantics:** Should the opposite outcome trigger as soon as its probability is inside 85%–90%, or only when it crosses into that range from below? The supplied rule says “touches” the range; no crossing/debounce rule was specified.
5. **Initial and reversal order relationship:** Should a reversal order be an additional $10 paper position, or should it replace/close the original $1 position? The current wording treats it as an additional $10 opposite-outcome order; confirm this interpretation.
6. **Position closure and settlement:** Should positions be held until the market resolves, or should there be any early exit rule? No early exit rule was supplied.
7. **Payout and P&L calculation:** Confirm whether the simulator should model actual Polymarket share acquisition and binary-market settlement, or use a simplified stake-based model. The exact fill price/share calculation must be defined before claiming realistic P&L.
8. **Fees and slippage:** Should the demo include Polymarket fees, spread, slippage, and execution depth? If yes, provide the intended assumptions or allow them to be sourced and documented. Do not assume they are zero while presenting the result as a realistic net P&L.
9. **Probability source:** Specify the exact market-data field/feed to use for UP and DOWN probabilities, including whether the strategy should use best ask, midpoint, last trade, or another value. These values may differ.
10. **Market selection:** Specify how the bot should identify which 5-minute BTC or other UP/DOWN market to trade, and whether more than one market may be monitored simultaneously. The supplied rules do not explicitly name the underlying asset or market-selection method in this strategy description.
11. **Timezone for reporting:** Confirm the timezone used to define the dashboard's hourly and daily reporting windows. The dashboard should make the selected timezone visible.
12. **No-signal behaviour:** No trade should be created when neither outcome meets its threshold. No fallback entry has been specified.

If a question blocks a realistic calculation or a safe implementation, stop at that boundary, explain what is missing, and ask for the decision. Do not fabricate a rule to make the implementation appear complete.

## 13. Safety, scope, and prohibited functionality

For this version, the following are explicitly prohibited:

- Live Polymarket order placement.
- Live order signing or wallet interaction.
- Private-key, seed-phrase, or wallet-secret collection/storage.
- A hidden or experimental live-trading switch.
- Automatic upgrade from paper mode to live mode.
- Any code path that submits an order to Polymarket or another exchange.

Read-only public market data may be used if needed and documented. Paper trades must be generated and stored entirely by the local simulation layer.

A future live-trading upgrade would require a separate specification, explicit user approval, and a separate implementation review. Do not prepare or activate that functionality as part of this task.

## 14. Final acceptance checklist

The project is considered complete for this version only when all applicable items below pass:

- [ ] The application runs locally on the user's PC.
- [ ] The market clock correctly tracks a 300-second market.
- [ ] No strategy order is triggered in the first 180 seconds.
- [ ] Strategy order triggers are disabled at/after 300 seconds.
- [ ] The initial trigger uses the inclusive 89%–92% range.
- [ ] Every initial paper order uses $1.
- [ ] The reversal monitor checks the opposite outcome after an initial order.
- [ ] The reversal trigger uses the inclusive 85%–90% range.
- [ ] Every reversal paper order uses 10× the associated initial amount ($10 for a $1 initial order).
- [ ] Trigger, data-receipt, and simulated-execution timestamps are recorded.
- [ ] Duplicate data events cannot accidentally duplicate paper orders.
- [ ] Paper trades and performance history persist across restarts.
- [ ] Hourly, daily, and lifetime activity and P&L views are available.
- [ ] The dashboard is light-themed, professional, interactive, and connected to the live local bot state.
- [ ] Dashboard data freshness and bot connection state are visible.
- [ ] P&L uses documented settlement, fill, fee, and slippage assumptions.
- [ ] No live-order functionality, wallet signing, or private-key handling is present.
- [ ] Tests cover all specified threshold and timing boundaries.
- [ ] All unresolved decisions that affect behaviour or P&L have been answered or are clearly shown as pending.

---

## 15. Core strategy summary

```text
FOR each 5-minute market (300 seconds):

    Observe market data during the first 180 seconds.
    Do not place any paper orders during this period.

    During the remaining 120 seconds, while the market is active:

        Monitor UP and DOWN probabilities continuously.

        INITIAL ENTRY:
            If UP or DOWN probability is between 89% and 92%, inclusive:
                Trigger a $1 paper order for that outcome.

        AFTER INITIAL ENTRY:
            Monitor the opposite outcome continuously.

            If the opposite outcome probability is between 85% and 90%, inclusive:
                Trigger a reversal paper order for 10x the initial amount.
                With a $1 initial order, the reversal amount is $10.

    At 300 seconds:
        Stop initiating new paper orders for that market.
        Finalize results only according to the configured and documented
        paper-settlement model.

Throughout:
    Persist all events and paper orders.
    Update the local dashboard with bot state, probabilities, trades,
    reversal activity, latency, hourly P&L, daily P&L, and lifetime P&L.
    Never place real-money orders.
```

**Implementation priority:** Correct strategy behaviour and trustworthy paper-trading records first; dashboard presentation second; latency optimisation after correctness and auditability are verified. The system must never sacrifice correctness or paper-only safety to appear faster.
