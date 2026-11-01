/**
 * Polymarket Workstation Client Logic
 * Handles real-time polling, gauge animation, tab switching, and bot controls.
 */

let activePerfTab = 'lifetime';
let performanceData = null;
let lastLogTimestamp = 0;
let autoScrollEnabled = true;
let lastRenderedLogHash = '';

// Pagination state for Execution & Capital Ledger History (50 items per page)
const PAGE_SIZE = 50;
let currentArchiveTab = 'orders';
let allTrades = [];
let allCapitalTxs = [];
let allArchiveLogs = [];
const archivePages = {
  orders: 1,
  'capital-tx': 1,
  logs: 1,
};

document.addEventListener('DOMContentLoaded', () => {
  initPolling();
  updateClockIST();
  setInterval(updateClockIST, 1000);
});

function updateClockIST() {
  const now = new Date();
  const timeStr = now.toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour12: false });
  const el = document.getElementById('clock-time');
  if (el) el.textContent = timeStr;
}

function formatISTTime(unixSec) {
  if (!unixSec) return '--';
  return new Date(unixSec * 1000).toLocaleTimeString('en-GB', {
    timeZone: 'Asia/Kolkata',
    hour12: false,
  });
}

function formatISTDateTime(unixSec) {
  if (!unixSec) return '--';
  return new Date(unixSec * 1000).toLocaleString('en-GB', {
    timeZone: 'Asia/Kolkata',
    hour12: false,
  });
}

function toggleAutoScroll() {
  autoScrollEnabled = !autoScrollEnabled;
  const btn = document.getElementById('btn-toggle-autoscroll');
  if (btn) {
    btn.textContent = autoScrollEnabled ? 'Auto-scroll: ON' : 'Auto-scroll: OFF';
    btn.classList.toggle('active', autoScrollEnabled);
  }
}

function initPolling() {
  fetchState();
  fetchCapital();
  fetchPerformance();
  fetchTrades();
  fetchCapitalTransactions();
  fetchArchiveLogs();

  // Rapid refresh cycle for smooth market timer & probability gauges (500ms)
  setInterval(fetchState, 500);
  // Metrics, capital, trades, & archive logs poll every 2.0s
  setInterval(() => {
    fetchCapital();
    fetchPerformance();
    fetchTrades();
    fetchCapitalTransactions();
    fetchArchiveLogs();
  }, 2000);
}

async function fetchArchiveLogs() {
  try {
    const res = await fetch('/api/logs?limit=1000');
    if (!res.ok) throw new Error('Logs fetch failed');
    const logs = await res.json();
    allArchiveLogs = logs || [];
    if (currentArchiveTab === 'logs') {
      renderArchiveLogs();
    }
  } catch (err) {
    console.debug('Archive logs poll error:', err);
  }
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
    const res = await fetch('/api/trades?limit=1000');
    if (!res.ok) throw new Error('Trades fetch failed');
    const trades = await res.json();
    allTrades = trades || [];
    renderTradesTable();
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

  // 7. Capital state rendering
  if (state.capital) {
    renderCapital(state.capital);
  }
}

function getTagClass(level) {
  const lvl = (level || '').toUpperCase();
  if (lvl.includes('REVERSAL')) return 'tag-reversal';
  if (lvl.includes('SIGNAL')) return 'tag-signal';
  if (lvl.includes('ORDER')) return 'tag-order';
  if (lvl.includes('BUY') || lvl.includes('FILL')) return 'tag-buy';
  if (lvl.includes('SETTLED')) return 'tag-settled';
  if (lvl.includes('REJECTED')) return 'tag-rejected';
  if (lvl.includes('SHUTDOWN')) return 'tag-shutdown';
  if (lvl.includes('RESERVE') || lvl.includes('CAPITAL')) return 'tag-capital';
  if (lvl.includes('MARKET')) return 'tag-market';
  if (lvl.includes('RECON')) return 'tag-recon';
  if (lvl.includes('CONTROL')) return 'tag-control';
  if (lvl.includes('WARN')) return 'tag-warning';
  return 'tag-info';
}

function renderLogs(logs) {
  const container = document.getElementById('log-stream');
  if (!container) return;

  if (!logs || logs.length === 0) {
    container.innerHTML = '<div class="log-empty">Listening for engine trade events and telemetry...</div>';
    return;
  }

  // Display in chronological order (oldest at top, newest at bottom of scroller)
  const chronological = logs.slice().reverse();
  const currentHash = chronological.map(l => `${l.timestamp}-${l.level}-${l.message}`).join('|');
  if (currentHash === lastRenderedLogHash) {
    return; // Don't disrupt DOM or scroll position if logs unchanged
  }
  lastRenderedLogHash = currentHash;

  const entriesHtml = chronological.map(l => {
    const tagClass = getTagClass(l.level);
    const timeStr = l.timestamp ? formatISTTime(l.timestamp) : (l.time_str || '--');
    return `
      <div class="log-entry">
        <span class="log-time">${timeStr}</span>
        <span class="log-tag ${tagClass}">${escapeHtml(l.level)}</span>
        <span class="log-message">${escapeHtml(l.message)}</span>
      </div>
    `;
  }).join('');

  container.innerHTML = entriesHtml;

  if (autoScrollEnabled) {
    container.scrollTop = container.scrollHeight;
  }
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
  if (trades !== undefined) {
    allTrades = trades || [];
  }
  const tbody = document.getElementById('tbody-trades');
  if (!tbody) return;

  const total = allTrades.length;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (archivePages.orders > totalPages) archivePages.orders = totalPages;
  if (archivePages.orders < 1) archivePages.orders = 1;

  const page = archivePages.orders;
  const start = (page - 1) * PAGE_SIZE;
  const pageItems = allTrades.slice(start, start + PAGE_SIZE);

  if (total === 0) {
    tbody.innerHTML = `
      <tr class="table-row-empty">
        <td colspan="13">No paper trades recorded yet in this session.</td>
      </tr>
    `;
  } else {
    tbody.innerHTML = pageItems.map(t => {
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

      const timeStr = formatISTTime(t.exec_ts);
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
  }

  if (currentArchiveTab === 'orders') {
    updatePaginationControls(total, page, totalPages, 'orders');
  }
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

async function fetchCapital() {
  try {
    const res = await fetch('/api/capital');
    if (!res.ok) throw new Error('Capital fetch failed');
    const data = await res.json();
    renderCapital(data);
  } catch (err) {
    console.debug('Capital poll error:', err);
  }
}

function renderCapital(cap) {
  if (!cap) return;

  const totalEl = document.getElementById('val-cap-total');
  if (totalEl) totalEl.textContent = `$${(cap.total_equity || 0).toFixed(2)}`;

  const activeEl = document.getElementById('val-cap-active');
  const activeSubEl = document.getElementById('val-cap-active-sub');
  if (activeEl) {
    const spendable = cap.spendable_active_funds !== undefined ? cap.spendable_active_funds : (cap.active_capital || 0);
    activeEl.textContent = `$${spendable.toFixed(2)}`;
    if (activeSubEl) {
      const reserved = cap.active_funds_reserved !== undefined ? cap.active_funds_reserved : (cap.open_exposure || 0);
      activeSubEl.textContent = `Reserved: $${reserved.toFixed(2)} | Total Active: $${(cap.active_capital || 0).toFixed(2)}`;
    }
  }

  const protEl = document.getElementById('val-cap-protected');
  const protSubEl = document.getElementById('val-cap-protected-sub');
  if (protEl) {
    protEl.textContent = `$${(cap.protected_capital || 0).toFixed(2)}`;
    if (protSubEl) {
      protSubEl.textContent = cap.milestone_44_activated
        ? 'Active 50/50 reserve compounding'
        : 'Accumulates after $44 milestone';
    }
  }

  const optBEl = document.getElementById('val-cap-option-b');
  const optBSubEl = document.getElementById('val-cap-option-b-sub');
  if (optBEl) {
    if (cap.next_day_activation_eligible) {
      optBEl.textContent = 'ELIGIBLE';
      optBEl.className = 'metric-large text-green';
      if (optBSubEl) optBSubEl.textContent = '50% protected release queued for tomorrow';
    } else if (cap.last_activation_trading_day) {
      optBEl.textContent = 'RELEASED';
      optBEl.className = 'metric-large';
      if (optBSubEl) optBSubEl.textContent = `Last activated for day: ${cap.last_activation_trading_day}`;
    } else {
      optBEl.textContent = 'INACTIVE';
      optBEl.className = 'metric-large';
      if (optBSubEl) optBSubEl.textContent = 'Triggers after qualifying shutdown';
    }
  }

  const msPill = document.getElementById('pill-cap-ms44');
  if (msPill) {
    if (cap.milestone_44_activated) {
      msPill.className = 'status-pill status-running';
      msPill.textContent = 'MILESTONE $44: ACTIVATED';
    } else {
      msPill.className = 'status-pill mode-paper';
      msPill.textContent = 'MILESTONE $44: PENDING';
    }
  }

  const reconPill = document.getElementById('pill-cap-recon');
  if (reconPill) {
    if (cap.reconciliation_blocked || cap.reconciliation_status === 'MISMATCH') {
      reconPill.className = 'status-pill status-stopped';
      reconPill.textContent = 'RECONCILIATION: MISMATCH';
    } else {
      reconPill.className = 'status-pill status-running';
      reconPill.textContent = 'RECONCILIATION: OK';
    }
  }

  const budgetsEl = document.getElementById('val-cap-budgets');
  if (budgetsEl) {
    budgetsEl.textContent = `$${cap.current_initial_budget || 1} Initial / $${cap.current_reversal_budget || 10} Reversal`;
  }

  const shutdownEl = document.getElementById('val-cap-shutdown-status');
  if (shutdownEl) {
    if (cap.daily_shutdown_status) {
      const nowSec = Date.now() / 1000;
      if (cap.shutdown_until_ts && cap.shutdown_until_ts > nowSec) {
        const remainingSec = Math.max(0, cap.shutdown_until_ts - nowSec);
        const hours = Math.floor(remainingSec / 3600);
        const mins = Math.floor((remainingSec % 3600) / 60);
        shutdownEl.textContent = `SHUTDOWN ACTIVE (Halting 12h • Resumes in ${hours}h ${mins}m)`;
      } else {
        shutdownEl.textContent = `SHUTDOWN ACTIVE (${cap.daily_shutdown_reason || 'Reversal failed'})`;
      }
      shutdownEl.style.color = 'var(--color-red)';
    } else {
      shutdownEl.textContent = `CLEAR (Rev: ${cap.daily_failed_reversal_count || 0}/1)`;
      shutdownEl.style.color = 'var(--text-primary)';
    }
  }

  const shortfallEl = document.getElementById('val-cap-shortfall');
  if (shortfallEl) {
    const sf = cap.active_shortfall || 0;
    if (sf > 0) {
      shortfallEl.textContent = `$${sf.toFixed(2)} (DEFICIT)`;
      shortfallEl.style.color = 'var(--color-red)';
    } else {
      shortfallEl.textContent = '$0.00 (Healthy)';
      shortfallEl.style.color = 'var(--color-green)';
    }
  }

  const lastAuditEl = document.getElementById('val-cap-last-audit');
  if (lastAuditEl && cap.last_reconciliation_ts > 0) {
    lastAuditEl.textContent = formatISTTime(cap.last_reconciliation_ts);
  }
}

async function fetchCapitalTransactions() {
  try {
    const res = await fetch('/api/capital/transactions?limit=1000');
    if (!res.ok) throw new Error('Capital transactions fetch failed');
    const txs = await res.json();
    allCapitalTxs = txs || [];
    renderCapitalTransactions();
  } catch (err) {
    console.debug('Capital tx poll error:', err);
  }
}

function renderCapitalTransactions(txs) {
  if (txs !== undefined) {
    allCapitalTxs = txs || [];
  }
  const tbody = document.getElementById('tbody-capital-transactions');
  if (!tbody) return;

  const total = allCapitalTxs.length;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (archivePages['capital-tx'] > totalPages) archivePages['capital-tx'] = totalPages;
  if (archivePages['capital-tx'] < 1) archivePages['capital-tx'] = 1;

  const page = archivePages['capital-tx'];
  const start = (page - 1) * PAGE_SIZE;
  const pageItems = allCapitalTxs.slice(start, start + PAGE_SIZE);

  if (total === 0) {
    tbody.innerHTML = `
      <tr class="table-row-empty">
        <td colspan="9">No capital transactions recorded yet in ledger.</td>
      </tr>
    `;
  } else {
    tbody.innerHTML = pageItems.map(t => {
      const amtUsd = (t.amount_cents / 100.0).toFixed(2);
      const actBefore = (t.active_before_cents / 100.0).toFixed(2);
      const actAfter = (t.active_after_cents / 100.0).toFixed(2);
      const protBefore = (t.protected_before_cents / 100.0).toFixed(2);
      const protAfter = (t.protected_after_cents / 100.0).toFixed(2);
      const totAfter = (t.total_after_cents / 100.0).toFixed(2);

      let tagClass = 'badge-tag-initial';
      if (t.event_type.includes('MILESTONE')) tagClass = 'status-pill status-running';
      else if (t.event_type.includes('RESERVE')) tagClass = 'status-pill mode-paper';
      else if (t.event_type.includes('LOSS')) tagClass = 'status-pill status-stopped';
      else if (t.event_type.includes('PROFIT')) tagClass = 'status-pill status-running';

      const timeStr = formatISTTime(t.created_at_ts);

      return `
        <tr>
          <td><code>${t.tx_id}</code></td>
          <td><span class="${tagClass}">${t.event_type}</span></td>
          <td><strong>$${amtUsd}</strong></td>
          <td>$${actBefore} &rarr; <strong>$${actAfter}</strong></td>
          <td>$${protBefore} &rarr; <strong>$${protAfter}</strong></td>
          <td><strong>$${totAfter}</strong></td>
          <td><code>${t.trading_day || '--'}</code></td>
          <td>${timeStr}</td>
          <td>${escapeHtml(t.reason)}</td>
        </tr>
      `;
    }).join('');
  }

  if (currentArchiveTab === 'capital-tx') {
    updatePaginationControls(total, page, totalPages, 'transactions');
  }
}

function renderArchiveLogs(logs) {
  if (logs !== undefined) {
    allArchiveLogs = logs || [];
  }
  const container = document.getElementById('archive-log-stream');
  if (!container) return;

  const total = allArchiveLogs.length;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (archivePages.logs > totalPages) archivePages.logs = totalPages;
  if (archivePages.logs < 1) archivePages.logs = 1;

  const page = archivePages.logs;
  const start = (page - 1) * PAGE_SIZE;
  const pageItems = allArchiveLogs.slice(start, start + PAGE_SIZE);

  if (total === 0) {
    container.innerHTML = '<div class="log-empty">No execution logs recorded yet.</div>';
  } else {
    container.innerHTML = pageItems.map(l => {
      const tagClass = getTagClass(l.level);
      const timeStr = l.timestamp ? formatISTTime(l.timestamp) : (l.time_str || '--');
      return `
        <div class="log-entry">
          <span class="log-time">${timeStr}</span>
          <span class="log-tag ${tagClass}">${escapeHtml(l.level)}</span>
          <span class="log-message">${escapeHtml(l.message)}</span>
        </div>
      `;
    }).join('');
  }

  if (currentArchiveTab === 'logs') {
    updatePaginationControls(total, page, totalPages, 'logs');
  }
}

function updatePaginationControls(total, page, totalPages, label) {
  const infoEl = document.getElementById('pagination-info');
  const indEl = document.getElementById('page-indicator');
  const btnFirst = document.getElementById('btn-page-first');
  const btnPrev = document.getElementById('btn-page-prev');
  const btnNext = document.getElementById('btn-page-next');
  const btnLast = document.getElementById('btn-page-last');

  const start = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const end = Math.min(page * PAGE_SIZE, total);

  if (infoEl) {
    infoEl.textContent = `Showing ${start}–${end} of ${total} ${label}`;
  }
  if (indEl) {
    indEl.textContent = `Page ${page} of ${totalPages}`;
  }

  if (btnFirst) btnFirst.disabled = (page <= 1);
  if (btnPrev) btnPrev.disabled = (page <= 1);
  if (btnNext) btnNext.disabled = (page >= totalPages || total === 0);
  if (btnLast) btnLast.disabled = (page >= totalPages || total === 0);
}

function goToPage(action) {
  const tab = currentArchiveTab;
  let totalItems = 0;
  if (tab === 'orders') totalItems = allTrades.length;
  else if (tab === 'capital-tx') totalItems = allCapitalTxs.length;
  else if (tab === 'logs') totalItems = allArchiveLogs.length;

  const totalPages = Math.max(1, Math.ceil(totalItems / PAGE_SIZE));
  let curr = archivePages[tab] || 1;

  if (action === 'first') {
    curr = 1;
  } else if (action === 'prev') {
    curr = Math.max(1, curr - 1);
  } else if (action === 'next') {
    curr = Math.min(totalPages, curr + 1);
  } else if (action === 'last') {
    curr = totalPages;
  }

  archivePages[tab] = curr;
  if (tab === 'orders') renderTradesTable();
  else if (tab === 'capital-tx') renderCapitalTransactions();
  else if (tab === 'logs') renderArchiveLogs();
}

function switchArchiveTab(tabName) {
  currentArchiveTab = tabName;
  const ordersTab = document.getElementById('tab-orders');
  const capTxTab = document.getElementById('tab-capital-tx');
  const logsTab = document.getElementById('tab-archive-logs');
  const ordersView = document.getElementById('view-orders-table');
  const capView = document.getElementById('view-capital-table');
  const logsView = document.getElementById('view-archive-logs');

  if (ordersTab) ordersTab.classList.toggle('active', tabName === 'orders');
  if (capTxTab) capTxTab.classList.toggle('active', tabName === 'capital-tx');
  if (logsTab) logsTab.classList.toggle('active', tabName === 'logs');

  if (ordersView) ordersView.style.display = (tabName === 'orders' ? 'block' : 'none');
  if (capView) capView.style.display = (tabName === 'capital-tx' ? 'block' : 'none');
  if (logsView) logsView.style.display = (tabName === 'logs' ? 'block' : 'none');

  if (tabName === 'orders') {
    renderTradesTable();
  } else if (tabName === 'capital-tx') {
    if (allCapitalTxs.length === 0) fetchCapitalTransactions();
    else renderCapitalTransactions();
  } else if (tabName === 'logs') {
    if (allArchiveLogs.length === 0) fetchArchiveLogs();
    else renderArchiveLogs();
  }
}

async function triggerReconcile() {
  try {
    const res = await fetch('/api/capital/reconcile', { method: 'POST' });
    const result = await res.json();
    console.log('Reconciliation result:', result);
    fetchCapital();
    fetchCapitalTransactions();
  } catch (err) {
    alert('Failed to trigger reconciliation: ' + err.message);
  }
}
