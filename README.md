# Polybot: Systematic Trading & Data System for Polymarket BTC 5-Minute Markets

A production-grade, testable quantitative trading system for Polymarket's **BTC Up or Down 5-minute** binary prediction markets.

---

## 1. System Architecture

```text
polymarketbot/
├── polybot/
│   ├── config.py              # Strongly-typed dataclasses + YAML + env overrides
│   ├── gamma.py               # Market discovery, settlement polling, and CLOB REST fallback
│   ├── recorder.py            # 24/7 lossless JSONL recorder with background thread batching
│   ├── model.py               # Brownian probability P = Φ(z), dual EWMA volatility, Platt scaling
│   ├── filters.py             # Pre-trade guards: adverse drift, vol spikes, liquidity, feed staleness
│   ├── strategy.py            # Shared execution engine: LegacyStrategy & EdgeStrategy
│   ├── monitor.py             # Telegram alerts, heartbeats, and circuit breaker notifications
│   ├── risk.py                # Daily drawdown limit, cooldown pauses, and 'STOP' file kill switch
│   ├── sizing.py              # Fixed stake vs. Fractional Kelly criterion with hard caps
│   ├── execution/
│   │   ├── base.py            # Orders, Fills, and Position interfaces
│   │   ├── fees.py            # Maker / taker fee models
│   │   ├── paper.py           # Simulated 400ms latency queue, book walking, and maker limits
│   │   └── live.py            # Double-gated micro-live module with startup reconciliation
│   └── backtest/
│       ├── loader.py          # Dual loader for polybot JSONL and polyticker archives
│       ├── engine.py          # Event-driven historical replayer (using live strategy code)
│       ├── report.py          # Quantitative analytics, Wilson CI, breakdown tables, Brier score
│       └── sweep.py           # Parameter grid search with 70/30 hold-out train/test split
├── deploy/                    # systemd service unit files for Ubuntu VPS
├── tests/                     # 100% offline unit tests with recorded fixtures
├── main.py                    # Unified CLI (recorder, paper, live)
├── backtest.py                # Standalone backtester CLI
└── install.md                 # Step-by-step VPS deployment guide
```

---

## 2. Core Operational Modes

### 2.1 24/7 Data Recorder (First Step)
Collect real market data for 1–2 weeks before deploying capital.
```bash
python main.py recorder

# Custom raw storage directory:
python main.py recorder --data-dir data/raw
```
Saves to `data/raw/{window_start}/`:
- `chainlink.jsonl`: Real-time Chainlink oracle ticks + gap markers.
- `binance.jsonl`: Binance spot trades for cross-check divergence tracking.
- `market.jsonl`: CLOB order book snapshots and delta price changes.
- `metadata.json`: Window slug, token IDs, strike $K$, timestamps.
- `resolution.json`: Verified settlement outcome.

Old windows are automatically rotated into `{window_start}.tar.gz` archives and cataloged in `manifest.json`.

---

### 2.2 Historical Backtesting & Parameter Sweeps
Replays recorded ticks through the exact same strategy and simulated execution queue.
```bash
# Backtest Edge Strategy on recorded data
python backtest.py --strategy edge --data data/raw

# A/B compare against the Legacy 0.93-0.96 late-entry strategy
python backtest.py --strategy legacy --data data/raw

# Run Parameter Sweep with 70/30 Hold-Out Split (prevents curve-fitting)
python backtest.py --sweep --data data/raw
```

---

### 2.3 Paper Trading Bot
Executes live simulated trades walking the real CLOB order book with simulated **400ms latency**.
```bash
# Probabilistic Edge Strategy
python main.py paper --strategy edge --stake 5.0

# Legacy Strategy
python main.py paper --strategy legacy
```

---

### 2.4 Gated Live Trading (Locked by Default)
Strictly double-gated behind both a CLI flag and environment variable:
```bash
# Will be denied without confirmation:
python main.py live

# Unlocked execution:
LIVE_CONFIRM=YES_I_UNDERSTAND python main.py live --live
```
- **Micro-Live Protection**: Hard-caps stake size at **$1.00** by default.
- **Fail-Closed Startup**: Reconciles position tokens against local ledger before placing orders.

---

## 3. Mathematical Edge & Break-Even Analysis

### Break-Even Win Rate Calculation
In binary prediction markets paying $1.00 on win and $0.00 on loss:
$$\text{Break-Even Win Rate} = \frac{\text{Average Fill Price} + \text{Fees}}{1.00}$$

| Entry Ask | Net Cost (with 0.1% fee) | Required Win Rate |
| :---: | :---: | :---: |
| 0.850 | $0.851 | **85.1%** |
| 0.900 | $0.901 | **90.1%** |
| 0.930 | $0.931 | **93.1%** |
| 0.960 | $0.961 | **96.1%** |
| 0.985 | $0.986 | **98.6%** |

### Probabilistic Model
- **Brownian Approximation**:
  $$z = \frac{S_{\text{now}} - K}{\sigma_{\text{per\_sec}} \cdot S_{\text{now}} \cdot \sqrt{t_{\text{left}}}}, \quad P = \Phi(z)$$
- **Tie-Breaker Rule**: Polymarket resolves to `Up` if $S_{\text{end}} \ge K$.
- **Trade Gate**:
  $$\text{Edge} = P_{\text{calibrated}} - \text{Ask} - \text{Fee} \ge \text{MIN\_EDGE} \quad (\text{default } 0.02)$$

---

## 4. Configuration & Environment Variables

| Variable | Default | Description |
|---|---|---|
| `STRATEGY` | `edge` | Strategy engine: `edge` or `legacy` |
| `STAKE_USD` | `5.0` | Dollars staked per trade |
| `PRICE_MIN` / `PRICE_MAX` | `0.85` / `0.985` | Allowed entry price band |
| `LEGACY_PRICE_MIN` / `LEGACY_PRICE_MAX` | `0.93` / `0.96` | Band for legacy strategy |
| `ENTRY_START_SEC` / `ENTRY_END_SEC` | `120` / `285` | Window time span to evaluate entries |
| `MIN_EDGE` | `0.02` | Minimum required edge above fill price + fees |
| `Z_MIN` | `1.0` | Minimum lead in standard deviations |
| `MIN_LEAD_USD` | `5.0` | Minimum lead in raw dollars |
| `ADVERSE_DRIFT_MAX_PCT` | `0.50` | Max lead erosion over 30s before skipping trade |
| `LATENCY_MS` | `400` | Simulated paper execution latency buffer |
| `DAILY_LOSS_LIMIT_USD` | `25.0` | Daily circuit breaker loss cap |
| `MAX_TRADES_PER_DAY` | `50` | Maximum allowed trades per calendar day |
| `PAUSE_CONSECUTIVE_LOSSES` | `3` | Pause trading after $N$ consecutive losses |
| `KILL_SWITCH_FILE` | `STOP` | File name that halts trading instantly |
| `TELEGRAM_BOT_TOKEN` | *empty* | Telegram Bot token for trade & crash alerts |
| `TELEGRAM_CHAT_ID` | *empty* | Telegram destination Chat ID |

---

## 5. Risk Controls & Emergency Kill Switch

- **Instant Kill Switch**: Creating a file named `STOP` in the root folder immediately halts all new order submissions while settling any existing positions:
  ```bash
  touch STOP
  ```
- **Feed Staleness Circuit Breaker**: If the Chainlink feed has no new ticks for $> 3.0$ seconds, trading automatically halts.
- **Divergence Guard**: If Chainlink and Binance spot prices diverge by $> 0.5\%$, entries are aborted.

---

## 6. Testing

The codebase includes an offline unit test suite with 48 tests:
```bash
python -m pytest -v
```
All tests run with mocked fixtures and recorded network payloads. Zero external network calls are made during tests.
