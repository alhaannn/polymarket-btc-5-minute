# Ubuntu VPS Installation & Deployment Guide

This guide walks you through setting up `polybot` on a fresh Ubuntu VPS (20.04/22.04/24.04 LTS).

---

## 1. System Preparation & Dependencies

```bash
# Update system packages
sudo apt update && sudo apt upgrade -y

# Install Python 3, venv, git, and build tools
sudo apt install -y python3 python3-venv python3-pip git tmux curl
```

---

## 2. Clone Repository & Setup Virtual Environment

```bash
# Clone the repository
git clone https://github.com/alhaannn/polymarket-btc-5-minute.git
cd polymarket-btc-5-minute

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Upgrade pip and install requirements
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 3. Configuration & Secrets

Create a `.env` file in the project root:

```bash
nano .env
```

Add your optional monitoring configuration:

```env
# Storage
DATA_DIR=data
RAW_DATA_DIR=data/raw

# Trading Parameters
STRATEGY=edge
STAKE_USD=5.0
PRICE_MIN=0.85
PRICE_MAX=0.985
MIN_EDGE=0.02

# Risk Management
DAILY_LOSS_LIMIT_USD=25.0
MAX_TRADES_PER_DAY=50
PAUSE_CONSECUTIVE_LOSSES=3

# Optional Telegram Alerts
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

---

## 4. Run the 24/7 Data Recorder (First Step)

To collect real data before deploying capital, start the recorder.

### Option A: Using systemd (Recommended for 24/7 reliability)

```bash
# Copy systemd unit file
sudo cp deploy/polybot-recorder.service /etc/systemd/system/

# Reload systemd daemon
sudo systemctl daemon-reload

# Enable and start the service
sudo systemctl enable polybot-recorder
sudo systemctl start polybot-recorder

# Check status
sudo systemctl status polybot-recorder

# View real-time logs
journalctl -u polybot-recorder -f
```

### Option B: Using tmux

```bash
tmux new -s recorder
source .venv/bin/activate
python main.py recorder
# Detach with: Ctrl+B then D
# Reattach with: tmux attach -t recorder
```

---

## 5. Running the Backtester & Parameter Sweeper

Once you have recorded 1–2 weeks of data:

```bash
# Backtest the Edge strategy on collected windows
python backtest.py --strategy edge --data data/raw

# A/B compare against the legacy strategy
python backtest.py --strategy legacy --data data/raw

# Run parameter sweep with 70/30 hold-out split
python backtest.py --sweep --data data/raw
```

---

## 6. Running the Paper Trading Bot

```bash
# Copy systemd paper unit file
sudo cp deploy/polybot-paper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable polybot-paper
sudo systemctl start polybot-paper

# Or run interactively
python main.py paper --strategy edge --stake 5.0
```

---

## 7. Emergency Kill Switch

To halt all trading operations immediately without killing the process:

```bash
# Create the STOP file in the project root
touch STOP

# To resume trading later:
rm STOP
```
