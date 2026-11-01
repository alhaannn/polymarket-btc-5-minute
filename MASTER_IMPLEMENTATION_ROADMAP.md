# MASTER IMPLEMENTATION ROADMAP
## Polymarket BTC Bot — Dynamic Capital Management, Compounding, Protected Capital & Next-Day Reserve Activation

**Document purpose:** Detailed, phase-by-phase implementation specification for upgrading an existing working Polymarket BTC 5-minute trading bot.

**Implementation mode:** Controlled modification of an existing repository. This is not a greenfield bot build.

**Initial implementation constraint:** Phase 1 is audit-only. No repository files may be modified until the user has reviewed and explicitly approved the audit and integration plan.

---

# 0. Executive Summary

The existing bot already performs BTC 5-minute prediction-market trading, including initial entries, reversal trades, settlement handling, and an existing daily shutdown condition.

The requested upgrade introduces a capital-management layer that determines:

- How much capital is active for trading.
- How initial and reversal order budgets scale with active capital.
- When capital protection is activated.
- How subsequent realised profits are split.
- How losses affect active capital.
- When protected capital may be partially released on the next trading day.
- How all balances and state transitions survive restarts and reconcile with actual exchange funds.

**The existing trading strategy must remain unchanged.** The upgrade must not redesign signal generation, market selection, probability thresholds, reversal detection, or the existing daily shutdown trigger.

## 0.1 Non-negotiable preservation rules

1. Do not build a new bot or duplicate the existing trading engine.
2. Do not rewrite the BTC strategy.
3. Do not change the existing 5-minute market selection or signal rules.
4. Do not change the existing initial-entry conditions.
5. Do not change reversal detection or its trigger conditions.
6. Do not change order execution semantics unless required to safely apply approved order budgets.
7. Preserve existing logging, monitoring, persistence, and operational controls.
8. Inspect the actual repository before proposing file changes; do not assume filenames or architecture.
9. Make changes incrementally and explain every modified file.
10. Never claim a test passed unless it was executed and its result is available.
11. Do not enable live trading or deploy without explicit user approval.
12. After each phase, stop and request approval before proceeding to the next phase.

## 0.2 Existing trading assumptions supplied by the user

These are strategy descriptions supplied by the user and must be verified against the repository, not assumed to match the implementation exactly.

| Item | User-described behaviour |
|---|---|
| Market | BTC 5-minute Up/Down prediction markets |
| Initial trade | Currently approximately $1 |
| Initial entry | Described as approximately 89% probability |
| Initial expected profit | Approximately $0.10 on a successful $1 trade |
| Reversal trade | Currently approximately $10 |
| Reversal expected profit | Approximately $1.10 on a successful $10 trade |
| Execution cadence | 5-minute market strategy; some opportunities are skipped |
| Performance normalisation | User sometimes uses a 10-minute average for rough calculations |
| Daily shutdown | Already implemented: at least one failed initial trade AND at least one failed reversal trade on the same trading day |

The user's 10-minute average is a performance-estimation convention. It does not authorize changing the bot from 5-minute markets to 10-minute markets.

---

# 1. Canonical Capital Rules

This section is the source of truth for implementation. If any later section or existing code conflicts with it, stop and report the conflict rather than silently choosing a rule.

## 1.1 Capital terminology

Use separate, clearly defined values:

- **Total equity:** Reconciled value of the bot's attributable capital, including active capital, protected capital, and any separately tracked parked/unallocated capital, without double-counting open positions or reserved collateral.
- **Active capital:** Capital budget eligible to fund strategy orders.
- **Protected capital:** Capital reserved from ordinary trading. It cannot be used by the normal initial or reversal execution path.
- **Open exposure / reserved collateral:** Funds already committed to open orders or positions. These are not free cash and must not be counted as spendable active capital.
- **Realised P&L:** Profit or loss confirmed by settlement and reconciled fees.
- **Daily shutdown eligibility:** A persisted flag/event indicating that the existing daily shutdown condition was triggered and that the next-day reserve activation may be considered.
- **Parked capital:** Any capital that must be tracked separately because it is not included in the current active/protected allocation. Do not create this category unless the repository's accounting requires it; explain it if used.

Never equate a position's displayed value, order notional, collateral, wallet balance, and realised P&L without checking the exchange's actual semantics.

## 1.2 Starting capital and initial sizing

Starting capital is **$11**.

Before the $44 milestone, 100% of eligible trading equity is active and protected capital is zero.

The position budget ratio is 1:10:

- Initial trade budget = active capital divided by 11.
- Reversal trade budget = active capital multiplied by 10 and divided by 11.

Round each calculated budget independently to the nearest whole dollar. Use a documented, deterministic rounding rule; exact half-dollar ties round upward.

Examples:

| Active capital | Initial budget | Reversal budget |
|---:|---:|---:|
| $11 | $1 | $10 |
| $22 | $2 | $20 |
| $33 | $3 | $30 |
| $40 | $4 | $36 |
| $43 | $4 | $39 |
| $44 | $4 | $40 |
| $55 | $5 | $50 |
| $66 | $6 | $60 |
| $88 | $8 | $80 |
| $110 | $10 | $100 |
| $220 | $20 | $200 |

**Important:** These are strategy budget calculations, not permission to spend both budgets simultaneously. The existing execution flow may treat initial and reversal positions as sequential or mutually exclusive. Inspect it. The order must be checked against free active funds after accounting for existing positions, open orders, fees, and exchange minimums.

The $1/$10 starting sizes are the exact ratio at $11. Whole-dollar independent rounding can cause small deviations from 1:10 at other balances.

## 1.3 First protection milestone at $44

When reconciled total equity first reaches or exceeds **$44**, activate the initial capital-protection split.

At exactly $44:

| Category | Amount |
|---|---:|
| Total equity | $44 |
| Active capital | $22 |
| Protected capital | $22 |

The milestone must activate once only. Persist the milestone event and its source equity snapshot.

The trigger is based on reconciled equity, not an estimated profit, unrealised P&L, or a stale in-memory balance.

If equity crosses the threshold while orders or positions are unsettled, define the exact safe activation point after audit. Do not split funds that are still committed or double-counted.

## 1.4 Profit allocation after the milestone

After the $44 milestone has activated:

- Allocate 50% of each subsequent **net realised profit** to active capital.
- Allocate 50% of each subsequent **net realised profit** to protected capital.
- Deduct realised trading losses from active capital.
- Do not automatically use protected capital to cover ordinary trading losses.

Example:

| Event | Active | Protected | Total |
|---|---:|---:|---:|
| Milestone activation | $22 | $22 | $44 |
| +$10 net realised profit | $27 | $27 | $54 |
| +$10 net realised profit | $32 | $32 | $64 |
| -$5 realised loss | $27 | $32 | $59 |

Use net realised P&L after the fees and settlement adjustments that the current system can reliably establish. Do not split gross profit and then ignore fees.

Before the $44 milestone, eligible net gains/losses affect the all-active pool according to the existing account balance semantics.

## 1.5 Existing daily shutdown condition — preserve exactly

The existing bot already has this daily shutdown rule:

> If at least one initial trade fails AND at least one reversal trade fails within the same configured trading day, stop opening new trades for the remainder of that trading day.

Preserve the existing definition of “failed” as implemented in the repository, after verifying how it handles settlement, cancellation, partial fills, and unresolved positions.

The condition is conjunctive:

- One failed initial trade alone: does not trigger this specific shutdown.
- One failed reversal trade alone: does not trigger this specific shutdown.
- At least one of each, on the same trading day: trigger the existing shutdown.

Do not add any additional daily drawdown percentage, fixed-dollar loss threshold, maximum-loss trigger, or alternative daily shutdown condition. The user explicitly states that this existing condition is the daily loss rule.

When the condition is satisfied:

1. Block new entries for the remainder of that trading day.
2. Handle eligible unfilled orders according to existing safe cancellation behaviour.
3. Manage already-open positions safely; do not abandon or falsely mark them settled.
4. Persist the shutdown state and reason.
5. Record that the day qualifies for next-day reserve activation.
6. Do not reset the shutdown early because of a process restart.

A failure must be counted only when the existing settlement/failure classification confirms it. An unresolved trade must not be treated as failed merely because it is pending.

## 1.6 Next-day protected-capital activation — Option B

This is the confirmed user choice.

If the previous trading day triggered the existing daily shutdown condition, then at the start of the next configured trading day, the bot may release **50% of the current protected capital**.

The released amount is **added to existing active capital**. It does not replace the active balance.

Formulas:

```text
released_amount = protected_capital * 0.50
new_active_capital = existing_active_capital + released_amount
new_protected_capital = protected_capital - released_amount
```

Example:

Before activation:

- Active capital = $30
- Protected capital = $110

Release:

- 50% of $110 = $55

After activation:

| Category | Amount |
|---|---:|
| Existing active capital | $30 |
| Released protected amount | $55 |
| New active capital | $85 |
| Remaining protected capital | $55 |
| Total equity | $140 |

New budgets:

- Initial = round($85 / 11) = $8.
- Reversal = round(($85 × 10) / 11) = $77.

Another example: if protected capital is $55 at a later qualifying activation, release $27.50, retain $27.50 protected, and add $27.50 to the existing active balance. Recalculate whole-dollar budgets using the resulting active balance.

Activation rules:

1. It requires a qualifying shutdown event from the immediately preceding configured trading day.
2. It occurs at most once for that shutdown event.
3. It is performed at the configured next-day boundary before new trading begins.
4. It releases only 50% of the protected balance available at activation time.
5. It adds the released amount to existing active capital.
6. It leaves the remaining protected balance protected.
7. It must be persisted atomically and be idempotent across crashes/restarts.
8. No qualifying shutdown means no release.
9. No protected balance means no release.
10. A repeated activation must not reuse an already-consumed event or release the same amount twice.

**Important accounting clarification:** The release is a reclassification between internal capital buckets, not a new profit or deposit. Total equity must not increase when protected capital is released.

## 1.7 Day boundary and timezone

Inspect the current bot's trading-day definition. Do not assume UTC, local machine time, or a calendar date without evidence.

Document:

- Configured trading timezone.
- Exact daily reset boundary.
- How market timestamps are converted.
- How late settlements are attributed to a trading day.
- What happens if the bot is offline at the day boundary.

Use a stable event identifier and date key so a restart cannot trigger duplicate activation.

If the current system has no explicit trading-day timezone, propose one and request user approval before implementation.

---

# 2. Development Governance

## 2.1 Phase gates

There are eight phases. Complete them sequentially.

After every phase:

1. Summarise what was inspected or changed.
2. List every file created, modified, or deleted.
3. Explain the reason for each change.
4. Report commands and tests actually executed.
5. Provide exact test results.
6. List unresolved issues and risks.
7. Stop and request approval before proceeding to the next phase.

Do not start the next phase automatically.

## 2.2 Change safety

- Establish the repository's current branch and working-tree status.
- Do not overwrite uncommitted user work.
- Do not delete files without explicit approval.
- Avoid broad refactors.
- Prefer small, reversible changes.
- Keep existing configuration defaults compatible.
- Do not introduce secrets into source files or logs.
- Never log private keys, API secrets, or sensitive wallet credentials.
- Do not silently migrate or discard existing trade history.
- Back up persistent data before any schema migration, using the repository's established safe backup process.

## 2.3 Financial precision

Use integer cents or an appropriate decimal/money type for internal accounting. Avoid binary floating-point arithmetic for balances, P&L, fees, and allocation splits.

Order execution may require exchange-specific decimal precision, tick size, minimum notional, and fee handling. Keep whole-dollar strategy budget rounding separate from exchange order-price/share precision.

The rounded initial and reversal budgets must not exceed the active funds actually available for the relevant order.

---

# 3. Phase-by-Phase Implementation Plan

# PHASE 1 — Complete Repository Audit (READ-ONLY)

## Objective

Understand the existing working bot and document the precise integration points without changing any file.

## Tasks

### A. Repository inventory

Inspect:

- Project structure and entry points.
- Runtime, language, package manager, and dependency versions.
- Configuration files and environment variable names (never print secret values).
- Existing test framework and test commands.
- Database/storage implementation.
- Dashboard/frontend structure, if present.
- Deployment and process-management setup.
- Logging and monitoring.

### B. Trading lifecycle

Trace one initial trade end to end:

1. Market discovery.
2. Market eligibility checks.
3. Signal generation.
4. Probability/price checks.
5. Order budget calculation.
6. Order submission.
7. Order acknowledgement.
8. Fill/partial-fill handling.
9. Position tracking.
10. Market settlement.
11. P&L calculation.
12. Balance update.
13. Trade-history logging.

Trace one reversal trade through the same lifecycle.

### C. Existing daily shutdown

Locate and document:

- Where initial failures are counted.
- Where reversal failures are counted.
- How a failed trade is defined.
- How the two failure types are combined.
- Where shutdown is triggered.
- How new entries are blocked.
- How open and pending orders are handled.
- How daily counters reset.
- Whether shutdown survives restart.
- How the next trading day is identified.

### D. Current capital accounting

Identify the authoritative source of:

- Wallet/collateral balance.
- Available funds.
- Open-order reserved funds.
- Position collateral.
- Realised P&L.
- Fees.
- Current initial size.
- Current reversal size.

Determine whether the bot already maintains an internal balance and whether it reconciles with the exchange.

### E. Data and restart behaviour

Inspect persistence for:

- Trade IDs.
- Order IDs.
- Market IDs.
- Settlement IDs.
- Daily failure counters.
- Shutdown state.
- Balance snapshots.
- Open positions.
- Pending orders.
- Restart recovery.

### Required deliverables

Create a read-only audit report with:

1. Repository architecture.
2. Module/file responsibility map.
3. Initial trade lifecycle.
4. Reversal trade lifecycle.
5. Settlement and P&L lifecycle.
6. Existing daily shutdown lifecycle.
7. Capital-accounting source of truth.
8. Persistence and restart behaviour.
9. Candidate integration points.
10. Risks, ambiguities, and missing information.
11. Proposed minimal file-change plan for Phases 2–7.
12. Exact commands available for testing.

## Phase 1 restrictions

**Do not modify, create, delete, rename, format, or migrate any repository file.**

Do not implement anything.

At completion, present the audit and wait for approval.

---

# PHASE 2 — Detailed Capital Architecture and Data Model

## Objective

Design the capital-management subsystem before integrating it with trading.

## A. Proposed conceptual components

Adapt these responsibilities to the existing repository; do not create duplicate services unnecessarily.

1. Capital ledger.
2. Capital allocation manager.
3. Trade budget calculator.
4. Profit/loss allocation processor.
5. Milestone activation handler.
6. Next-day reserve activation handler.
7. Daily-shutdown event integration.
8. Reconciliation service.
9. Audit/event history.

## B. Required state

At minimum, determine how to persist:

```text
total_equity
active_capital
protected_capital
realised_profit_total
realised_loss_total
milestone_44_activated
daily_shutdown_status
daily_shutdown_reason
daily_shutdown_trading_day
next_day_activation_eligible
last_activated_shutdown_event_id
last_activation_trading_day
capital_ledger_version
last_processed_settlement_id
```

These are conceptual fields. Adapt names and schema to the existing technology.

Also account for:

- Open-order reservations.
- Open-position collateral.
- Pending settlements.
- Fees.
- Any separately parked or unallocated balance required by reconciliation.

## C. Capital invariants

The following invariants must always hold:

1. Active capital cannot be negative.
2. Protected capital cannot be negative.
3. Protected capital cannot fund ordinary orders.
4. Reclassifying protected capital into active capital cannot change total equity.
5. A settlement can affect capital only once.
6. A milestone can activate only once.
7. A shutdown event can release reserve only once.
8. Open collateral cannot be counted as free active capital.
9. Internal equity must reconcile with exchange funds and tracked positions within documented tolerances.
10. No state transition may silently discard funds.

## D. State transitions

Document and test conceptual transitions:

- Initial capital creation.
- Pre-milestone profit/loss.
- First $44 milestone.
- Post-milestone profit allocation.
- Post-milestone loss deduction.
- Qualifying daily shutdown.
- Next-day reserve release.
- Restart during a pending transition.
- Late settlement.
- Reconciliation mismatch.

## Deliverables

- Data model proposal.
- Ledger transaction types.
- State-transition diagram.
- Function/API design.
- Persistence and migration proposal.
- Invariant list.
- Failure/recovery scenarios.

Do not integrate with live order execution in this phase.

Wait for approval.

---

# PHASE 3 — Dynamic Trade-Sizing Engine

## Objective

Replace fixed strategy budgets with capital-derived budgets while preserving the strategy signals.

## A. Budget calculation

For active capital `A`:

```text
initial_raw = A / 11
reversal_raw = (A * 10) / 11

initial_budget = round_half_up_to_whole_dollar(initial_raw)
reversal_budget = round_half_up_to_whole_dollar(reversal_raw)
```

Use a deterministic decimal rounding function.

## B. Required examples

| Active capital | Initial budget | Reversal budget |
|---:|---:|---:|
| $11 | $1 | $10 |
| $22 | $2 | $20 |
| $33 | $3 | $30 |
| $40 | $4 | $36 |
| $43 | $4 | $39 |
| $44 | $4 | $40 |
| $55 | $5 | $50 |
| $66 | $6 | $60 |
| $88 | $8 | $80 |
| $110 | $10 | $100 |
| $220 | $20 | $200 |

## C. Execution safety

Before an order is submitted:

1. Calculate the appropriate initial or reversal budget.
2. Determine free active funds.
3. Subtract active funds committed to open orders/positions as required by the actual execution model.
4. Include fees and exchange-specific constraints.
5. Verify the requested order fits the active budget.
6. Reject unsafe orders.
7. Never fall back to protected capital.
8. Record the calculated budget and the final accepted/rejected amount.

Do not assume the initial and reversal budgets can both be used at once. Inspect the existing strategy lifecycle and preserve its current sequencing.

## D. Integration boundary

The sizing engine may provide a budget to existing execution code. It must not:

- Generate signals.
- Select markets.
- Change probability thresholds.
- Change reversal logic.
- Alter settlement classification.

## E. Tests

Unit-test:

- All specified example balances.
- Values immediately below and above whole-dollar rounding boundaries.
- Exact half-dollar ties.
- Very small balances.
- Zero balance.
- Negative/invalid input.
- Insufficient free active funds.
- Open-position reservations.
- Fee/minimum-order constraints.

Wait for approval after the phase report.

---

# PHASE 4 — Capital Ledger and Compounding Engine

## Objective

Apply real settlement results to active and protected capital.

## A. Pre-milestone accounting

Before the first $44 milestone, the eligible capital pool is entirely active.

Use the existing account-reconciliation model to establish whether realised gains/losses change active equity immediately or only after settlement and collateral release.

## B. First milestone

When reconciled total equity reaches at least $44:

```text
active_capital = eligible_equity * 0.50
protected_capital = eligible_equity - active_capital
milestone_44_activated = true
```

Use cents for the split; if there is an odd cent, define a deterministic allocation policy and document it.

At $44 exactly:

```text
active = $22
protected = $22
```

Persist the transition atomically.

## C. Subsequent net realised profits

For net realised profit `profit`:

```text
active_share = profit * 0.50
protected_share = profit - active_share

active_capital += active_share
protected_capital += protected_share
```

Use cents, with the remainder cent allocated deterministically so the total is preserved.

## D. Subsequent realised losses

For a realised loss `loss`:

```text
active_capital -= loss
```

Do not deduct the loss from protected capital through ordinary trading accounting.

If the active pool cannot cover a loss that has already occurred, record the accounting shortfall and halt new orders until reconciled. Do not fabricate funds or silently borrow protected capital.

## E. Settlement idempotency

Each settlement must have a unique key.

Before applying a settlement:

- Check whether it has already been processed.
- Apply the net P&L once.
- Persist the settlement ID and ledger transaction atomically.
- Update the relevant trade state.
- Reconcile the resulting balances.

A restart or duplicate callback must not credit/debit the same settlement again.

## F. Ledger history

Every transaction should record:

- Unique ledger transaction ID.
- Event type.
- Trade/order/market/settlement references.
- Timestamp.
- Trading-day key.
- Amount.
- Active balance before/after.
- Protected balance before/after.
- Total equity before/after.
- Fee treatment.
- Reason/source.
- Idempotency key.

Do not expose credentials or private account data in the ledger.

## G. Tests

Verify:

- $44 milestone.
- $10 profit split.
- Multiple sequential profit splits.
- Active-only loss deduction.
- Fee-adjusted net P&L.
- Duplicate settlement.
- Late settlement.
- Crash between processing and persistence.
- Odd-cent allocation.
- Reconciliation mismatch.

Wait for approval.

---

# PHASE 5 — Integrate Existing Daily Shutdown

## Objective

Connect the new capital system to the existing daily shutdown without changing its trigger.

## A. Preserve existing condition

The exact condition remains:

```text
failed_initial_count >= 1
AND
failed_reversal_count >= 1
AND
both failures belong to the same configured trading day
```

This is a description of the intended condition. Prefer the repository's existing logic if it already implements the equivalent behaviour correctly.

## B. Failure classification

Inspect existing settlement logic and document:

- What makes an initial trade failed.
- What makes a reversal trade failed.
- Whether partial fills count.
- Whether cancelled orders count.
- Whether an expired/unfilled market order counts.
- How fees are included.
- How unresolved positions are treated.

Do not change these definitions without explicit approval.

## C. Shutdown event

When the existing condition fires:

- Preserve the existing new-entry block.
- Preserve existing safe cancellation behaviour.
- Continue safe handling of open positions.
- Persist the shutdown.
- Create one unique qualifying shutdown event.
- Mark it eligible for next-day reserve activation.

Do not create a second competing shutdown controller.

## D. Trading-day handling

Use the existing configured timezone and daily boundary if present.

If absent or ambiguous, stop and request approval for the timezone/boundary before coding.

## E. Restart tests

Test restart:

- Before the first failure.
- After an initial failure only.
- After a reversal failure only.
- After both failures.
- After shutdown has triggered.
- At the daily boundary.
- With an unresolved position.

The shutdown must not be bypassed by restarting the bot.

Wait for approval.

---

# PHASE 6 — Next-Day Protected Capital Activation (Option B)

## Objective

Release half of protected capital after a qualifying previous-day shutdown and ADD it to the existing active capital.

## A. Eligibility

Reserve release requires:

- A qualifying daily shutdown event.
- The next configured trading day has begun.
- The event has not already been activated.
- Protected capital is greater than zero.
- Capital and open-position state have been reconciled sufficiently to permit safe trading.

No qualifying shutdown means no release.

## B. Formula

```text
released_amount = protected_capital * 0.50

new_active_capital =
    existing_active_capital + released_amount

new_protected_capital =
    protected_capital - released_amount
```

## C. Required example

Before:

```text
active_capital = $30
protected_capital = $110
total_equity = $140
```

Release:

```text
released_amount = $55
```

After:

```text
active_capital = $85
protected_capital = $55
total_equity = $140
```

New sizing:

```text
initial_budget = round($85 / 11) = $8
reversal_budget = round(($85 * 10) / 11) = $77
```

## D. Repeated qualifying shutdowns

If a later qualifying shutdown occurs with protected capital of $55:

```text
released_amount = $27.50
remaining_protected = $27.50
```

Add $27.50 to the existing active capital. Do not replace it.

Apply whole-dollar rounding only to the derived trade budgets, not to the internal capital balances. Preserve cents in the ledger.

## E. Exactly-once activation

Use a unique shutdown event ID and an activation record.

The activation must be atomic:

1. Verify eligibility.
2. Verify the event has not been activated.
3. Calculate 50% of current protected capital.
4. Reclassify that amount.
5. Record the activation event.
6. Mark the shutdown event consumed.
7. Commit all changes together.

If the process crashes, recovery must determine whether the transaction committed. It must never release the same reserve twice.

## F. Activation timing

Perform activation before new orders are allowed on the next configured trading day.

If the bot was offline at the boundary:

- Recover the prior shutdown event.
- Reconcile current balances and positions.
- Perform the eligible activation once.
- Only then permit new entries.

Do not apply multiple releases for the same event.

## G. Tests

- $30 active / $110 protected → $85 active / $55 protected.
- No shutdown → no release.
- No protected capital → no release.
- Repeated activation attempt → no second release.
- Crash before commit.
- Crash after commit.
- Restart next day.
- Two separate qualifying shutdown events on separate days.
- Late settlement affecting prior-day accounting.
- Open/unresolved position at activation time.

Wait for approval.

---

# PHASE 7 — Persistence, Dashboard, Monitoring and Reconciliation

## Objective

Make capital state transparent, durable, and auditable.

## A. Persistence

Use the existing database or storage architecture unless the audit establishes a compelling reason to change it.

Persist:

- Active capital.
- Protected capital.
- Total equity snapshot.
- Milestone activation.
- Ledger transaction history.
- Daily failure counters.
- Shutdown events.
- Reserve activation events.
- Processed settlement IDs.
- Open orders and positions or their reliable references.
- Last successful reconciliation.
- Schema/version metadata.

Do not create competing balance sources.

## B. Dashboard

Extend the existing dashboard rather than replacing it.

Show, as applicable:

1. Total equity.
2. Active capital.
3. Protected capital.
4. Current initial budget.
5. Current reversal budget.
6. Realised net P&L.
7. Fees.
8. Open exposure.
9. Reserved collateral.
10. Daily initial failure count.
11. Daily reversal failure count.
12. Existing daily shutdown status.
13. Next-day activation eligibility.
14. Last reserve activation.
15. Capital ledger history.
16. Reconciliation status.

Clearly distinguish:

- Spendable active funds.
- Active funds already reserved.
- Protected funds.
- Unsettled funds.
- Total equity.

## C. Monitoring and logs

Log state transitions with unique event IDs.

Useful events include:

```text
CAPITAL_INITIALIZED
MILESTONE_44_ACTIVATED
TRADE_BUDGET_CALCULATED
ORDER_BUDGET_REJECTED
SETTLEMENT_PROCESSED
PROFIT_ALLOCATED
LOSS_DEDUCTED
DAILY_SHUTDOWN_TRIGGERED
NEXT_DAY_ACTIVATION_ELIGIBLE
PROTECTED_CAPITAL_RELEASED
CAPITAL_RECONCILIATION_COMPLETED
CAPITAL_RECONCILIATION_MISMATCH
```

Never log private keys, API secrets, or sensitive credentials.

## D. Restart recovery

After restart:

1. Load the durable ledger.
2. Restore active/protected balances.
3. Restore milestone status.
4. Restore daily shutdown state.
5. Restore pending activation eligibility.
6. Restore processed settlement IDs.
7. Reconcile exchange balances, open orders, and positions.
8. Resolve incomplete transactions safely.
9. Recalculate trade budgets.
10. Only then enable new entries if the existing strategy permits them.

## E. Reconciliation

Internal accounting must not drift silently from actual exchange collateral and position state.

Document how the exchange represents:

- Available collateral.
- Locked collateral.
- Open-order commitments.
- Position value.
- Settlement payouts.
- Fees.
- Withdrawals/deposits, if relevant.

If there is a mismatch, block new orders and report the discrepancy. Do not automatically transfer protected capital into active capital to hide it.

Wait for approval.

---

# PHASE 8 — Historical Replay, Simulation and Paper Validation

## Objective

Prove the new capital manager works without risking live funds.

## A. Historical CSV replay

Use the user's existing `paper_trades.csv` if available.

First inspect its schema and sample records.

Determine whether it contains enough information to reconstruct:

- Initial versus reversal type.
- Entry amount.
- Settlement outcome.
- Net realised P&L.
- Fees.
- Trade/settlement ID.
- Timestamp and trading-day attribution.

Do not infer missing settlement results or fabricate timestamps.

If the CSV is incomplete, identify which tests can be performed and which require additional logs.

## B. Replay modes

Where possible, compare:

1. Existing fixed-size behaviour.
2. New dynamic-sizing behaviour.
3. New capital allocation without reserve release.
4. New capital allocation with Option B next-day release.

Historical replay must be clearly labelled as a simulation. Do not imply that historical results prove future profitability.

## C. Mandatory acceptance tests

### Test 1 — Starting capital

Input:

```text
total_equity = $11
```

Expected:

```text
active = $11
protected = $0
initial = $1
reversal = $10
```

### Test 2 — Pre-milestone sizing

Input:

```text
active = $33
```

Expected:

```text
initial = $3
reversal = $30
protected = $0
```

### Test 3 — Exact milestone

Input:

```text
total_equity = $44
```

Expected:

```text
active = $22
protected = $22
initial = $2
reversal = $20
```

### Test 4 — Post-milestone profit

Input:

```text
active = $22
protected = $22
net_profit = $10
```

Expected:

```text
active = $27
protected = $27
total = $54
```

### Test 5 — Post-milestone loss

Input:

```text
active = $27
protected = $27
net_loss = $5
```

Expected:

```text
active = $22
protected = $27
total = $49
```

### Test 6 — Daily shutdown

Input:

```text
failed_initial = true
failed_reversal = true
same_trading_day = true
```

Expected:

- Existing shutdown triggers.
- New entries blocked.
- Shutdown state persisted.
- Next-day activation eligibility recorded.

### Test 7 — Single failure only

Run two independent cases:

- Initial failure only.
- Reversal failure only.

Expected in both cases: this specific combined-condition shutdown must not trigger.

### Test 8 — Option B activation

Input:

```text
active = $30
protected = $110
qualifying_previous_day_shutdown = true
```

Expected:

```text
released = $55
active = $85
protected = $55
total = $140
initial = $8
reversal = $77
```

### Test 9 — No qualifying shutdown

Input:

```text
active = $30
protected = $110
qualifying_previous_day_shutdown = false
```

Expected:

```text
active = $30
protected = $110
no_release = true
```

### Test 10 — Exactly-once activation

Trigger the same activation event twice, including across a restart.

Expected: only one release.

### Test 11 — Insufficient active funds

Expected:

- Reject unsafe order.
- Protected funds remain untouched.
- Existing state remains consistent.

### Test 12 — Restart recovery

Restart after:

- Trade submission.
- Partial fill.
- Settlement.
- Milestone activation.
- Daily shutdown.
- Reserve activation.

Expected: no duplicated trades, settlements, or capital transitions.

### Test 13 — Reconciliation mismatch

Expected:

- New orders blocked.
- Mismatch logged.
- No silent capital reclassification.

### Test 14 — Existing strategy regression

Run the existing test suite and verify:

- Same market-selection rules.
- Same signal decisions for equivalent inputs.
- Same reversal triggers for equivalent inputs.
- Same daily shutdown condition.
- Same settlement classification.

Only the approved capital budgets and accounting behaviour should differ.

## D. Paper-trading soak test

After all tests pass:

- Run in paper mode.
- Observe actual bot state transitions.
- Compare internal ledger with paper execution records.
- Verify daily reset and next-day activation.
- Verify restart recovery.
- Verify order sizing.
- Verify protected capital isolation.

Define the observation duration and pass criteria with the user before treating the soak test as complete.

Do not enable live trading without explicit approval.

---

# 4. Final Acceptance Criteria

The project is complete only when all of the following are demonstrated:

- [ ] Existing BTC 5-minute trading strategy preserved.
- [ ] Existing initial entry logic preserved.
- [ ] Existing reversal logic preserved.
- [ ] Existing daily shutdown condition preserved.
- [ ] Starting capital of $11 supported.
- [ ] Initial/reversal 1:10 budget calculation implemented.
- [ ] Whole-dollar half-up rounding implemented.
- [ ] $44 milestone activates exactly once.
- [ ] Initial 50/50 capital split is correct.
- [ ] Subsequent net realised profits split 50/50.
- [ ] Realised losses reduce active capital only.
- [ ] Protected capital is excluded from ordinary order budgets.
- [ ] Open exposure and reserved funds are accounted for.
- [ ] Daily combined failure condition is correctly persisted.
- [ ] Next-day activation requires a qualifying previous-day shutdown.
- [ ] Option B adds 50% of protected capital to existing active capital.
- [ ] Remaining protected capital is correct.
- [ ] Activation is idempotent.
- [ ] Settlement processing is idempotent.
- [ ] Restart recovery works.
- [ ] Exchange reconciliation works or safely blocks orders on mismatch.
- [ ] Dashboard exposes the relevant capital state.
- [ ] Historical replay is completed to the extent supported by available data.
- [ ] Unit and integration tests pass.
- [ ] Paper-trading validation passes.
- [ ] No live deployment occurs without user approval.

---

# 5. Required Phase Completion Report Format

At the end of each phase, return:

## Phase [N] Completion Report

### 1. Status
Completed / Partially completed / Blocked.

### 2. Work performed
Detailed description of the work.

### 3. Files inspected
List paths and purpose.

### 4. Files created or modified
For every file, describe the exact change. If none, explicitly say none.

### 5. Tests executed
List commands, test cases, and actual results.

### 6. Capital/accounting validation
Show relevant before/after balances and invariants.

### 7. Risks and unresolved questions
Do not hide incomplete work.

### 8. Next phase proposal
Explain what will happen next, but do not execute it.

### 9. Approval gate
Stop and wait for explicit user approval.

---

# 6. Immediate Instruction — Begin Phase 1 Only

Start by inspecting the complete existing repository.

Do not modify any file.

Do not implement the capital manager.

Do not alter the trading strategy.

Do not modify configuration or persistent data.

Produce the complete Phase 1 audit, including the exact integration plan and any ambiguities that require my decision.

**Wait for my explicit approval before beginning Phase 2.**
