# Polymarket BTC 5m Up/Down - late-entry paper trader

Strategy: once 3:00 of the 5:00 window has elapsed (2 min left), buy whichever side's
best ask is between 0.93 and 0.96, fixed stake, hold to resolution. One trade per window.

## Run on Ubuntu VPS

    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python bot.py

Keep it alive with tmux (`tmux new -s bot`) or a systemd service.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| STAKE_USD | 5 | dollars per trade |
| PRICE_MIN / PRICE_MAX | 0.93 / 0.96 | entry band on best ask |
| ENTRY_START_SEC | 180 | seconds into window before looking |
| ENTRY_END_SEC | 285 | stop looking after this |
| FEE_RATE | 0 | taker fee as a fraction of notional (set from Polymarket's fee schedule) |
| POLL_SEC | 1 | order book poll interval |

Example: `STAKE_USD=10 PRICE_MAX=0.95 python bot.py`

## Output (./data)

- `trades.csv`: every paper trade with fill price, shares, winner, PnL
- `windows.csv`: every window, traded or not, with the highest ask seen on each side

Fills are simulated by walking the real order book, so slippage is included.
The bot prints the break-even win rate at startup and running win rate/PnL after each settlement.
