# AWS Ireland (`eu-west-1`) Unified Operations Guide (Live & Paper)

> **Root AWS Account:** `kazi69041@gmail.com`  
> **Primary Region:** Europe (Ireland) `eu-west-1`  
> **Consolidated Host:** Single EC2 Ubuntu 24.04 instance running both Live and Paper engines under PM2.

---

## 1. Unified Production Instance Reference

| Parameter | Live Trading Engine | Paper Trading Engine |
| :--- | :--- | :--- |
| **Instance Public IPv4** | **`3.252.144.120`** | **`3.252.144.120`** |
| **AWS Region** | Europe (Ireland) `eu-west-1` | Europe (Ireland) `eu-west-1` |
| **Operating System** | Ubuntu Server 24.04 LTS (HVM) | Ubuntu Server 24.04 LTS (HVM) |
| **SSH User** | `ubuntu` | `ubuntu` |
| **SSH Key Path (Local)** | `C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem` | `C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem` |
| **Remote Project Path** | `/home/ubuntu/polymarket-btc-5-minute` | `/home/ubuntu/polymarket-btc-5-minute` |
| **GitHub Repository** | `https://github.com/alhaannn/polymarket-btc-5-minute.git` | `https://github.com/alhaannn/polymarket-btc-5-minute.git` |
| **Active Branch** | `feature/new-5m-strategy` | `feature/new-5m-strategy` |
| **PM2 Process Name** | **`polybot-live`** (ID: 0) | **`polybot-paper`** (ID: 1) |
| **Dashboard Port** | **`8080`** | **`8081`** |
| **Direct Web URL** | 👉 **[http://3.252.144.120:8080](http://3.252.144.120:8080)** | 👉 **[http://3.252.144.120:8081](http://3.252.144.120:8081)** |
| **Execution Mode** | `EXECUTION_MODE=live` (CLOB real orders) | `EXECUTION_MODE=paper` (Simulated taker) |
| **Bot Status** | **`PAUSED`** (Awaiting deposit to activate) | **`RUNNING`** (Continuous benchmark) |
| **Ledger Database** | `/data/live_ledger.db` (Clean, $11 seed) | `/data/paper_ledger.db` (Migrated, **$38.00+** equity) |
| **Polygon Funder Address** | `0x5923CFc24AA6f876f27bc9257890126a8c3E72B6` | Local virtual balance |
| **Polygon Signer Address** | `0x284f018FfF5B0Ec392440693eB30d13ae5f2604F` | N/A |
| **Signature Type** | `POLY_SIG_TYPE=2` (Proxy/Gnosis Safe) | N/A |
| **Virginia EC2 (`54.226.232.16`)** | **TERMINATED** (Deleted from AWS console to eliminate double-billing) |

---

## 2. Why Consolidated on AWS Ireland (`eu-west-1`)?

1. **Polymarket Compliance & Geoblock Bypass:** Polymarket strictly blocks US IP addresses (`blocked: true`, HTTP 403). Ireland (`eu-west-1`) is fully authorized (`blocked: false`) with direct CLOB access.
2. **Native Binance Price Feeds:** Ireland connects directly to Binance real-time WebSocket feeds with zero regional restrictions (HTTP 200).
3. **Ultra-Low Latency:** Direct fiber interconnect from Dublin to Polymarket's London matching engine (`eu-west-2`) yields sub-5ms round-trip latency.
4. **Cost Elimination:** All workloads (Live + Paper) execute on a single t3/t4g micro instance, cutting AWS infrastructure spend in half.

---

## 3. Concurrency & Rate Limit Safety

Running two bot processes on one public IP is well within all exchange and API rate limits:

| Feed / Service | Combined Ireland Consumption | Provider Rate Limit | Safety Margin |
| :--- | :--- | :--- | :--- |
| **Polymarket CLOB WebSocket** | 4 token subscriptions (2 Live + 2 Paper) | 500 tokens / connection | **>99% safe** |
| **Binance Price WebSocket** | 2 WS client connections | 300 connections / IP | **>99% safe** |
| **Polymarket REST Polling** | ~1 request / 5 min (Market discovery) | 60 requests / minute | **<2% utilized** |
| **CLOB Order Submission** | 0 from Paper (simulated in RAM) | 100 requests / sec | **Completely isolated** |
| **Database Concurrency** | Dedicated files: `live_ledger.db` vs `paper_ledger.db` in WAL mode | N/A | **Zero lock contention** |

---

## 4. Web Dashboard & 4-Stage Execution Progress Bar

Both dashboards (`:8080` and `:8081`) feature the real-time **4-Stage Execution Progress Bar**:

- **Stage 1 (0s – 180s): Observation Gate** (Muted gray) — Bot streams live prices, order book depth, and calculates probabilities without placing orders.
- **Stage 2 (180s – 210s): Initial Snipe Window** (Emerald green pulse) — 30-second prime entry window. Fires $1.00 canonical snipe order if probability reaches $\ge 89\%$.
- **Stage 3 (210s – 280s): Reversal Recovery Window** (Amber) — 10x hedge window. If BTC momentum reverses, fires a $10.00 recovery hedge on the opposite side.
- **Stage 4 (280s – 300s): Pre-settlement Lockout** (Slate gray) — Trading gates close. No orders permitted; waits for final Polymarket settlement.

---

## 5. Standard Deployment Workflow (Local $\to$ GitHub $\to$ Ireland)

Whenever code changes are made on your local machine, follow this standard deployment sequence:

### Step 5.1: Run Local Validation Tests
```powershell
python -m pytest -q
```

### Step 5.2: Commit & Push to GitHub
In Windows PowerShell (use `;` to chain commands):
```powershell
git add -A; git commit -m "feat/fix: summary of changes"; git push origin feature/new-5m-strategy
```

### Step 5.3: Pull on Ireland Server & Restart
Pull the latest code on Ireland and restart both engines under PM2:
```powershell
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "cd /home/ubuntu/polymarket-btc-5-minute && git pull origin feature/new-5m-strategy && pm2 restart all"
```

---

## 6. PM2 Process Management & CLI Commands

### 6.1 Check Process Table
```powershell
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "pm2 status"
```

### 6.2 Stream or View Logs
```powershell
# View last 40 lines of paper bot (Port 8081)
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "pm2 logs polybot-paper --lines 40 --nostream"

# View last 40 lines of live bot (Port 8080)
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "pm2 logs polybot-live --lines 40 --nostream"

# Real-time streaming logs (Ctrl+C to exit)
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "pm2 logs"
```

### 6.3 Restart Individual Processes
```powershell
# Restart paper bot only
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "pm2 restart polybot-paper"

# Restart live bot only
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "pm2 restart polybot-live"
```

### 6.4 Pause / Resume Control via REST API
```powershell
# Pause Live Trading immediately
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "curl -s -X POST http://localhost:8080/api/control -H 'Content-Type: application/json' -d '{\"action\":\"PAUSE\"}'"

# Resume / Run Live Trading
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "curl -s -X POST http://localhost:8080/api/control -H 'Content-Type: application/json' -d '{\"action\":\"START\"}'"

# Resume Paper Trading
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "curl -s -X POST http://localhost:8081/api/control -H 'Content-Type: application/json' -d '{\"action\":\"START\"}'"
```

### 6.5 Persist PM2 Across System Reboots
Whenever the PM2 process list is updated, save the configuration to systemd:
```powershell
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "pm2 save"
```

---

## 7. Direct Telemetry & REST API Verification Endpoints

You can probe either port (`8080` for Live, `8081` for Paper) directly:

| Endpoint | Description | Sample Command (Live `:8080`) |
| :--- | :--- | :--- |
| **`/api/state`** | Current phase, elapsed seconds, prices, probabilities, bot status | `curl -s http://3.252.144.120:8080/api/state` |
| **`/api/capital`** | Total equity, active capital, PnL, reconciliation status | `curl -s http://3.252.144.120:8080/api/capital` |
| **`/api/performance`** | Win rate, trade counts, profit/loss breakdown in IST | `curl -s http://3.252.144.120:8080/api/performance` |
| **`/api/trades`** | Complete list of recent settled and active trades | `curl -s http://3.252.144.120:8080/api/trades` |

---

## 8. SSH Port Forwarding Tunnel (Fallback)

If direct browser access to port 8080 or 8081 is ever blocked by a local network, firewall, or ISP:

```powershell
# Tunnel Live Bot to http://localhost:8080
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" -N -L 8080:127.0.0.1:8080 ubuntu@3.252.144.120

# Tunnel Paper Bot to http://localhost:8081
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" -N -L 8081:127.0.0.1:8081 ubuntu@3.252.144.120
```

---

## 9. Live Money Activation Checklist

### 9.1 Deposit Funds
Send collateral to your Polymarket Polygon (PoS) address:
- **Wallet Address:** `0x5923CFc24AA6f876f27bc9257890126a8c3E72B6`
- **Asset:** **USDC.e** (Bridged) or Native **USDC**
- **Recommended Deposit:** **$15.00 – $25.00**
- **Gas Reserve:** ~0.2 POL (MATIC)
- **Token Approvals:** Active (wallet previously traded on Polymarket)

### 9.2 Unpause Live Trading
- **Browser:** Open **[http://3.252.144.120:8080](http://3.252.144.120:8080)** and click the green **`▶ Run`** button.
- **Status Check:** Verify status flips to `RUNNING`.

---

## 10. Ledger Reset Procedures

### 10.1 Reset Live Ledger (Clean Slate)
If you want to clear test data and start live trading with an exact fresh balance:
```powershell
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "cd /home/ubuntu/polymarket-btc-5-minute && pm2 stop polybot-live && rm -f data/live_ledger.db && pm2 restart polybot-live"
```

### 10.2 Reset Paper Ledger
If you ever want to reset the paper trading benchmark back to a clean $11.00 starting state:
```powershell
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@3.252.144.120 "cd /home/ubuntu/polymarket-btc-5-minute && pm2 stop polybot-paper && python3 -m polybot.reset && pm2 restart polybot-paper"
```

---

## 11. Important PowerShell & Environment Gotchas

1. **PowerShell Statement Chaining:**
   - Windows PowerShell 5.1 does not recognize `&&`. Use `;` to chain commands locally:
     `git add -A; git commit -m "..."; git push origin ...`
   - Inside double quotes for remote SSH commands (`ssh ... "cd ... && pm2 restart ..."`), `&&` is parsed by Linux bash and works properly.
2. **Line Endings (`\r\n` vs `\n`):**
   - Avoid piping multiline PowerShell strings into SSH bash (`$cmd | ssh ... bash -s`) unless CRLF characters are explicitly stripped. Passing quoted bash commands directly avoids this.
3. **Database Locks:**
   - Always stop PM2 processes before moving or copying `.db` files to allow SQLite to flush WAL journals cleanly.
