# AWS Ireland (`eu-west-1`) Unified Bot Operations Guide (Live & Paper)

This guide documents the single-server consolidated architecture for the **Polymarket 5-Minute BTC Bot** hosted on **AWS Ireland (`eu-west-1`)**. Both **Live Real-Money Trading** and **Paper Trading Simulation** run concurrently under PM2 on independent ports with dedicated databases.

---

## 1. Unified Production Instance Reference

| Parameter | Live Trading Process | Paper Trading Process |
| :--- | :--- | :--- |
| **Instance Public IPv4** | **`3.252.144.120`** | **`3.252.144.120`** |
| **AWS Region** | Europe (Ireland) `eu-west-1` | Europe (Ireland) `eu-west-1` |
| **Operating System** | Ubuntu Server 24.04 LTS (HVM) | Ubuntu Server 24.04 LTS (HVM) |
| **PM2 Process Name** | **`polybot-live`** (ID: 0) | **`polybot-paper`** (ID: 1) |
| **Web Dashboard URL** | 👉 **[http://3.252.144.120:8080](http://3.252.144.120:8080)** | 👉 **[http://3.252.144.120:8081](http://3.252.144.120:8081)** |
| **Port** | `8080` | `8081` |
| **Execution Mode** | `EXECUTION_MODE=live` (CLOB real orders) | `EXECUTION_MODE=paper` (Simulated taker) |
| **Bot Status** | **`PAUSED`** (Awaiting wallet deposit) | **`RUNNING`** (Continuous benchmark) |
| **Ledger Database** | `/data/live_ledger.db` (Clean, $11 seed) | `/data/paper_ledger.db` (Migrated, **$37.63** equity) |
| **SSH Key Path (Local)** | `C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem` | `C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem` |
| **Repository Path** | `/home/ubuntu/polymarket-btc-5-minute` | `/home/ubuntu/polymarket-btc-5-minute` |
| **Branch** | `feature/new-5m-strategy` | `feature/new-5m-strategy` |
| **Virginia EC2 (`54.226.232.16`)** | **DECOMMISSIONED** (All paper data transferred to Ireland; Virginia can be terminated) |

---

## 2. Why Ireland (`eu-west-1`) for Both Processes?

1. **Zero Geoblocking:** Polymarket CLOB permits Ireland (`eu-west-1`), while Virginia (`us-east-1`) returns HTTP 403.
2. **Binance Direct Feed:** Ireland accesses native Binance public feeds without geo-restrictions.
3. **Ultra-Low Latency:** Direct fiber to Polymarket matching engine in London (`eu-west-2`) with sub-5ms round-trip latency.
4. **Cost Optimization:** Consolidated from two EC2 instances down to **one single instance**, eliminating duplicate AWS monthly server costs.

---

## 3. Concurrency & Rate Limit Safeguards

Both bots operate simultaneously on the single Ireland IP without risk of rate limits:

| Feed / Service | Combined Ireland Consumption | Provider Limit | Headroom / Safety Margin |
| :--- | :--- | :--- | :--- |
| **Polymarket CLOB WS** | 4 token subscriptions (2 live + 2 paper) | 500 tokens / connection | **>99% safety margin** |
| **Binance Price WS** | 2 WebSocket connections | 300 connections / IP | **>99% safety margin** |
| **Polymarket REST Polling** | ~1 request / 5 minutes (Gamma Discovery) | 60 requests / minute | **<2% of limit** |
| **CLOB API Rate Limits** | 0 orders from paper (RAM only) | 100 requests / second | **Completely unaffected** |
| **Database Locks** | `live_ledger.db` and `paper_ledger.db` are separate SQLite files in WAL mode | N/A | **Zero contention** |

---

## 4. Web Dashboard & 4-Stage Execution Bar

Both dashboards (`:8080` and `:8081`) feature the upgraded **4-Stage Execution Progress Bar**:

- **Stage 1 (0s – 180s): Observation Gate** (Muted gray) — Market price and order book depth tracking.
- **Stage 2 (180s – 210s): Initial Snipe** (Bright green pulse) — 30-second prime probability entry window ($1.00 canonical stake).
- **Stage 3 (210s – 280s): Reversal Recovery** (Amber) — 10x reversal hedge window ($10.00 stake) if BTC price swings.
- **Stage 4 (280s – 300s): Pre-settlement Lockout** (Slate gray) — No new orders permitted before final expiry.

---

## 5. Live Money Activation Checklist

### 5.1 Funding the Live Wallet
Deposit USDC into your Polymarket wallet on **Polygon (PoS)**:
- **Wallet Address:** `0x5923CFc24AA6f876f27bc9257890126a8c3E72B6`
- **Asset:** **USDC.e** (Bridged) or native **USDC**
- **Recommended Deposit:** **$15.00 – $25.00**
- **Gas Reserve:** 0.2 POL (MATIC) for transaction gas
- **Pre-Approved:** Wallet previously traded on Polymarket; token approvals are active.

### 5.2 Unpausing Live Trading
Once funds arrive:
1. Open **[http://3.252.144.120:8080](http://3.252.144.120:8080)**.
2. Click **`▶ Run`** in the top navigation bar.
3. The status flips to **`RUNNING`** and will begin evaluating live markets.

---

## 6. PM2 Operational Commands

### 6.1 Check Bot Status
```bash
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "pm2 status"
```

### 6.2 View Real-Time Logs
```bash
# Paper trading logs (Port 8081)
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "pm2 logs polybot-paper"

# Live trading logs (Port 8080)
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "pm2 logs polybot-live"
```

### 6.3 Restart or Pause
```bash
# Restart paper bot
pm2 restart polybot-paper

# Restart live bot
pm2 restart polybot-live

# Pause live bot from CLI
curl -s -X POST http://localhost:8080/api/control -H 'Content-Type: application/json' -d '{"action":"PAUSE"}'

# Resume paper bot from CLI
curl -s -X POST http://localhost:8081/api/control -H 'Content-Type: application/json' -d '{"action":"START"}'
```

### 6.4 Save Process State to Systemd
Whenever PM2 process list is updated:
```bash
pm2 save
```

---

## 7. Virginia EC2 Decommissioning Procedure

Since the paper database and all 76+ trades ($37.63 total equity) have been safely migrated and verified running on Ireland port 8081:

1. Log into your **AWS Management Console**.
2. Navigate to **EC2** $\to$ **Instances** in region **`us-east-1` (N. Virginia)**.
3. Locate instance `54.226.232.16` (named `polymarket-bot` or similar).
4. Select **Instance state** $\to$ **Terminate instance**.
5. All monthly costs for Virginia EC2 are now permanently eliminated.
