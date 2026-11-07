# AWS Ireland (`eu-west-1`) Live Trading Deployment Guide

This guide provides end-to-end instructions for launching, provisioning, and operating the **Polymarket 5-Minute BTC Bot** in **AWS Ireland (`eu-west-1`)** for **LIVE real-money trading**.

---

## Why AWS Ireland (`eu-west-1`)?

| Metric | AWS Virginia (`us-east-1`) | AWS Ireland (`eu-west-1`) |
| :--- | :--- | :--- |
| **Polymarket Geoblock** | ❌ **BLOCKED (`blocked: true`)** | ✅ **PERMITTED (`blocked: false`)** |
| **Order Execution** | ❌ HTTP 403 Forbidden | ✅ Full CLOB Taker/Maker execution |
| **Latency to Polymarket CLOB** | ~80ms–110ms | ⚡ **Sub-2ms to 5ms** (Direct fiber to London `eu-west-2`) |
| **Binance WebSocket Feed** | ❌ HTTP 451 Geo-Restricted | ✅ Native sub-second price ticks |

---

## Phase 1: Provisioning the Ireland EC2 Instance

1. Log into your **AWS Management Console** (`https://console.aws.amazon.com`).
2. In the top-right navigation bar, click the region dropdown and select **Europe (Ireland) `eu-west-1`**.
3. Navigate to **EC2** $\rightarrow$ Click **Launch Instance**.
4. Configure the instance:
   * **Name**: `polymarket-bot-live-ireland`
   * **Application and OS Images (AMI)**: **Ubuntu Server 24.04 LTS (HVM)**, SSD Volume Type (64-bit x86).
   * **Instance Type**: 
     * `t3.small` (Recommended: 2 vCPUs, 2 GB RAM — optimal for async WebSocket concurrency).
     * `t3.micro` (Acceptable: 2 vCPUs, 1 GB RAM — Free Tier eligible).
   * **Key Pair**:
     * Click **Create new key pair**.
     * Name: `polymarket-ireland-key`
     * Type: `RSA`, format: `.pem` (for OpenSSH / Windows PowerShell).
     * Click **Create key pair** and save the downloaded file to:  
       `C:\Users\<YourUser>\Downloads\linux server\polymarket-ireland-key.pem`
   * **Network Settings (Security Group)**:
     * Check **Allow SSH traffic from** $\rightarrow$ Select **My IP** (or `0.0.0.0/0` if dynamic IP).
     * (Optional) Click **Edit network settings** $\rightarrow$ Add Security Group Rule:
       * Type: **Custom TCP**
       * Port: `8080`
       * Source: **My IP** (Allows direct access to dashboard without SSH tunnel).
   * **Configure Storage**:
     * Change size to **20 GiB** gp3 (plenty of disk space for logs and database).
5. Click **Launch Instance**.
6. On the Instances page, copy your new **Public IPv4 address** (e.g., `52.214.xxx.xxx`).

---

## Phase 2: Connecting & Verifying Compliance (The 3-Second Check)

### 2.1 Set SSH Key Permissions (Windows PowerShell)

Open PowerShell and verify the key file exists:
```powershell
Get-Item "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem"
```

### 2.2 SSH into the Server

Replace `<IRELAND_IP>` with your instance's public IP:
```powershell
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" ubuntu@<IRELAND_IP>
```

### 2.3 Run the 3-Second Compliance & Feed Verification

Run these two commands directly in your EC2 terminal:

```bash
# 1. Verify Polymarket Geoblock is NOT active
curl -s https://polymarket.com/api/geoblock
```
**Expected Output:**
```json
{"blocked": false, "country": "IE", "region": "L"}
```
*(Must show `"blocked": false`)*

```bash
# 2. Verify Binance price feed is NOT restricted
curl -sI https://api.binance.com/api/v3/ping
```
**Expected Output:**
```http
HTTP/2 200
```
*(No HTTP 451 error)*

---

## Phase 3: Base Environment & Tool Installation

Run these commands on the Ireland EC2 terminal to set up Python 3.12, Git, Node.js, and PM2:

```bash
# 1. Update OS packages
sudo apt update && sudo apt upgrade -y

# 2. Install Python, Pip, venv, and build tools
sudo apt install -y python3 python3-pip python3-venv git curl build-essential

# 3. Install Node.js 20.x and PM2 (Process Manager)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
sudo npm install -g pm2

# 4. Verify installations
python3 --version
node --version
pm2 --version
```

---

## Phase 4: Repository Setup & Python Environment

```bash
# 1. Clone repository into home directory
cd /home/ubuntu
git clone -b feature/new-5m-strategy https://github.com/alhaannn/polymarket-btc-5-minute.git
cd polymarket-btc-5-minute

# 2. Create isolated Python virtual environment
python3 -m venv venv

# 3. Upgrade pip and install all project requirements
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
venv/bin/pip install py-clob-client python-dotenv

# 4. Run automated test suite to verify code health
venv/bin/python3 -m pytest -q
```
**Expected Output:**
```text
60 passed in 2.40s
```

---

## Phase 5: Wallet & Live Environment Configuration

### 5.1 Polygon Wallet Prerequisites
To trade live on Polymarket, your wallet must have:
1. **USDC (PoS / Native USDC on Polygon)**: E.g., $25 to $50 starting collateral.
2. **POL / MATIC (Gas)**: E.g., 1–2 POL for any on-chain approvals or settlements.
3. **Wallet Type**:
   * Standard Private Key (EOA): `POLY_SIG_TYPE=0`
   * Polymarket Proxy / Gnosis Safe / Magic Link: `POLY_SIG_TYPE=2`

### 5.2 Create the `.env` Configuration File

On the EC2 server inside `/home/ubuntu/polymarket-btc-5-minute`, create `.env`:

```bash
nano .env
```

Paste your live configuration (adjust wallet keys and amounts):

```ini
# =====================================================================
# POLYMARKET 5-MINUTE LIVE PRODUCTION CONFIGURATION (IRELAND)
# =====================================================================

# 1. Mandatory Safety Double-Gate
LIVE_CONFIRM=YES_I_UNDERSTAND

# 2. Polygon Wallet Credentials
POLY_PRIVATE_KEY=0xYOUR_POLYGON_PRIVATE_KEY_HERE
POLY_SIGNER_ADDRESS=0xYOUR_SIGNER_ADDRESS_HERE
POLY_FUNDER_ADDRESS=0xYOUR_FUNDER_ADDRESS_HERE
POLY_SIG_TYPE=2

# 3. Polygon RPC Endpoint
POLYGON_RPC_URL=https://polygon-bor-rpc.publicnode.com

# 4. Canonical Sizing ($1 Initial / 10x Reversal Scaling)
SIZING_MODE=canonical
INITIAL_STAKE_USD=1.00
REVERSAL_STAKE_USD=10.00
REVERSAL_MULTIPLIER=10.0
MICRO_STAKE_CAP_USD=10.00

# 5. Core Sniper & Reversal Thresholds
PRIME_HOURS_ONLY=true
ENABLE_REVERSALS=true
INITIAL_PROB_MIN=0.89
INITIAL_PROB_MAX=0.91
PRICE_CEILING=0.910
REVERSAL_PRICE_CEILING=0.930
MAX_SLIPPAGE=0.035
REVERSAL_PROB_MIN=0.85
REVERSAL_PROB_MAX=0.95
EARLY_EXIT=false
EXIT_P=0.35
RECOVERY_FLIP_MODE=fixed
RECOVERY_MAX_TIME_SEC=280.0
RECOVERY_MAX_BALANCE_PCT=0.80

# 6. Risk Circuit Breakers
DAILY_LOSS_LIMIT_USD=15.00
DAILY_LOSS_LIMIT_PCT=0.40
DAILY_LOSS_MIN_USD=10.00
PAUSE_CONSECUTIVE_LOSSES=3
PAUSE_DURATION_WINDOWS=3
KILL_SWITCH_FILE=STOP

# 7. Dashboard Configuration
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8080
DASHBOARD_ENABLED=true
```

Save and exit (`Ctrl + O`, `Enter`, then `Ctrl + X`).

Secure the file permissions:
```bash
chmod 600 .env
```

---

## Phase 6: Starting & Daemonizing with PM2

### 6.1 Test Initial Startup (Console Mode)

Run the bot directly once to ensure all WebSocket connections and feeds establish cleanly:

```bash
venv/bin/python3 -m polybot.main paper --strategy reversal
```
Verify you see:
* `Connected to RTDS WebSocket`
* `Connected to Binance WebSocket` (Native connection without HTTP 451)
* `Connected to CLOB WebSocket`
* `Reconciliation successful`

Press `Ctrl + C` to stop the test.

---

### 6.2 Launch with PM2 Background Process

Start the persistent daemon under PM2:

```bash
pm2 start venv/bin/python3 --name "polybot-live" --update-env -- -m polybot.main paper --strategy reversal
```

*(Note: When you are ready for real on-chain order dispatch, pass your live execution flag according to `polybot/execution/live.py`)*

### 6.3 Configure PM2 Auto-Restart on Server Reboot

To guarantee the bot restarts automatically if AWS reboots the server:

```bash
pm2 save
pm2 startup
```
*(Copy and paste the `sudo env PATH=...` command that PM2 outputs to enable the systemd service).*

---

## Phase 7: Real-Time Monitoring & Web Dashboard

### 7.1 View Live Logs

```bash
# View last 40 lines
pm2 logs polybot-live --lines 40 --nostream

# Stream logs in real-time
pm2 logs polybot-live
```

### 7.2 Access the Web Dashboard Locally

From your **local Windows PowerShell**, open an encrypted SSH tunnel to forward the dashboard port:

```powershell
ssh -i "C:\Users\HP Victus\Downloads\linux server\polymarket-ireland-key.pem" -N -L 8080:127.0.0.1:8080 ubuntu@<IRELAND_IP>
```

Open your browser to:
**`http://localhost:8080`**

### 7.3 Instant Terminal Health Check Commands

You can run these curl commands to inspect your stats instantly:

```bash
# Check Capital & Available Sizing
curl -s http://localhost:8080/api/capital

# Check IST Performance (Hourly, Daily, Lifetime Win Rate)
curl -s http://localhost:8080/api/performance

# Check Current 5-Minute Window & Token Prices
curl -s http://localhost:8080/api/state

# Check Recent Trades & Settled PnL
curl -s http://localhost:8080/api/trades
```

---

## Phase 8: Operational Commands & Safety Controls

### 8.1 Emergency Kill Switch

To immediately halt all trading and cancel any open orders:

```bash
# Option A: Touch the KILL_SWITCH_FILE (graceful zero-exposure stop)
touch /home/ubuntu/polymarket-btc-5-minute/STOP

# Option B: Stop PM2 process immediately
pm2 stop polybot-live
```

To resume:
```bash
rm -f /home/ubuntu/polymarket-btc-5-minute/STOP
pm2 restart polybot-live --update-env
```

### 8.2 Updating Code from GitHub

Whenever you push new improvements to the repository from your local machine:

```bash
cd /home/ubuntu/polymarket-btc-5-minute
git pull origin feature/new-5m-strategy
pm2 restart polybot-live --update-env
```

### 8.3 Resetting Stats & Capital to $11.00 Clean Slate

If you ever wish to clear historical paper/test logs and reset your balance back to an exact starting state:

```bash
pm2 stop polybot-live
python3 -m polybot.reset
pm2 restart polybot-live --update-env
```

---

## Summary of Ireland (`eu-west-1`) Advantages

1. **Zero Geoblock**: Fully approved on Polymarket's compliance infrastructure.
2. **Sub-2ms Order Book Speed**: Direct low-latency network route into Polymarket's London matching engine.
3. **Dual Unrestricted Feeds**: Both Polymarket RTDS and Binance WebSocket streams operate at peak speed.
4. **24/7 PM2 Uptime**: Continuous trading during Prime Hours (13:30 – 01:00 IST) with auto-restart on reboot.
