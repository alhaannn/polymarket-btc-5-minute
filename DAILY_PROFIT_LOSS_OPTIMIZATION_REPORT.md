# Daily Profit Target & Maximum Loss Optimization Report — Paper 1 (Initials Strategy)

**Analysis Date:** 2026-11-26  
**Scope:** Strictly Paper 1 (`data/paper1_ledger.db`, BTC 5-Minute Initial-Only Strategy, `$5.00` flat stake, zero reversals)  
**Sample Period:** 2026-11-21 00:43 IST – 2026-11-26 15:43 IST (`238` settled trades across `5` complete Prime Hours sessions + `2` partial boundary sessions)  
**Compliance Mode:** **Read-Only Analysis** — zero source files, configuration files, databases, or running PM2 processes were modified.

---

## Executive Summary & Headline Recommendations

| Optimization Parameter | Current / Proposed | **Recommended Value (Provisional)** | Primary Rationale |
| :--- | :--- | :--- | :--- |
| **A. Daily Profit Target** | `$4.00` (Proposed) | **`$10.00` per session** *(or `Uncapped` if maximizing pure EV)* | `$4.00` loses money historically (`-$0.53` with `LL=-$10`, `-$1.72` unstopped) and sacrifices **61%–66%** of Monte Carlo expected profit due to negative payoff skew (`+$0.58` win vs `-$5.005` loss). **`$10.00`** restores 1:1 daily risk/reward symmetry, captures high-WR evening hours (`17:30–01:00 IST`), delivers the best finite-target historical P&L (`+$14.48` / 5 full days; `+$17.44` / 7 sessions), and achieves a **1.63 Sharpe** in Monte Carlo (`+$78.82 / 30 days`). |
| **B. Daily Maximum Loss** | None (`DISABLED`) | **`-$10.00` per session** *(assuming bankroll ≥ `$25–$60`; see `$10` bankroll caveat below)* | Sits just beyond a normal 2-loss early swing (`-$8.95`), preserving `Nov 24` (`-$8.95 → +$7.62` recovery) while halting genuine tail-bleed days (`Nov 25` stopped at `-$12.41`, saving `$1.20` and 18 trades). Delivers **highest 5-day P&L (`+$18.43` unstopped / `+$14.48` with `PT=$10`)** and **highest Return-to-Drawdown (`1.48x`–`1.72x`)**. |
| **Critical Bankroll Warning** | `$10.00` start balance | **Increase Paper/Live bankroll to `$50–$60`** *(or temporarily cap at `-$5.00` while equity < `$20`)* | With a `$5.00` flat stake and discrete `-$5.005` losses, a `$10.00` wallet has only **2 bullets** and suffers a **~59%–66% 30-day ruin risk** if an early losing session occurs before profits build a buffer. A `-$10.00` daily limit can overshoot to `-$12.41`–`-$14.78` on the triggering trade. |

---

## 1. Historical Performance Overview & Data Integrity

### 1.1 Data Integrity Verification (`paper1_ledger.db`)

All records were queried read-only directly from `/home/ubuntu/polymarket-btc-5-minute/data/paper1_ledger.db` and cross-checked against `.env.paper1`:

- **Strategy Purity:** `238 / 238` rows in `paper_trades` have `signal_type = 'INITIAL'`, `action = 'BUY'`, `status = 'SETTLED'`, `stake_usd = 5.00`, and `slug` starting with `btc-updown-5m-`. Zero reversal, early-exit, pending, or rejected orders exist (`processed_settlements = 238`, unique `order_id = 238`, unique `slug = 238`).
- **Schedule Compliance:** `0 / 238` trades occurred outside the configured Prime Hours window (`13:30 – 01:00 IST`).
- **Ledger Arithmetic Integrity:** Across all `238` trades, `payout_usd - stake_usd - fee_usd` matches `pnl_usd` with a maximum discrepancy of **`$0.000000`**.
  - **Gross Winning Trades (`217`):** `+$125.3005` (`+$0.5774` avg win; min `+$0.4895`, max `+$0.9474`, avg entry `0.8958`).
  - **Gross Losing Trades (`21`):** `-$105.1050` (`-$5.0050` exact per loss: `$5.00` stake + `$0.0050` taker fee; avg entry `0.9020`).
  - **Total Fees Paid:** `$1.1900` (`$0.0050` per trade).
  - **True Realized Cumulative P&L:** **`+$20.1955`** (`+$20.20`). Starting from `$10.00`, true economic equity is **`$30.20`**.
- **Capital-State Accounting Discrepancy Found (Important):**
  - `capital_state` reports `total_equity_cents = 3943` (`$39.43` in `protected_capital_cents`, `$0.00` in `active_capital_cents`) alongside `active_shortfall_cents = 936` (`$9.36`), because after crossing the `$44.00` milestone at trade #126 (`$44.08` total → `$22.04` active / `$22.04` protected), subsequent losses exhausted `active_capital` to `$0` and accrued into `active_shortfall` rather than deducting from `protected_capital` (`$39.43 - $9.36 = $30.07`, matching cent-rounded realized profit `$125.07 - $105.00 + $10.00 = $30.07`).
  - **Methodology Rule:** All P&L and optimization calculations in this report strictly use the exact trade-level `paper_trades.pnl_usd` ledger rather than `capital_state.total_equity_cents`.
- **Execution Overlap Note:** `14` consecutive trade pairs had `trade[i].exec_ts < trade[i-1].settled_at_ts` (sub-second/few-second overlap near window rollover), meaning shutdown checks must evaluate promptly on settlement and block new entries when an unresolved prior position exists if strict exposure capping is required.

### 1.2 Global Trade & Payoff Statistics (`N = 238`)

| Metric | Value | Notes |
| :--- | :--- | :--- |
| **Total Trades / Wins / Losses** | `238` / `217` / `21` | `91.18%` observed win rate |
| **Break-Even Win Rate** | **`89.66%`** | Computed as `AvgLoss / (AvgWin + AvgLoss) = 5.0050 / (0.5774 + 5.0050)` |
| **Expected Value (EV) per Trade** | **`+$0.0849`** | `SD = $1.5884`, `SE = $0.1030` |
| **Global Max Streaks** | **47 Wins / 2 Losses** | Loss streak lengths observed: seventeen `1`-loss events and two `2`-loss events |
| **Global Equity Peak / Max DD** | `+$36.82` / **`-$21.00`** | Multi-day peak-to-trough drawdown across the 238-trade equity curve |

### 1.3 Session-by-Session Performance (`13:30 – 01:00 IST` Trading Sessions)

Because Paper 1 runs `PRIME_HOURS_ONLY=true` (`13:30 – 01:00 IST`), each continuous overnight shift is the true operational trading session. There are **5 complete sessions** (`Nov 21`–`Nov 25`, `223` trades, `+$17.23`) and **2 partial boundary sessions** (`Nov 20` launch tail = `3` trades; `Nov 26` in-progress = `12` trades).

| Session (`13:30–01:00 IST`) | Status | Trades (`W / L`) | Win Rate | Net P&L | Intraday Peak | Intraday Trough | Max Intraday DD | Giveback (`Peak - End`) | Max Streak (`W / L`) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **2026-11-20** *(00:43–00:58 tail)* | Partial | `3` (`3 / 0`) | `100.0%` | `+$1.78` | `+$1.78` | `$0.00` | `$0.00` | `$0.00` | `3W / 0L` |
| **2026-11-21** | **Full** | `49` (`48 / 1`) | `98.0%` | **`+$22.50`** | `+$22.50` | `-$1.15` | `-$5.00` | `$0.00` | `41W / 1L` |
| **2026-11-22** | **Full** | `46` (`41 / 5`) | `89.1%` | **`-$0.96`** | `+$3.55` | `-$8.74` | `-$12.29` | `$4.52` | `19W / 1L` |
| **2026-11-23** | **Full** | `41` (`37 / 4`) | `90.2%` | **`+$1.67`** | `+$13.51` | `-$0.96` | `-$14.46` | **`$11.83`** | `18W / 2L` |
| **2026-11-24** | **Full** | `42` (`39 / 3`) | `92.9%` | **`+$7.62`** | `+$7.62` | **`-$8.95`** | `-$8.95` | `$0.00` | `26W / 1L` |
| **2026-11-25** | **Full** | `45` (`38 / 7`) | `84.4%` | **`-$13.61`** | `+$1.23` | **`-$16.79`** | **`-$18.01`** | **`$14.83`** | `11W / 2L` |
| **2026-11-26** *(13:38–15:43)* | Partial | `12` (`11 / 1`) | `91.7%` | `+$1.19` | `+$4.05` | `-$0.96` | `-$5.00` | `$2.86` | `7W / 1L` |
| **5 Full Sessions Total** | — | **`223`** (`203 / 20`) | **`91.03%`** | **`+$17.23`** | — | — | **`-$18.01`** | `$31.18` | — |
| **All 7 Sessions Total** | — | **`238`** (`217 / 21`) | **`91.18%`** | **`+$20.20`** | — | — | **`-$18.01`** | `$34.04` | — |

*(Note: For completeness, calendar IST `00:00–23:59` grouping yields `+$20.38`, `+$6.19`, `-$4.21`, `+$8.11`, `-$13.05` across `Nov 21–25` (`+$17.42` total), because midnight `00:00–01:00 IST` shifts late wins into the next calendar date. Section 9 addresses this rollover bug.)*

### 1.4 Intraday Session Timing, Side/Price Breakdown & Post-Profitability Trajectory

**Performance by Time Window (IST):**

| Session Segment (IST) | Trades (`W / L`) | Win Rate | Net P&L | Avg P&L / Trade |
| :--- | :---: | :---: | :---: | :---: |
| **13:30 – 17:30 IST (Early)** | `86` (`76 / 10`) | `88.37%` | **`-$6.31`** | `-$0.0734` |
| **17:30 – 21:30 IST (Mid)** | `78` (`73 / 5`) | **`93.59%`** | **`+$17.51`** | **`+$0.2245`** |
| **21:30 – 01:00 IST (Late)** | `74` (`68 / 6`) | `91.89%` | **`+$9.00`** | `+$0.1216` |

- **Critical Insight on "Trading Longer After Reaching Profitability":**
  - On days that reached `+$4.00` (`Nov 21, 23, 24`), the **`71` trades executed AFTER passing `+$4.00`** went **`67W / 4L` (`94.4%` WR)** and generated **`+$18.95` net (`+$0.2669/trade`)** — **more than 3× the dataset average EV/trade!**
  - Why? Because `13:30–17:30 IST` accounted for nearly half of all losses (`10 / 21`, `-$6.31`), whereas **`17:30–01:00 IST` generated `+$26.51` (`92.8%` WR across `152` trades)**! Stopping early in the afternoon around `+$2`–`+$4` systematically removes the bot from its highest-performing evening liquidity hours.
- **Secondary Observational Patterns (Entry Price & Side):**
  - `Up` entries (`125` trades): **`95.20%` WR**, `+$39.55`; `Down` entries (`113` trades): **`86.73%` WR**, `-$19.35`.
  - Entry fills at `0.89` (`76` trades): **`97.37%` WR**, `+$35.30`; at `0.90` (`83` trades): `87.95%` WR, `-$9.91`; at `0.91` (`56` trades): **`85.71%` WR**, `-$16.51` (below the `89.66%` breakeven rate). *(Not modified per analysis-only rule; logged in Section 10).*

---

## 2. Phase 2: Daily Profit Target Optimization

Chronological trade-by-trade simulation was run independently across the **5 complete sessions (`Nov 21–25`)** and **all 7 sessions (`Nov 20–26`)**. Once `session_pnl >= Target` on trade settlement, new entries stop for the remainder of the session.

### 2.1 Results on 5 Complete Sessions (`Nov 21 – Nov 25`, `223` Baseline Trades)

| Profit Target | Days Hit | Avg Time to Hit *(from 13:30 / 1st tr)* | Avg Trade # | Total P&L | Avg Daily P&L | Trades Exec | Net Profitable Days Sacrificed (`$`) | Net Losing Days Avoided (`$`) | Profitable Days | Per-Day P&L (`Nov 21..25`) |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **No Target** | `0 / 5` | — | — | **`+$17.23`** | **`+$3.45`** | `223` | `$0.00` | `$0.00` | `3 / 5` (`60%`) | `[+22.50, -0.96, +1.67, +7.62, -13.61]` |
| **`+$1`** | `5 / 5` | `2.13h` / `1.88h` | `8.0` | `+$6.44` | `+$1.29` | `40` | `$27.81` | `$17.02` | `5 / 5` (`100%`) | `[+1.10, +1.23, +1.10, +1.79, +1.23]` |
| **`+$2`** | `4 / 5` | `3.02h` / `2.71h` | `11.2` | `-$4.54` | `-$0.91` | `90` | `$25.71` | `$3.95` | `4 / 5` (`80%`) | `[+2.14, +2.39, +2.26, +2.28, -13.61]` |
| **`+$3`** | `4 / 5` | `3.54h` / `3.23h` | `13.2` | `+$0.18` | `+$0.04` | `98` | `$23.45` | `$6.40` | `4 / 5` (`80%`) | `[+3.24, +3.55, +3.55, +3.44, -13.61]` |
| **`+$4` *(Prop.)*** | **`3 / 5`** | `5.12h` / `4.78h` | `20.3` | **`-$1.72`** | **`-$0.34`** | `152` | `$21.34` | `$2.39` | `3 / 5` (`60%`) | `[+4.12, -0.96, +4.06, +4.67, -13.61]` |
| **`+$5`** | `3 / 5` | `5.46h` / `5.13h` | `21.7` | `+$1.14` | `+$0.23` | `156` | `$19.58` | `$3.49` | `3 / 5` (`60%`) | `[+5.06, -0.96, +5.16, +5.48, -13.61]` |
| **`+$6`** | `3 / 5` | `5.95h` / `5.62h` | `23.7` | `+$4.38` | `+$0.88` | `162` | `$17.44` | `$4.59` | `3 / 5` (`60%`) | `[+6.17, -0.96, +6.26, +6.52, -13.61]` |
| **`+$8`** | `2 / 5` | `5.41h` / `5.11h` | `24.0` | `+$9.72` | `+$1.94` | `181` | `$14.14` | `$6.63` | `3 / 5` (`60%`) | `[+8.36, -0.96, +8.31, +7.62, -13.61]` |
| **`+$10`** | **`2 / 5`** | `6.72h` / `6.41h` | `27.0` | **`+$13.28`** | **`+$2.66`** | `187` | `$12.43` | `$8.48` | `3 / 5` (`60%`) | `[+10.08, -0.96, +10.15, +7.62, -13.61]` |

*(Note: "Net Profitable Days Sacrificed" = sum of additional gains foregone on days that would have ended higher than the stopped value; "Net Losing Days Avoided" = sum of declines avoided on days that ended below the stopped value. On All 7 Sessions (`Nov 20–26`): `NoPT = +$20.20`, `PT=$1 = +$8.65` (`7/7`), `PT=$2 = -$0.55` (`6/7`), `PT=$3 = +$5.46` (`6/7`), `PT=$4 = +$4.10` (`5/7`), `PT=$5 = +$4.10` (`5/7`), `PT=$6 = +$7.35` (`5/7`), `PT=$8 = +$12.69` (`5/7`), `PT=$10 = +$16.25` (`5/7`).)*

### 2.2 Key Profit Target Findings

1. **Why `$4.00` Underperforms (`-$1.72` across 5 full days):** On `Nov 21`, stopping at `+$4.12` (trade #17) surrendered `$18.38` of pure profit (`+$22.50` actual). On `Nov 24`, stopping at `+$4.67` (trade #37) surrendered `$2.95`. In exchange, it saved only `$2.39` on `Nov 23` (`+$4.06` vs `+$1.67`), while **failing to protect `Nov 25` (`-$13.61`)** because `Nov 25` peaked at only `+$1.23`.
2. **Why `+$1.00` (`5/5` wins, `+$6.44`) is a Small-Sample Mirage:** `Nov 21, 22, 23, 25` all happened to start with 2 consecutive wins (`+$1.10` to `+$1.23`) before any loss occurred, while `Nov 24` recovered from `-$8.95` to `+$1.79` only because no loss limit was active. In 5,000 Monte Carlo runs (Section 4.4), `PT=$1` earns only **`$0.38/day`** and loses to `LL=-$10` overshoots (`-$14.40` average) whenever a loss occurs on Trade #1 or #2 (~`16.9%` of sessions).
3. **`$10.00` Dominates All Finite Targets:** Among `$1`–`$10`, **`$10.00`** maximizes total P&L (`+$13.28` / 5 full days; `+$16.25` / 7 sessions) because it exceeds a full single loss (`-$5.005`), lets `Nov 21` and `Nov 23` ride into the prime `17:30–21:30 IST` window, and still locks `+$10.15` on `Nov 23` before its late `$11.83` giveback.

---

## 3. Phase 3: Daily Maximum Loss Optimization

### 3.1 Discrete `$5.005` Overshoot Mechanics (Why Exact-Dollar Shutdowns Are Impossible)

With a `$5.00` flat stake and `90¢` entry, every loss removes **`-$5.005`** in a single indivisible step while every win adds **`~+$0.58`**. Consequently:
- A `-$1`, `-$2`, `-$3`, `-$4`, or `-$5` threshold is **routinely overshot by `$0.15` to `$4.16`** on the triggering trade. For example, losing Trade #1 on `Nov 24` (`-$5.005`) instantly breaches `-$1, -$2, -$3, -$4,` and `-$5` simultaneously!
- Worse, **any threshold from `-$1` through `-$8` triggered on `Nov 24` (`-$5.00` on Trade #1 or `-$8.95` on Trade #4)** and forbade the subsequent `26`-win comeback that finished **`+$7.62`**!

### 3.2 Results on 5 Complete Sessions (`Nov 21 – Nov 25`)

| Max Loss Limit | Total P&L | Avg Daily P&L | Max Daily Loss *(Overshoot)* | Max Intraday DD | Global Curve DD | Days Triggered | Trades Prevented | Net Losing Days Avoided (`$`) | Net Winning Days Sacrificed (`$`) | W / L Days | Per-Day P&L (`Nov 21..25`) |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **No Limit** | `+$17.23` | `+$3.45` | `-$13.61` | `-$18.01` | `-$13.61` | `0 / 5` | `0` | `$0.00` | `$0.00` | `3 / 2` | `[+22.50, -0.96, +1.67, +7.62, -13.61]` |
| **`-$1`** | `-$9.71` | `-$1.94` | `-$5.00` *(+$4.00)* | `-$14.46` | `-$9.71` | `4 / 5` | `163` | `$9.83` | `$36.77` | `1 / 4` | `[-1.15, -1.45, +1.67, -5.00, -3.78]` |
| **`-$2`** | `+$12.43` | `+$2.49` | `-$5.00` *(+$3.00)* | `-$14.46` | `-$10.08` | `3 / 5` | `115` | `$9.83` | `$14.63` | `2 / 3` | `[+22.50, -2.96, +1.67, -5.00, -3.78]` |
| **`-$3`** | `+$8.23` | `+$1.65` | `-$7.16` *(+$4.16)* | `-$14.46` | `-$14.27` | `3 / 5` | `113` | `$9.83` | `$18.83` | `2 / 3` | `[+22.50, -7.16, +1.67, -5.00, -3.78]` |
| **`-$4`** | `+$7.76` | `+$1.55` | `-$7.16` *(+$3.16)* | `-$14.46` | `-$14.75` | `3 / 5` | `104` | `$9.35` | `$18.83` | `2 / 3` | `[+22.50, -7.16, +1.67, -5.00, -4.25]` |
| **`-$5`** | `+$3.37` | `+$0.67` | `-$8.65` *(+$3.65)* | `-$14.46` | `-$19.14` | `3 / 5` | `102` | `$4.96` | `$18.83` | `2 / 3` | `[+22.50, -7.16, +1.67, -5.00, -8.65]` |
| **`-$6`** | `-$0.58` | `-$0.12` | `-$8.95` *(+$2.95)* | `-$14.46` | `-$23.08` | `3 / 5` | `99` | `$4.96` | `$22.77` | `2 / 3` | `[+22.50, -7.16, +1.67, -8.95, -8.65]` |
| **`-$8`** | `-$2.15` | `-$0.43` | `-$8.95` *(+$0.95)* | `-$14.46` | `-$24.66` | `3 / 5` | `92` | `$4.96` | `$24.35` | `2 / 3` | `[+22.50, -8.74, +1.67, -8.95, -8.65]` |
| **`-$10`** | **`+$18.43`** | **`+$3.69`** | `-$12.41` *(+$2.41)* | `-$14.46` | **`-$12.41`** | **`1 / 5`** | `18` | `$1.20` | **`$0.00`** | **`3 / 2`** | `[+22.50, -0.96, +1.67, +7.62, -12.41]` |
| **`-$12`** | **`+$18.43`** | **`+$3.69`** | `-$12.41` *(+$0.41)* | `-$14.46` | **`-$12.41`** | **`1 / 5`** | `18` | `$1.20` | **`$0.00`** | **`3 / 2`** | `[+22.50, -0.96, +1.67, +7.62, -12.41]` |

*(On All 7 Sessions: `LL=-$10` and `LL=-$12` both yield **`+$21.39`** (`+$3.06/day`, `5W / 2L`), strictly outperforming `No Limit` (`+$20.20`) and every `-$1`..`-$8` threshold.)*

### 3.3 Why Tight Daily Loss Limits (`-$5`, `-$6`, `-$8`) Perform Worse Than Both `-$2` and `-$10`

Notice the striking "dead zone" between **`-$5` and `-$8`** (`+$3.37` down to `-$2.15`, with global drawdown ballooning to **`-$24.66`**!):
- At `-$5` to `-$8`, the stop is **too loose to exit after 1 early loss** (`-$3.78` on `Nov 25` kept trading until `-$8.65`; `-$2.96` on `Nov 22` kept trading until `-$7.16`/`-$8.74`; `Nov 24` kept trading until `-$8.95`) — **so you absorb 2-loss damage (~`-$8.70`–`-$8.95`) and THEN shut down right when mathematical mean-reversion/recovery begins!**
- By contrast, **`-$10` (`1.48x` Return/GlobalDD)** gives a `$5`-stake strategy the **minimum 2-to-3-loss runway** needed for normal statistical comebacks (`Nov 22` recovered from `-$8.74` to `-$0.96`; `Nov 24` recovered from `-$8.95` to `+$7.62`), while still cutting off a true tail-loss cascade (`Nov 25` halted at `-$12.41` before hitting `-$16.79`).

---

## 4. Phase 4: Combined Profit Target & Maximum Loss Simulation

### 4.1 Complete `PT × LL` Matrix — Total 5-Day P&L (`Nov 21 – Nov 25`, 5 Full Sessions)

| `PT \ LL` | **`-$1`** | **`-$2`** | **`-$3`** | **`-$4`** | **`-$5`** | **`-$6`** | **`-$8`** | **`-$10`** | **`-$12`** | **`No LL`** |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`+$1`** | `-$0.35` | `-$0.35` | `-$0.35` | `-$0.35` | `-$0.35` | `-$4.29` | `-$4.29` | `+$6.44` | `+$6.44` | `+$6.44` |
| **`+$2`** | `-$1.99` | `-$1.99` | `-$1.99` | `-$2.47` | `-$6.86` | `-$10.80` | `-$10.80` | `-$3.34` | `-$3.34` | `-$4.54` |
| **`+$3`** | `+$1.56` | `+$1.56` | `+$1.56` | `+$1.09` | `-$3.31` | `-$7.25` | `-$7.25` | `+$1.38` | `+$1.38` | `+$0.18` |
| **`+$4` *(Prop.)*** | `-$7.33` | `-$3.57` | `-$7.77` | `-$8.24` | `-$12.63` | `-$16.58` | **`-$18.15`** | **`-$0.53`** | `-$0.53` | `-$1.72` |
| **`+$5`** | `-$6.23` | `-$1.52` | `-$5.72` | `-$6.19` | `-$10.58` | `-$14.53` | `-$16.11` | `+$2.33` | `+$2.33` | `+$1.14` |
| **`+$6`** | `-$5.12` | `+$0.68` | `-$3.51` | `-$3.99` | `-$8.38` | `-$12.33` | `-$13.90` | `+$5.58` | `+$5.58` | `+$4.38` |
| **`+$8`** | `-$3.08` | `+$4.92` | `+$0.72` | `+$0.25` | `-$4.14` | `-$8.09` | `-$9.66` | `+$10.92` | `+$10.92` | `+$9.72` |
| **`+$10`** | `-$1.24` | `+$8.48` | `+$4.28` | `+$3.81` | `-$0.58` | `-$4.53` | `-$6.10` | **`+$14.48`** | **`+$14.48`** | `+$13.28` |
| **`No PT`** | `-$9.71` | `+$12.43` | `+$8.23` | `+$7.76` | `+$3.37` | `-$0.58` | `-$2.15` | **`+$18.43`** | **`+$18.43`** | `+$17.23` |

### 4.2 Complete `PT × LL` Matrix — Global Equity Max Drawdown (5 Full Sessions)

| `PT \ LL` | **`-$1`** | **`-$2`** | **`-$3`** | **`-$4`** | **`-$5`** | **`-$6`** | **`-$8`** | **`-$10`** | **`-$12`** | **`No LL`** |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`+$1`** | `-$5.00` | `-$5.00` | `-$5.00` | `-$5.00` | `-$5.00` | `-$8.95` | `-$8.95` | `$0.00` | `$0.00` | `$0.00` |
| **`+$2`** | `-$8.78` | `-$8.78` | `-$8.78` | `-$9.26` | `-$13.65` | `-$17.60` | `-$17.60` | `-$12.41` | `-$12.41` | `-$13.61` |
| **`+$3`** | `-$8.78` | `-$8.78` | `-$8.78` | `-$9.26` | `-$13.65` | `-$17.60` | `-$17.60` | `-$12.41` | `-$12.41` | `-$13.61` |
| **`+$4`** | `-$8.78` | `-$8.78` | `-$11.89` | `-$12.36` | `-$16.75` | `-$20.70` | `-$22.27` | `-$12.41` | `-$12.41` | `-$13.61` |
| **`+$5`** | `-$8.78` | `-$8.78` | `-$10.78` | `-$11.26` | `-$15.65` | `-$19.59` | `-$21.17` | `-$12.41` | `-$12.41` | `-$13.61` |
| **`+$6`** | `-$8.78` | `-$8.78` | `-$9.68` | `-$10.15` | `-$14.55` | `-$18.49` | `-$20.07` | `-$12.41` | `-$12.41` | `-$13.61` |
| **`+$8`** | `-$8.78` | `-$8.78` | `-$8.78` | `-$9.26` | `-$13.65` | `-$17.60` | `-$18.03` | `-$12.41` | `-$12.41` | `-$13.61` |
| **`+$10`** | `-$8.78` | `-$8.78` | `-$8.78` | `-$9.26` | `-$13.65` | `-$17.60` | `-$17.60` | **`-$12.41`** | **`-$12.41`** | `-$13.61` |
| **`No PT`** | `-$9.71` | `-$10.08` | `-$14.27` | `-$14.75` | `-$19.14` | `-$23.08` | `-$24.66` | **`-$12.41`** | **`-$12.41`** | `-$13.61` |

### 4.3 Spotlight on Proposed `PT = +$4` vs Top Combinations

- **Every combination using `PT = +$4` lost money across the 5 full sessions (`Nov 21–25`)**, ranging from `-$0.53` (`LL=-$10`) to `-$18.15` (`LL=-$8`). Even on all 7 sessions (including partial `Nov 20` and `Nov 26`), `PT=$4, LL=$10` only netted `+$5.30` vs `+$17.44` (`PT=$10, LL=$10`) and `+$21.39` (`NoPT, LL=$10`).
- **Calendar IST (`00:00–23:59`) Comparison (`Nov 21–25`):** Even under calendar-day slicing, `PT=$4, LL=$4..5` lost `-$8.72` (`2W/3L`), `PT=$4, LL=$10` earned `+$7.44` (`4W/1L`), while **`PT=$10, LL=$10` earned `+$24.43` (`4W/1L`, `GlobalDD = -$10.27`)** — beating even `NoPT, LL=$10` (`+$20.20`).

### 4.4 Monte Carlo Stress-Test (5,000 × 30-Day Runs = 6.75M Trades/Scenario)

To avoid overfitting to 5 historical days, we simulated `5,000` synthetic 30-day runs (`45` trades/day) using empirical trade outcomes (`91.18%` WR), the `92.0%` replay baseline, and a pessimistic `89.5%` stress test (just below the `89.66%` breakeven rate):

| `(PT, LL)` Pair | Empirical `91.18%` WR Avg 30d P&L | Prob > `$0` (30d) | Avg Trades / Day | Win Day % | P95 Max 30d DD | P95 Worst Day Loss | Sharpe (30d) | `92.0%` WR Avg 30d P&L | `89.5%` WR Stress 30d P&L |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`(No PT, No LL)`** | `+$113.24` | `97.4%` | `45.0` | `63.3%` | `-$63.96` | `-$30.49` | `1.98` | `+$175.95` | `-$12.45` |
| **`(No PT, -$10)`** | **`+$101.12`** | **`96.1%`** | `39.7` | `61.8%` | `-$62.52` | `-$14.78` | **`1.73`** | `+$161.35` | `-$10.31` |
| **`(+$10, -$10)`** | **`+$78.82`** | **`94.6%`** | `31.3` | **`64.4%`** | `-$61.04` | `-$14.77` | **`1.63`** | **`+$122.14`** | `-$9.00` |
| **`(+$8, -$10)`** | `+$69.30` | `93.9%` | `27.2` | `67.6%` | `-$59.16` | `-$14.75` | `1.58` | `+$104.92` | `-$6.78` |
| **`(+$6, -$10)`** | `+$55.85` | `91.5%` | `22.1` | `72.4%` | `-$55.75` | `-$14.76` | `1.41` | `+$84.43` | `-$6.13` |
| **`(+$4, -$10)`** *(Prop.)* | `+$39.04` | `87.8%` | `15.8` | `79.1%` | `-$52.55` | `-$14.69` | `1.19` | `+$59.87` | `-$5.03` |
| **`(+$4, -$5)`** *(Prop.)* | `+$25.41` | `81.7%` | `10.1` | `67.9%` | `-$44.33` | `-$9.69` | `0.92` | `+$39.42` | `-$3.01` |
| **`(+$2, -$10)`** | `+$21.91` | `82.1%` | `8.6` | `88.1%` | `-$43.96` | `-$14.49` | `0.92` | — | `-$1.71` |
| **`(+$1, -$10)`** | `+$11.28` | `73.1%` | `4.5` | `93.4%` | `-$34.41` | `-$14.40` | `0.66` | — | — |
| **`(+$1, -$5)`** | `+$6.70` | `70.9%` | `2.6` | `86.5%` | `-$27.34` | `-$9.52` | `0.48` | — | `-$0.78` |

**Monte Carlo Bankroll Ruin Test (`P(Equity < $5.00)` within 30 days at `$5.00` flat stake):**
- Starting at **`$10.00`**: Ruin probability is **`52.1%–66.9%`** across all configurations (Paper 1 only survived Day 1 in production because `Nov 21` (`+$22.50`) occurred first!).
- Starting at **`$25.00`**: Ruin probability drops to **`24.7%–25.7%`** for `+$10/-$10` (`20.1%` for `+$4/-$5`).
- Starting at **`$60.00+`** (matching empirical `P95 MaxDD ≈ -$61`): Ruin probability falls below **`5%`**.

---

## 5. Recommended Daily Profit Target

**Recommendation: `+$10.00` per Trading Session (`13:30 – 01:00 IST`) — Provisional.** *(Alternative: Uncapped / `None` if maximum expected return without daily upside truncation is preferred.)*

1. **Reject `$4.00`:** Both historical execution (`-$0.53` with `LL=-$10`; `-$12.63` with `LL=-$5`) and Monte Carlo analysis (`+$39.04/30d`, cutting 61% of expected profits vs `NoPT` and 50% vs `+$10`) show that `$4.00` underperforms materially. Stopping at `$4.00` locks in early afternoon exits (`~18:40 IST`) and misses the highest-WR evening hours (`17:30–01:00 IST`, `92.8%` WR).
2. **Why `+$10.00` is the Best Finite Target:**
   - **1:1 Daily Payoff Symmetry:** Matches the `-$10.00` daily max loss (`1:1`), eliminating negative daily truncation skew.
   - **Historical & Monte Carlo Leader Among Finite Targets:** `+$14.48` over 5 full days (`+$17.44` over 7 sessions), `+$78.82/30d` expected profit at `91.18%` WR (`94.6%` probability of a profitable 30-day period, **Sharpe `1.63`**), while trimming average daily market exposure by `~30%` (`31.3` vs `45.0` trades/day).

---

## 6. Recommended Daily Maximum Loss

**Recommendation: `-$10.00` per Trading Session (`13:30 – 01:00 IST`) — Provisional** *(conditioned on maintaining ≥ `$25–$60` capital buffer; Paper 1 currently holds `$30.20` real equity).*

1. **Why `-$10.00` Outperforms Tighter Limits (`-$1` to `-$8`):**
   - Tight limits (`-$3` to `-$8`) triggered on `3 / 5` sessions (`Nov 22, 24, 25`), forced premature exits during routine 2-loss fluctuations (`Nov 24` recovered from `-$8.95` to `+$7.62`), and suffered severe overshoot penalties (`-$7.16` to `-$8.95`).
   - `-$10.00` triggered **only once** (`Nov 25`, Trade #27 at `-$12.41`), avoided `-$1.20` and 18 trades of further bleed, achieved the **highest 5-day P&L (`+$18.43` unstopped / `+$14.48` with `PT=$10`)**, and reduced worst historical single-day loss from `-$13.61` to `-$12.41` (and in 30-day Monte Carlo, slashes `P95` worst-day loss by **more than half**, from **`-$30.49` down to `-$14.77`**!).
2. **Why `-$10.00` vs `-$12.00`:** Both yielded identical 5-day P&L (`+$18.43`), but `-$10.00` guarantees that two straight losses from breakeven (`-$10.01`) immediately halt trading rather than permitting a 3rd loss to push realized daily loss to `-$15.02`.
3. **Capital-Sizing Guardrail (Important for Fresh `$10` Accounts):** Do NOT deploy `-$10.00` max loss on a fresh `$10.00` real-money wallet with `$5.00` stakes (a 2-loss `-$10.01` to `-$12.41` overshoot exceeds `$10.00`). Either fund **`$50–$60`** per `$5` unit (recommended), drop stake to **`$1.00`** for `$10` wallets, or use a temporary **`-$5.00` (1-loss stop)** ONLY while active balance `< $20.00`.

---

## 7. Expected Performance & Risk Implications

Operating under **`PT = +$10.00` / `LL = -$10.00`** (with `$5.00` flat stakes during `13:30–01:00 IST`):

- **Expected Daily Performance (at empirical `91.18%` WR):**
  - **Expected 30-Day Profit:** **`+$78.82`** (`+$2.63/day` average; **`+$122.14`** if WR matches the `92.0%` historical replay).
  - **Expected Profitable Days:** **`64.4%`** (`~19–20` winning days per month; `~43.1%` hit `+$10` target, `~21.9%` hit `-$10` stop, `~35.0%` finish between `-$10` and `+$10`).
  - **Expected Realized Daily Stop Overshoot:** When `-$10.00` triggers, expect actual realized loss of **`-$11.00` to `-$14.80`** (`P95 = -$14.77`).
  - **30-Day Tail Drawdown (`P95 MaxDD`):** **`-$61.04`** (requiring at least `$60`–`$75` capital per `$5` stake for `< 5%` ruin risk).
- **Downside Regime Sensitivity (`89.5%` WR Stress):** Because `89.66%` is breakeven, a `1.7%` drop in win rate flips expectation slightly negative (`-$9.00/30d`), with `-$10` daily stop keeping worst single-day damage capped near `-$14.87` instead of `-$40.76` uncapped.

---

## 8. Statistical Limitations & Overfitting Risks

1. **Insufficient Sample Size for Statistical Significance (`N = 238` / 5 Full Sessions):**
   - Observed win rate `91.18%` has a **95% Wilson CI of `[86.89%, 94.16%]`**, which **spans across the `89.66%` breakeven threshold**.
   - Trade EV `+$0.0849` has `SE = $0.1030` (`t = 0.82`, 95% CI `[-$0.1169, +$0.2867]`). Reaching 95% statistical confidence ($t \ge 1.96$) requires roughly **`~1,345` trades (~30 full sessions)**.
2. **Extreme Single-Day Outliers Skew Short-Sample Totals:**
   - `Nov 21` (`48W/1L`, `98.0%`, `+$22.50`; binomial $p = 6.21\%$) and `Nov 25` (`38W/7L`, `84.4%`, `-$13.61`; $p = 9.79\%$) dominate the 5-day totals. Removing `Nov 21` alone turns unstopped 5-day P&L negative (`-$5.27`).
3. **No Evidence of Serial Loss Clustering:** Same-session conditional probability `P(Loss | Prev=Loss) = 9.52%` (`2/21`) vs `P(Loss | Prev=Win) = 8.57%` (`18/210`). Individual trade outcomes behave closely to i.i.d. Bernoulli trials rather than momentum loss cascades — meaning tight loss stops mainly cut trade volume, not future per-trade edge, and explains why `PT=$1` (`5/5` win streak) is an overfit ordering artifact.
4. **Paper Execution Assumption:** Paper fills assume simulated taker liquidity at top-of-book asks without live partial-fill / CLOB queue degradation on `$5.00` orders.

---

## 9. Recommended Shutdown Logic (Design Specification — Not Implemented)

1. **Session-Aligned Reset Boundary (Critical Fix Needed in Future Code):**
   - Currently `polybot/risk.py` (`_check_day_rollover`) resets `daily_pnl` using `ist_today_str()` (**`00:00 IST`**), which splits the `13:30–01:00 IST` Prime Hours session! A shutdown triggered at `23:30 IST` would wrongly reset 30 minutes later at `00:00 IST` and resume trading from `00:00–01:00 IST`.
   - **Spec:** Key session P&L to **`session_start_ts`** (from `compute_schedule_state` in `polybot/tz.py`, i.e., `13:30 IST`), resetting counters only when `session_start_ts` advances at `13:30 IST`.
2. **Independent Dual-Condition Latch (Realized Settled P&L):**
   - Track `session_realized_pnl_usd = sum(pnl_usd)` strictly from settled `paper_trades` where `exec_ts >= session_start_ts`.
   - Evaluate immediately upon every settlement:
     - `if session_realized_pnl_usd >= +10.00: trigger_session_shutdown("DAILY_PROFIT_TARGET_HIT", until=next_1330_ist)`
     - `elif session_realized_pnl_usd <= -10.00: trigger_session_shutdown("DAILY_MAX_LOSS_HIT", until=next_1330_ist)`
   - **Pre-Entry Guard:** Block any new signal if `shutdown_active` OR if a prior trade in `paper_trades` is still unsettled (eliminates the `14` exec/settle timestamp overlaps).
3. **Decouple Fixed-Stake Ledger from Milestone Capital Split:**
   - When `SIZING_MODE=fixed`, bypass the `$44` Protected Capital / `active_shortfall` split so `active_capital` accurately mirrors true economic equity (`$30.20`) rather than clamping to `$0.00`.

---

## 10. Recommended Next Steps

1. **Keep Paper 1 Running for 15–25 Additional Full Sessions (Until `N ≥ 1,000` Trades):** Validate whether the `91.18%` win rate stabilizes safely above the `89.66%` breakeven line (`> 91.5%` target) before treating thresholds as permanent.
2. **Align Paper 1 Bankroll & Test Implementation in a Separate Controlled Phase:** When ready to implement code changes (outside this analysis-only phase), adjust Paper 1 / Live capital sizing (`≥ $50–$60` per `$5` flat unit), implement the `13:30 IST` session-keyed dual shutdown (`+$10.00` / `-$10.00`), and fix `SIZING_MODE=fixed` ledger accounting.
3. **Investigate Signal Quality Sub-Buckets (High-Leverage Edge Audit):** Before scaling capital, inspect why **`0.91` entry prices (`85.71%` WR, `-$16.51`)** and **`13:30–17:30 IST` early hours (`88.37%` WR, `-$6.31`)** underperformed `0.89` (`97.37%` WR, `+$35.30`) and `17:30–01:00 IST` (`92.8%` WR, `+$26.51`). Tightening `INITIAL_PROB_MAX` from `0.91` to `0.90` or `0.895` could materially raise true EV/trade and lower breakeven risk.

---

## 11. Addendum: Multi-Week Cross-Validation (Live Real-Money + Early Paper + Paper 1) & Enhanced Strategy Blueprint

To test the day-of-week hypothesis (`Fri/Sat/Sun/Mon` green vs `Tue/Wed/Thu` red) and eliminate 5-day overfitting, all `INITIAL` trades (`463` unique slugs after deduplicating `8` overlapping slugs in favor of live fills) were audited across **all three historical databases** on EC2 (normalized to `$5.00` flat stakes per trade):
- **Early Paper (`paper_ledger.db.bak.1794252891`, Nov 6–10):** `149` INITIAL trades (`87.25%` WR, `-$18.74` norm `$5`)
- **Live Real Money (`live_ledger.db`, Nov 9–12):** `84` INITIAL trades (`91.67%` WR, `+$10.29` norm `$5`; `+$4.16` actual INITIAL PnL; `0.00000` fill slippage vs `trigger_prob`, with `56` unlatched FAK rejections logged cleanly)
- **Paper 1 (`paper1_ledger.db`, Nov 20–26):** `238` INITIAL trades (`91.18%` WR, `+$20.20`)

### 11.1 Day-of-Week & Intraday Timing Verification (`N = 463`)

| Hypothesis Tested | Early Paper (`Nov 6–10`) | Live Real Money (`Nov 9–12`) | Paper 1 (`Nov 20–26`) | Pooled (`N = 463`) Verdict |
| :--- | :--- | :--- | :--- | :--- |
| **`Fri+Sat+Sun+Mon` vs `Tue+Wed+Thu`** | Sat (`Nov 7`) lost **`-$26.32`** (`80.0%` WR); Sun `-$1.05` | Tue `+$6.27` (`93.1%`), Wed `+$5.41` (`92.1%`) were **best days** | Sat `+$22.50`, Tue `+$7.62`, Wed `-$13.61` | **REFUTED** (`Fri–Mon`: `89.93%` WR, `+$6.00` vs `Tue–Thu`: `90.29%` WR, `+$6.83`; Tue overall is best at `92.96%` WR, `+$13.90`). Do **not** skip Tue/Wed/Thu. |
| **Skip Early Hours (`13:30–17:30 IST`)** | Lost `-$6.79` | **Best window** (`93.10%` WR, `+$6.03`) | Lost `-$6.31` | **REFUTED** (dropping `13:30–17:30` reduced pooled `Price <= 0.90` profit from `+$31.06` to `+$7.26`). Keep `13:30–01:00 IST`. |

### 11.2 Two Structural Filters Replicated Across ALL Three Datasets

Unlike day-of-week or afternoon filtering, **two order-book / payoff rules improved performance independently in Early Paper, Live Real Money, AND Paper 1**:

1. **Cap Initial Entry at `0.90` (`INITIAL_PROB_MAX = 0.90`, `PRICE_CEILING = 0.900`):**
   - Pooled `avg_price == 0.91` (`N = 115`): **`87.83%` WR, `-$19.83`** (lost in both Live `-$3.48` and Paper 1 `-$16.51`), because paying `91¢` nets only `+$0.49` and requires `91.1%` win rate just to break even.
2. **Reject Wide Opposite-Book Spreads (`counterpart_prob <= 0.13`):**
   - When `trigger_prob` is `0.89–0.90` and the opposing best ask (`counterpart_prob`) is `>= 0.14` (order-book ask sum stretched to `1.03–1.14`, indicating fast BTC whipsaw / widened market-maker quotes), pooled performance (`N = 31`) crashed to **`77.42%` WR (`24W / 7L`, `-$20.65`)** — losing across **Early Paper (`57.1%`, `-$12.62`)**, **Live Real Money (`75.0%`, `-$3.08`)**, and **Paper 1 (`85.0%`, `-$4.95`)**.

### 11.3 Pooled 3-Week Strategy Comparison (`14` Sessions, `N = 463` Baseline → `242–292` Filtered)

| Configuration (`$5.00` Flat Norm) | Trades (`WR%`) | Green / Red Sessions | Total P&L (`14` Sess.) | Multi-Day Max DD | Live Money (`Nov 9–12`) | Early Paper (`Nov 6–10`) | Paper 1 Full (`Nov 21–25`) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Current Baseline (`0.89–0.91`, No Stops)** | `463` (`90.1%`) | `9 / 5` | `+$12.83` | `-$27.37` | `+$10.29` | `-$18.74` | `+$17.23` (`3W/2L`) |
| **Step 1: `Price <= 0.90` (Drop `0.91`)** | `318` (`91.5%`) | `9 / 5` | `+$42.24` | `-$13.11` | `+$13.78` | `-$3.81` | `+$30.30` (`3W/2L`) |
| **Step 2: `Price <= 0.90 & CP <= 0.13`** | `292` (`93.2%`) | `11 / 3` | **`+$65.41`** | **`-$2.13`** | **`+$16.86`** (`94.5%`) | **`+$9.78`** | **`+$37.48`** (`4W/1L`) |
| **Step 2 + `Trail(Arm +$6, Drop $5)` + `LL -$10`** | `262` (`95.0%`) | **`13 / 1`** *(only partial `Nov 26`)* | **`+$87.15`** | **`-$1.32`** | `+$15.07` | **`+$24.54`** (`4W/0L`) | **`+$44.99`** (`5W/0L`!) |
| **Step 2 + `PT +$10 / LL -$10`** | `242` (`96.3%`) | **`12 / 2`** *(max loss `-$1.32`!)* | **`+$96.55`** | **`-$2.13`** | `+$19.45` | **`+$36.84`** (`4W/0L`) | **`+$37.69`** (`4W/1L`) |

- **Statistical Validation of Step 2 (`Price <= 0.90 & CP <= 0.13`, `N = 292`, `272W / 20L`):**
  - Win Rate **`93.15%`** (95% Wilson CI: **`[89.66%, 95.52%]`**), exceeding the lowered **`89.16%` breakeven win rate** (`AvgWin = +$0.6084` vs `AvgLoss = -$5.0043`).
  - Expected Value = **`+$0.2240 / trade`** (`SE = $0.0832`, **`t = 2.69`**, $p < 0.01$ — statistically significant across 3 independent weeks).

### 11.4 Future Implementation Blueprint (Deferred Until User Approval)

1. `INITIAL_PROB_MIN=0.88` (or `0.89`), `INITIAL_PROB_MAX=0.90`, `PRICE_CEILING=0.900`.
2. New Initial-Entry Spread/Counterpart Guard: `MAX_INITIAL_COUNTERPART_PROB=0.13` (skip entry if opposite best ask `> 0.13`).
3. Session-Keyed Dual Shutdown (`13:30 IST` boundary): `SESSION_MAX_LOSS_USD=10.00` AND either **`SESSION_PROFIT_TARGET_USD=10.00`** OR **`Trailing Lock` (`Arm +$6.00`, `Stop Drop $5.00`)**.
4. Capital Alignment: Use `$50–$60` bankroll per `$5.00` flat stake, and bypass `$44` Protected Capital lock when `SIZING_MODE=fixed`.

