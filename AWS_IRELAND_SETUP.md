# AWS Ireland (`eu-west-1`) Live Trading Deployment & Operations Guide

This guide provides end-to-end instructions for launching, provisioning, configuring, and operating the **Polymarket 5-Minute BTC Bot** in **AWS Ireland (`eu-west-1`)** for **LIVE real-money trading**.

---

## 1. Live Production Instance Reference

| Parameter | Current Live Production Value |
| :--- | :--- |
| **Instance Public IPv4** | **`3.252.144.120`** |
| **AWS Region** | Europe (Ireland) `eu-west-1` |
| **Operating System** | Ubuntu Server 24.04 LTS (HVM) |
| **PM2 Process Name** | **`polybot-live`** (PID 12436, auto-reboot via systemd) |
| **Execution Mode** | **`EXECUTION_MODE=live`** (Double-gate unlocked with `LIVE_CONFIRM=YES_I_UNDERSTAND`) |
| **Current Bot Status** | **`PAUSED`** (Awaiting wallet funding; zero orders will fire) |
| **Direct Web Dashboard** | 👉 **[http://3.252.144.120:8080](http://3.252.144.120:8080)** |
| **SSH Key Path (Local)** | `C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem` |
| **Repository Path** | `/home/ubuntu/polymarket-btc-5-minute` (Branch: `feature/new-5m-strategy`) |
| **Live Ledger Database** | `/home/ubuntu/polymarket-btc-5-minute/data/live_ledger.db` |
| **USDC Wallet Address** | `0x5923CFc24AA6f876f27bc9257890126a8c3E72B6` (Signature Type 2) |
| **Old Virginia Instance** | `54.226.232.16` (`polybot-paper`, retained purely for paper trading) |

---

## 2. Why AWS Ireland (`eu-west-1`)?

| Metric | AWS Virginia (`us-east-1`) | AWS Ireland (`eu-west-1`) |
| :--- | :--- | :--- |
| **Polymarket Geoblock** | ❌ **BLOCKED (`blocked: true`)** | ✅ **PERMITTED (`blocked: false`)** |
| **Order Execution** | ❌ HTTP 403 Forbidden | ✅ Full CLOB Taker/Maker execution |
| **Latency to Polymarket CLOB** | ~80ms–110ms | ⚡ **Sub-2ms to 5ms** (Direct fiber to London `eu-west-2`) |
| **Binance WebSocket Feed** | ❌ HTTP 451 Geo-Restricted | ✅ Native sub-second price ticks (`HTTP 200`) |

---

## 3. Quick Connect & Verification Commands

### 3.1 SSH into Ireland Instance
From local Windows PowerShell:
```powershell
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120
```

### 3.2 Verify Compliance & Feed Status
```bash
# 1. Verify Binance WebSocket price ticks
curl -sI https://api.binance.com/api/v3/ping
# Expected: HTTP/2 200

# 2. Verify Polymarket CLOB Order Book access
curl -s "https://clob.polymarket.com/markets" | head -c 100
# Expected: Valid market metadata JSON
```

### 3.3 Verify Live Dashboard & Process Status
```bash
# Check PM2 process table
pm2 status

# Check last 30 log lines
pm2 logs polybot-live --lines 30 --nostream

# Query live bot telemetry endpoint
curl -s http://localhost:8080/api/state
```

---

## 4. Live Money Trading Activation (Funding & Unpausing)

### 4.1 Wallet Funding
Deposit starting collateral into your Polymarket wallet address on the **Polygon (PoS)** network:
- **Wallet Address:** `0x5923CFc24AA6f876f27bc9257890126a8c3E72B6`
- **Asset:** **USDC.e** (Bridged USDC on Polygon) or Native USDC
- **Recommended Deposit:** **$15.00 to $25.00** (Covers initial $1.00 stake, 10x reversal stake, and daily loss buffer)
- **Gas (POL / MATIC):** ~0.2 POL for token transfer/approval transactions
- **Prior Trading Note:** Since this wallet has already traded on Polymarket in the past, token approvals for Polymarket CTF Exchange contracts are already established.

### 4.2 How to Unpause the Bot
Once the funds arrive in your wallet:

#### Option A: One-Click from Browser (Recommended)
1. Open **[http://3.252.144.120:8080](http://3.252.144.120:8080)** in your browser.
2. Click the **`▶ Run`** button in the top-right header controls.
3. The bot status pill will immediately switch from `PAUSED` (amber) to `RUNNING` (green).

#### Option B: Terminal Command
```bash
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "cd /home/ubuntu/polymarket-btc-5-minute && sed -i 's/^PAUSED=.*/PAUSED=false/' .env && pm2 restart polybot-live --update-env"
```

---

## 5. Live Architecture & Sizing Safeguards

The bot operates under strict canonical sizing and risk limits configured in `/home/ubuntu/polymarket-btc-5-minute/.env`:

```ini
# =====================================================================
# POLYMARKET 5-MINUTE LIVE PRODUCTION CONFIGURATION (IRELAND)
# =====================================================================

# 1. Mandatory Safety Double-Gate
LIVE_CONFIRM=YES_I_UNDERSTAND
EXECUTION_MODE=live
PAUSED=true

# 2. Polygon Wallet Configuration
POLY_PRIVATE_KEY=0x...
POLY_SIGNER_ADDRESS=0x284f018FfF5B0Ec392440693eB30d13ae5f2604F
POLY_FUNDER_ADDRESS=0x5923CFc24AA6f876f27bc9257890126a8c3E72B6
POLY_SIG_TYPE=2
POLYGON_RPC_URL=https://polygon-bor-rpc.publicnode.com

# 3. Canonical Sizing ($1 Initial / 10x Reversal Scaling)
SIZING_MODE=canonical
INITIAL_STAKE_USD=1.00
REVERSAL_STAKE_USD=10.00
REVERSAL_MULTIPLIER=10.0
MICRO_STAKE_CAP_USD=10.00

# 4. Core Sniper & Reversal Thresholds
PRIME_HOURS_ONLY=true
ENABLE_REVERSALS=true
INITIAL_PROB_MIN=0.89
INITIAL_PROB_MAX=0.91
PRICE_CEILING=0.910
REVERSAL_PRICE_CEILING=0.930
MAX_SLIPPAGE=0.035
REVERSAL_PROB_MIN=0.85
REVERSAL_PROB_MAX=0.95
REVERSAL_WINDOW_START_SEC=180.0
INITIAL_SNIPE_END_SEC=210.0
REVERSAL_WINDOW_END_SEC=300.0
EARLY_EXIT=false

# 5. Risk Circuit Breakers
DAILY_LOSS_LIMIT_USD=6.00
DAILY_LOSS_LIMIT_PCT=0.30
DAILY_LOSS_MIN_USD=6.00
PAUSE_CONSECUTIVE_LOSSES=3
PAUSE_DURATION_WINDOWS=3
KILL_SWITCH_FILE=STOP

# 6. Dashboard Configuration
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8080
DASHBOARD_ENABLED=true
```

### Safety Features Built-in:
1. **Pre-Execution Balance Check:** Before posting each order to Polymarket CLOB, `LiveExecutionEngine` queries your on-chain collateral balance. If available balance < stake amount, the trade is rejected (fail-closed) rather than failing on-chain.
2. **Order Dispatched to CLOB:** Orders are signed with the local private key using Level 2 authentication and posted to `POST https://clob.polymarket.com/order`.
3. **Rejection Callback Unlatching:** If an order is rejected (e.g., due to price movement or insufficient funds), the strategy state unlatches immediately with a 1-second cooldown, preventing state locks.
4. **Hard Stake Cap:** Stakes are strictly capped at `$10.00` via `MICRO_STAKE_CAP_USD=10.00`.
5. **Daily Loss Breaker:** If cumulative daily losses exceed `$6.00`, trading halts automatically until the next trading day (Option B reserve protection).

---

## 6. Operations & Maintenance Playbook

### 6.1 View Live Logs in Real Time
```bash
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "pm2 logs polybot-live"
```

### 6.2 Pause Trading Immediately
- **From Browser:** Click **`⏸ Pause`** on the dashboard.
- **From CLI:**
  ```bash
  ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "curl -s -X POST http://localhost:8080/api/control -H 'Content-Type: application/json' -d '{\"action\":\"PAUSE\"}'"
  ```

### 6.3 Emergency Hard Stop (Kill Switch)
```bash
# Method 1: Drop STOP kill-switch file
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "touch /home/ubuntu/polymarket-btc-5-minute/STOP"

# Method 2: Stop PM2 process
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "pm2 stop polybot-live"
```

### 6.4 Pull Code Updates from GitHub
```bash
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "cd /home/ubuntu/polymarket-btc-5-minute && git pull origin feature/new-5m-strategy && pm2 restart polybot-live --update-env"
```

### 6.5 Reset Live Trade Logs & Ledger
If you want to clear historical logs and reset the database to a clean starting slate:
```bash
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "cd /home/ubuntu/polymarket-btc-5-minute && rm -f data/live_ledger.db && pm2 flush && pm2 restart polybot-live --update-env"
```

### 6.6 Systemd Reboot Persistence
PM2 is registered with systemd under `/etc/systemd/system/pm2-ubuntu.service`:
- Configured as `Type=oneshot` with `RemainAfterExit=yes`
- Auto-resurrects `polybot-live` from `/home/ubuntu/.pm2/dump.pm2` on system reboot.
- To re-freeze the process list after configuration changes:
  ```bash
  pm2 save
  ```

---

## 7. Dual Server Topology Summary

| Server | Region | IP | Function | Mode |
| :--- | :--- | :--- | :--- | :--- |
| **Ireland EC2** | `eu-west-1` | **`3.252.144.120`** | **LIVE REAL-MONEY TRADING** | `EXECUTION_MODE=live` (CLOB orders) |
| **Virginia EC2** | `us-east-1` | **`54.226.232.16`** | **PAPER TRADING BENCHMARK** | `EXECUTION_MODE=paper` (Simulation) |

Both servers run independently with zero cross-talk, keeping your live capital isolated while preserving your continuous paper-trading benchmark.
