/**
 * Polymarket Workstation Client Logic
 * Handles real-time polling, gauge animation, tab switching, and bot controls.
 */

let activePerfTab = 'lifetime';
let performanceData = null;
let lastLogTimestamp = 0;

document.addEventListener('DOMContentLoaded', () => {
  initPolling();
  updateClockLocal();
  setInterval(updateClockLocal, 1000);
});

function updateClockLocal() {
  const now = new Date();
  const timeStr = now.toTimeString().split(' ')[0];
  const el = document.getElementById('clock-time');
  if (el) el.textContent = timeStr;
}

function initPolling() {
  fetchState();
  fetchPerformance();
  fetchTrades();

  // Rapid refresh cycle for smooth market timer & probability gauges (500ms)
  setInterval(fetchState, 500);
  // Metrics & trades poll every 2.0s
  setInterval(() => {
    fetchPerformance();
    fetchTrades();
  }, 2000);
}

async function fetchState() {
  try {
    const res = await fetch('/api/state');
    if (!res.ok) throw new Error('State fetch failed');
    const data = await res.json();
    renderState(data);
  } catch (err) {
    console.debug('State poll error:', err);
    setFeedStatusDisconnected();
  }
}

async function fetchPerformance() {
  try {
    const res = await fetch('/api/performance');
    if (!res.ok) throw new Error('Performance fetch failed');
    performanceData = await res.json();
    renderPerformanceTab();
  } catch (err) {
    console.debug('Perf poll error:', err);
  }
}

async function fetchTrades() {
  try {
    const res = await fetch('/api/trades');
    if (!res.ok) throw new Error('Trades fetch failed');
    const trades = await res.json();
    renderTradesTable(trades);
  } catch (err) {
    console.debug('Trades poll error:', err);
  }
}

function setFeedStatusDisconnected() {
  const pill = document.getElementById('pill-feed-status');
  const text = document.getElementById('text-feed-status');
  if (pill && text) {
    pill.className = 'status-pill feed-disconnected';
    text.textContent = 'FEED: DISCONNECTED';
  }
}

function renderState(state) {
  // 1. Bot status & pill
  const botPill = document.getElementById('pill-bot-status');
  const botText = document.getElementById('text-bot-status');
  if (botPill && botText) {
    botText.textContent = state.bot_status || 'UNKNOWN';
    if (state.bot_status === 'RUNNING') {
      botPill.className = 'status-pill status-running';
    } else if (state.bot_status === 'PAUSED') {
      botPill.className = 'status-pill status-paused';
    } else {
      botPill.className = 'status-pill status-stopped';
    }
  }

  // 2. Feed status & Latency
  const feedPill = document.getElementById('pill-feed-status');
  const feedText = document.getElementById('text-feed-status');
  if (feedPill && feedText) {
    if (state.feed_status === 'CONNECTED') {
      feedPill.className = 'status-pill feed-live';
      feedText.textContent = 'FEED: LIVE';
    } else if (state.feed_status === 'STALE') {
      feedPill.className = 'status-pill feed-stale';
      feedText.textContent = `FEED: STALE (${state.staleness_sec}s)`;
    } else {
      feedPill.className = 'status-pill feed-disconnected';
      feedText.textContent = 'FEED: DISCONNECTED';
    }
  }

  const latVal = document.getElementById('val-latency');
  if (latVal) {
    latVal.textContent = `${state.latency_ms.toFixed(1)} ms`;
  }

  // 3. Market Session & Clock
  const slugEl = document.getElementById('val-market-slug');
  if (slugEl) slugEl.textContent = state.current_slug || 'Scanning for active market...';

  const phaseBadge = document.getElementById('badge-market-phase');
  if (phaseBadge) {
    if (state.phase === 'WAITING') {
      phaseBadge.className = 'phase-badge phase-waiting';
      phaseBadge.textContent = 'WAITING PHASE (0 - 180s)';
    } else if (state.phase === 'ACTIVE') {
      phaseBadge.className = 'phase-badge phase-active';
      phaseBadge.textContent = 'ACTIVE TRADING WINDOW (180 - 300s)';
    } else {
      phaseBadge.className = 'phase-badge phase-closed';
      phaseBadge.textContent = 'MARKET CLOSED / SETTLING (>= 300s)';
    }
  }

  // Progress bars (300s cycle)
  const elapsed = Math.min(300.0, Math.max(0.0, state.elapsed_sec));
  const waitingBar = document.getElementById('progress-bar-waiting');
  const tradingBar = document.getElementById('progress-bar-trading');

  if (waitingBar && tradingBar) {
    const waitingPct = (Math.min(180.0, elapsed) / 300.0) * 100.0;
    const tradingPct = (Math.max(0.0, elapsed - 180.0) / 300.0) * 100.0;
    waitingBar.style.width = `${waitingPct}%`;
    tradingBar.style.width = `${tradingPct}%`;
  }

  const elTime = document.getElementById('val-elapsed-time');
  if (elTime) elTime.textContent = `${state.elapsed_sec.toFixed(1)}s`;

  const remTime = document.getElementById('val-remaining-time');
  if (remTime) remTime.textContent = `${state.remaining_sec.toFixed(1)}s`;

  const winStatus = document.getElementById('val-window-status');
  if (winStatus) {
    if (state.is_trading_allowed) {
      winStatus.className = 'stat-value status-badge-active';
      winStatus.textContent = 'ELIGIBLE (120s WINDOW)';
    } else if (state.elapsed_sec < 180) {
      winStatus.className = 'stat-value status-badge-inactive';
      winStatus.textContent = `WAITING (${(180 - state.elapsed_sec).toFixed(0)}s left)`;
    } else {
      winStatus.className = 'stat-value status-badge-inactive';
      winStatus.textContent = 'CLOSED (NO NEW TRADES)';
    }
  }

  const freshEl = document.getElementById('val-data-freshness');
  if (freshEl) {
    freshEl.textContent = state.staleness_sec < 1 ? '< 1s ago' : `${state.staleness_sec.toFixed(1)}s ago`;
  }

  // 4. Outcome Probabilities
  const upProbEl = document.getElementById('val-up-prob');
  const upCentsEl = document.getElementById('val-up-cents');
  const upAskEl = document.getElementById('val-up-ask');
  const gaugeUp = document.getElementById('gauge-up');
  const badgeUp = document.getElementById('badge-up-status');

  if (state.up_prob !== null && state.up_prob !== undefined) {
    const upPct = (state.up_prob * 100).toFixed(1);
    if (upProbEl) upProbEl.textContent = `${upPct}%`;
    if (upCentsEl) upCentsEl.textContent = `($${state.up_prob.toFixed(3)})`;
    if (upAskEl) upAskEl.textContent = `Ask: $${state.up_prob.toFixed(3)}`;
    if (gaugeUp) gaugeUp.style.width = `${Math.min(100, Math.max(0, state.up_prob * 100))}%`;

    // Up badge
    if (badgeUp) {
      if (state.is_trading_allowed && state.up_prob >= 0.89 && state.up_prob <= 0.92) {
        badgeUp.className = 'outcome-state-badge badge-qualified-initial';
        badgeUp.textContent = 'INITIAL RANGE QUALIFIED [89% - 92%]';
      } else if (state.is_trading_allowed && state.up_prob >= 0.85 && state.up_prob <= 0.90 && state.initial_side === 'Down') {
        badgeUp.className = 'outcome-state-badge badge-qualified-reversal';
        badgeUp.textContent = 'REVERSAL RANGE QUALIFIED [85% - 90%]';
      } else {
        badgeUp.className = 'outcome-state-badge badge-neutral';
        badgeUp.textContent = state.is_trading_allowed ? 'In Trading Window' : 'Waiting Phase';
      }
    }
  } else {
    if (upProbEl) upProbEl.textContent = '--%';
    if (upAskEl) upAskEl.textContent = 'Ask: None';
  }

  const downProbEl = document.getElementById('val-down-prob');
  const downCentsEl = document.getElementById('val-down-cents');
  const downAskEl = document.getElementById('val-down-ask');
  const gaugeDown = document.getElementById('gauge-down');
  const badgeDown = document.getElementById('badge-down-status');

  if (state.down_prob !== null && state.down_prob !== undefined) {
    const downPct = (state.down_prob * 100).toFixed(1);
    if (downProbEl) downProbEl.textContent = `${downPct}%`;
    if (downCentsEl) downCentsEl.textContent = `($${state.down_prob.toFixed(3)})`;
    if (downAskEl) downAskEl.textContent = `Ask: $${state.down_prob.toFixed(3)}`;
    if (gaugeDown) gaugeDown.style.width = `${Math.min(100, Math.max(0, state.down_prob * 100))}%`;

    // Down badge
    if (badgeDown) {
      if (state.is_trading_allowed && state.down_prob >= 0.89 && state.down_prob <= 0.92) {
        badgeDown.className = 'outcome-state-badge badge-qualified-initial';
        badgeDown.textContent = 'INITIAL RANGE QUALIFIED [89% - 92%]';
      } else if (state.is_trading_allowed && state.down_prob >= 0.85 && state.down_prob <= 0.90 && state.initial_side === 'Up') {
        badgeDown.className = 'outcome-state-badge badge-qualified-reversal';
        badgeDown.textContent = 'REVERSAL RANGE QUALIFIED [85% - 90%]';
      } else {
        badgeDown.className = 'outcome-state-badge badge-neutral';
        badgeDown.textContent = state.is_trading_allowed ? 'In Trading Window' : 'Waiting Phase';
      }
    }
  } else {
    if (downProbEl) downProbEl.textContent = '--%';
    if (downAskEl) downAskEl.textContent = 'Ask: None';
  }

  // 5. Strategy step progression
  const stepInit = document.getElementById('step-initial');
  const descInit = document.getElementById('desc-step-initial');
  const stepRev = document.getElementById('step-reversal');
  const descRev = document.getElementById('desc-step-reversal');

  if (state.initial_order_triggered) {
    if (stepInit) stepInit.className = 'exec-step-item step-done';
    if (descInit) descInit.textContent = `Filled: ${state.initial_side} ($1.00)`;

    if (state.reversal_order_triggered) {
      if (stepRev) stepRev.className = 'exec-step-item step-done';
      if (descRev) descRev.textContent = 'Filled ($10.00 Reversal)';
    } else {
      if (stepRev) stepRev.className = 'exec-step-item step-active';
      const opp = state.initial_side === 'Up' ? 'Down' : 'Up';
      if (descRev) descRev.textContent = `Active: Monitoring ${opp} for 85%–90%`;
    }
  } else {
    if (stepInit) {
      stepInit.className = state.is_trading_allowed ? 'exec-step-item step-active' : 'exec-step-item';
    }
    if (descInit) {
      descInit.textContent = state.is_trading_allowed ? 'Active: scanning 89%–92%' : 'Waiting: gate opens at 180s';
    }
    if (stepRev) stepRev.className = 'exec-step-item';
    if (descRev) descRev.textContent = 'Awaiting initial trade';
  }

  // 6. Logs stream
  if (state.logs && state.logs.length > 0) {
    renderLogs(state.logs);
  }
}

function renderLogs(logs) {
  const container = document.getElementById('log-stream');
  if (!container) return;

  const entriesHtml = logs.map(l => {
    let tagClass = 'tag-info';
    if (l.level.includes('SIGNAL') || l.level.includes('BUY')) tagClass = 'tag-signal';
    if (l.level.includes('REVERSAL')) tagClass = 'tag-reversal';
    if (l.level.includes('SETTLED')) tagClass = 'tag-settled';
    if (l.level.includes('CONTROL')) tagClass = 'tag-control';

    return `
      <div class="log-entry">
        <span class="log-time">${l.time_str}</span>
        <span class="log-tag ${tagClass}">${l.level}</span>
        <span class="log-message">${escapeHtml(l.message)}</span>
      </div>
    `;
  }).join('');

  container.innerHTML = entriesHtml;
}

function renderPerformanceTab() {
  if (!performanceData) return;
  const metrics = performanceData[activePerfTab] || performanceData['lifetime'];
  if (!metrics) return;

  // Net PnL
  const pnlEl = document.getElementById('val-net-pnl');
  const pnlStatus = document.getElementById('val-pnl-status');
  if (pnlEl) {
    const pnl = metrics.net_pnl_usd;
    pnlEl.textContent = `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`;
    if (pnl > 0) {
      pnlEl.className = 'metric-large pnl-positive';
      if (pnlStatus) pnlStatus.textContent = 'NET PROFIT (REALIZED)';
    } else if (pnl < 0) {
      pnlEl.className = 'metric-large pnl-negative';
      if (pnlStatus) pnlStatus.textContent = 'NET LOSS (REALIZED)';
    } else {
      pnlEl.className = 'metric-large pnl-neutral';
      if (pnlStatus) pnlStatus.textContent = metrics.settled_trades > 0 ? 'BREAK EVEN' : 'NO REALIZED TRADES';
    }
  }

  // Win rate
  const wrEl = document.getElementById('val-win-rate');
  const winCountEl = document.getElementById('val-win-count');
  if (wrEl) wrEl.textContent = `${metrics.win_rate_pct.toFixed(1)}%`;
  if (winCountEl) winCountEl.textContent = `${metrics.wins} Wins / ${metrics.losses} Losses`;

  // Gross profit / loss
  const gpEl = document.getElementById('val-gross-profit');
  if (gpEl) gpEl.textContent = `+$${metrics.gross_profit_usd.toFixed(2)}`;
  const glEl = document.getElementById('val-gross-loss');
  if (glEl) glEl.textContent = `-$${metrics.gross_loss_usd.toFixed(2)}`;

  // Total orders
  const ordersEl = document.getElementById('val-total-orders');
  const splitEl = document.getElementById('val-order-split');
  if (ordersEl) ordersEl.textContent = metrics.total_trades;
  if (splitEl) splitEl.textContent = `${metrics.initial_trades} Init ($1) • ${metrics.reversal_trades} Rev ($10)`;

  // Reversals & markets
  const revCountEl = document.getElementById('val-reversals-count');
  const mktCountEl = document.getElementById('val-markets-count');
  if (revCountEl) revCountEl.textContent = performanceData.reversal_events_detected;
  if (mktCountEl) mktCountEl.textContent = `${performanceData.markets_observed} Markets Observed`;
}

function switchPerfTab(tabName) {
  activePerfTab = tabName;
  document.querySelectorAll('.time-tabs .tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-tab') === tabName);
  });
  renderPerformanceTab();
}

function renderTradesTable(trades) {
  const tbody = document.getElementById('tbody-trades');
  if (!tbody) return;

  if (!trades || trades.length === 0) {
    tbody.innerHTML = `
      <tr class="table-row-empty">
        <td colspan="13">No paper trades recorded yet in this session.</td>
      </tr>
    `;
    return;
  }

  const rowsHtml = trades.map(t => {
    const isWin = t.winner && t.side === t.winner;
    const isLoss = t.winner && t.side !== t.winner;
    let pnlClass = 'pnl-neutral';
    let pnlText = '--';

    if (t.status === 'SETTLED' && t.pnl_usd !== null) {
      pnlClass = t.pnl_usd >= 0 ? 'text-green' : 'text-red';
      pnlText = `${t.pnl_usd >= 0 ? '+' : ''}$${t.pnl_usd.toFixed(2)}`;
    }

    const typeBadge = t.signal_type === 'REVERSAL'
      ? `<span class="badge-tag-reversal">REVERSAL 10x</span>`
      : `<span class="badge-tag-initial">INITIAL $1</span>`;

    const sideBadge = t.side === 'Up'
      ? `<span class="outcome-pill pill-up">UP</span>`
      : `<span class="outcome-pill pill-down">DOWN</span>`;

    const timeStr = t.exec_ts ? new Date(t.exec_ts * 1000).toTimeString().split(' ')[0] : '--';
    const latStr = t.latency_ms ? `${t.latency_ms.toFixed(1)}ms` : '< 1ms';

    return `
      <tr>
        <td><code>${t.order_id}</code></td>
        <td><code>${t.slug}</code></td>
        <td>${typeBadge}</td>
        <td>${sideBadge}</td>
        <td>$${t.stake_usd.toFixed(2)}</td>
        <td>${t.shares.toFixed(2)}</td>
        <td>$${t.avg_price.toFixed(3)}</td>
        <td>${t.trigger_prob ? (t.trigger_prob * 100).toFixed(1) + '%' : '--'}</td>
        <td>${latStr}</td>
        <td>${timeStr}</td>
        <td><strong>${t.status}</strong></td>
        <td>${t.winner || '--'}</td>
        <td class="${pnlClass}"><strong>${pnlText}</strong></td>
      </tr>
    `;
  }).join('');

  tbody.innerHTML = rowsHtml;
}

async function sendControl(action) {
  try {
    const res = await fetch('/api/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    });
    const result = await res.json();
    console.log('Control response:', result);
    fetchState();
  } catch (err) {
    alert('Failed to send control command: ' + err.message);
  }
}

function clearLocalLogs() {
  const container = document.getElementById('log-stream');
  if (container) {
    container.innerHTML = '<div class="log-empty">Log cleared locally.</div>';
  }
}

function exportTradesCSV() {
  window.open('/api/trades/csv', '_blank');
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
