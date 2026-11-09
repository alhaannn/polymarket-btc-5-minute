/**
 * Polymarket Workstation Client Logic
 * Handles real-time polling, gauge animation, tab switching, and bot controls.
 */

const API_BASE = window.location.pathname.startsWith('/paper') ? '/paper' : '';

let authConfig = { authenticated: false, auth_enabled: false, pin_required: false, username: '' };
let pendingAction = null;
let dashState = null;

async function authFetch(url, options = {}) {
  const fullUrl = url.startsWith('/') ? `${API_BASE}${url}` : url;
  const res = await fetch(fullUrl, options);
  if (res.status === 401) {
    window.location.href = `${API_BASE}/login`;
    return res;
  }
  return res;
}

async function checkAuthStatus() {
  try {
    const res = await fetch(`${API_BASE}/api/auth/me`);
    if (res.status === 401) {
      window.location.href = `${API_BASE}/login`;
      return;
    }
    if (res.ok) {
      authConfig = await res.json();
      const logoutBtn = document.getElementById('btn-auth-logout');
      if (logoutBtn) {
        logoutBtn.style.display = authConfig.auth_enabled ? 'inline-block' : 'none';
      }
    }
  } catch (err) {
    console.debug('Auth check error:', err);
  }
}

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

let currentSchedule = null;
let lastScheduleReceiptTs = 0;

document.addEventListener('DOMContentLoaded', () => {
  checkAuthStatus();
  initPolling();
  updateClockIST();
  setInterval(updateClockIST, 1000);
});

function updateClockIST() {
  const now = new Date();
  const timeStr = now.toLocaleTimeString('en-GB', { timeZone: 'Asia/Kolkata', hour12: false });
  const el = document.getElementById('clock-time');
  if (el) el.textContent = timeStr;
  tickScheduleTimer();
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
    const res = await authFetch('/api/logs?limit=1000');
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
    const res = await authFetch('/api/state');
    if (!res.ok) throw new Error('State fetch failed');
    const data = await res.json();
    renderState(data);
    if (data && data.capital) {
      renderCapital(data.capital);
    }
  } catch (err) {
    console.debug('State poll error:', err);
    setFeedStatusDisconnected();
  }
}

async function fetchPerformance() {
  try {
    const res = await authFetch('/api/performance');
    if (!res.ok) throw new Error('Performance fetch failed');
    performanceData = await res.json();
    renderPerformanceTab();
  } catch (err) {
    console.debug('Perf poll error:', err);
  }
}

async function fetchTrades() {
  try {
    const res = await authFetch('/api/trades?limit=1000');
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

function formatDurationSec(sec) {
  const s = Math.max(0, Math.round(sec));
  const hours = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  if (hours > 0) {
    return `${hours}h ${String(mins).padStart(2, '0')}m ${String(secs).padStart(2, '0')}s`;
  } else if (mins > 0) {
    return `${mins}m ${String(secs).padStart(2, '0')}s`;
  } else {
    return `${secs}s`;
  }
}

function renderSchedule(sched) {
  if (!sched) return;
  currentSchedule = sched;
  lastScheduleReceiptTs = Date.now();

  const isWorking = !!sched.is_working;

  // 1. Header Pill
  const schedPill = document.getElementById('pill-schedule-status');
  const schedText = document.getElementById('text-schedule-status');
  if (schedPill && schedText) {
    if (isWorking) {
      schedPill.className = 'status-pill status-working';
      schedText.textContent = `WORKING • Active ${sched.working_elapsed_formatted}`;
    } else {
      schedPill.className = 'status-pill status-resting';
      schedText.textContent = `RESTING • Starts in ${sched.time_left_to_start_formatted}`;
    }
  }

  // 2. Banner Badge
  const badgeEl = document.getElementById('badge-work-rest');
  const badgeText = document.getElementById('text-work-rest-badge');
  if (badgeEl && badgeText) {
    if (isWorking) {
      badgeEl.className = 'schedule-status-badge badge-working';
      badgeText.textContent = 'WORKING (ACTIVE)';
    } else {
      badgeEl.className = 'schedule-status-badge badge-resting';
      badgeText.textContent = sched.status || 'RESTING';
    }
  }

  // 3. Window & Reason Tags
  const windowTag = document.getElementById('tag-schedule-window');
  if (windowTag) {
    windowTag.textContent = sched.prime_hours_only ? `PRIME HOURS: ${sched.schedule_window}` : '24/7 CONTINUOUS MODE';
  }

  const reasonDesc = document.getElementById('desc-schedule-reason');
  if (reasonDesc) {
    reasonDesc.textContent = sched.reason || '';
  }

  // 4. Timer Box
  const timerLbl = document.getElementById('lbl-schedule-timer');
  const timerVal = document.getElementById('val-schedule-timer');
  const timerSub = document.getElementById('sub-schedule-timer');
  if (timerLbl && timerVal && timerSub) {
    if (isWorking) {
      timerLbl.textContent = 'ACTIVE WORK DURATION';
      timerVal.textContent = sched.working_elapsed_formatted;
      timerSub.textContent = `Closes at ${sched.session_end_str} (${sched.working_remaining_formatted} left)`;
    } else {
      timerLbl.textContent = 'TIME UNTIL WORK STARTS';
      timerVal.textContent = sched.time_left_to_start_formatted;
      timerSub.textContent = `Opens at ${sched.next_start_str || '13:30:00 IST'}`;
    }
  }

  // 5. Session PnL & Trades
  const pnlVal = document.getElementById('val-schedule-session-pnl');
  const pnlSub = document.getElementById('sub-schedule-session-trades');
  if (pnlVal && pnlSub) {
    const pnl = Number(sched.session_pnl_usd || 0);
    const sign = pnl > 0 ? '+' : '';
    pnlVal.textContent = `${sign}$${pnl.toFixed(2)}`;
    pnlVal.className = 'schedule-metric-value ' + (pnl > 0 ? 'text-green' : pnl < 0 ? 'text-red' : 'pnl-neutral');

    const trades = sched.session_trades_count || 0;
    const wins = sched.session_wins || 0;
    const losses = sched.session_losses || 0;
    if (trades === 0) {
      pnlSub.textContent = isWorking ? 'No trades yet in this active session' : '0 trades (waiting for next session)';
    } else {
      pnlSub.textContent = `${trades} trade${trades > 1 ? 's' : ''} (${wins}W / ${losses}L) since session start`;
    }
  }
}

function tickScheduleTimer() {
  if (!currentSchedule || !lastScheduleReceiptTs) return;
  const elapsedSec = (Date.now() - lastScheduleReceiptTs) / 1000.0;
  const isWorking = !!currentSchedule.is_working;

  const timerVal = document.getElementById('val-schedule-timer');
  const schedText = document.getElementById('text-schedule-status');

  if (isWorking) {
    const curElapsed = (currentSchedule.working_elapsed_sec || 0) + elapsedSec;
    const formatted = formatDurationSec(curElapsed);
    if (timerVal) timerVal.textContent = formatted;
    if (schedText) schedText.textContent = `WORKING • Active ${formatted}`;
  } else {
    const curLeft = Math.max(0, (currentSchedule.time_left_to_start_sec || 0) - elapsedSec);
    const formatted = formatDurationSec(curLeft);
    if (timerVal) timerVal.textContent = formatted;
    if (schedText) schedText.textContent = `RESTING • Starts in ${formatted}`;
  }
}

function renderState(state) {
  if (!state) return;
  dashState = state;

  // 0. Render Schedule (Working / Resting & Session PnL)
  if (state.schedule) {
    renderSchedule(state.schedule);
  }

  // 0.5 Execution Mode (Paper vs Live)
  const modePill = document.getElementById('pill-paper-mode');
  if (modePill) {
    if (state.execution_mode === 'live') {
      modePill.className = 'status-pill mode-live';
      modePill.innerHTML = '<span class="status-dot"></span> LIVE TRADING';
      modePill.title = 'Live real-money trading mode on Polymarket CLOB.';
      if (document.title.includes('Paper')) {
        document.title = document.title.replace('Paper', 'Live');
      }
      const titleEl = document.querySelector('.brand-title');
      if (titleEl && !titleEl.textContent.includes('Live')) {
        titleEl.textContent = 'Polymarket 5-Minute Live Trading Workstation';
      }
    } else {
      modePill.className = 'status-pill mode-paper';
      modePill.innerHTML = '<span class="status-dot"></span> PAPER SIMULATION';
      modePill.title = 'Paper trading mode only. No live keys or real orders.';
    }
  }

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

  const elapsed = Math.min(300.0, Math.max(0.0, state.elapsed_sec));

  // Dynamic Phase Badge (4 Stages)
  const phaseBadge = document.getElementById('badge-market-phase');
  if (phaseBadge) {
    if (elapsed < 180.0) {
      phaseBadge.className = 'phase-badge phase-waiting';
      phaseBadge.textContent = `WAITING PHASE (0 - 180s) • ${(180.0 - elapsed).toFixed(0)}s left`;
    } else if (elapsed < 210.0) {
      phaseBadge.className = 'phase-badge phase-snipe';
      phaseBadge.textContent = `INITIAL SNIPE ACTIVE (180 - 210s) • ${(210.0 - elapsed).toFixed(0)}s left`;
    } else if (elapsed < 280.0) {
      phaseBadge.className = 'phase-badge phase-reversal';
      phaseBadge.textContent = `REVERSAL WINDOW (210 - 280s) • ${(280.0 - elapsed).toFixed(0)}s left`;
    } else if (elapsed < 300.0) {
      phaseBadge.className = 'phase-badge phase-lockout';
      phaseBadge.textContent = `SETTLEMENT LOCKOUT (280 - 300s) • ${(300.0 - elapsed).toFixed(0)}s left`;
    } else {
      phaseBadge.className = 'phase-badge phase-closed';
      phaseBadge.textContent = 'MARKET CLOSED / SETTLING (>= 300s)';
    }
  }

  // 4-Stage Progress Bars (300s cycle)
  // Stage 1: 0s - 180s (Observation: 180s duration = 60.0% of total)
  // Stage 2: 180s - 210s (Initial Snipe: 30s duration = 10.0% of total)
  // Stage 3: 210s - 280s (Reversal Recovery: 70s duration = 23.33% of total)
  // Stage 4: 280s - 300s (Pre-Settlement Lockout: 20s duration = 6.67% of total)
  const waitingElapsed = Math.min(180.0, elapsed);
  const snipeElapsed = Math.max(0.0, Math.min(30.0, elapsed - 180.0));
  const revElapsed = Math.max(0.0, Math.min(70.0, elapsed - 210.0));
  const lockElapsed = Math.max(0.0, Math.min(20.0, elapsed - 280.0));

  const waitingPct = (waitingElapsed / 300.0) * 100.0;
  const snipePct = (snipeElapsed / 300.0) * 100.0;
  const revPct = (revElapsed / 300.0) * 100.0;
  const lockPct = (lockElapsed / 300.0) * 100.0;

  const barWaiting = document.getElementById('progress-bar-waiting');
  const barSnipe = document.getElementById('progress-bar-snipe');
  const barRev = document.getElementById('progress-bar-reversal');
  const barLock = document.getElementById('progress-bar-lockout');

  if (barWaiting) barWaiting.style.width = `${waitingPct}%`;
  if (barSnipe) barSnipe.style.width = `${snipePct}%`;
  if (barRev) barRev.style.width = `${revPct}%`;
  if (barLock) barLock.style.width = `${lockPct}%`;

  const elTime = document.getElementById('val-elapsed-time');
  if (elTime) elTime.textContent = `${elapsed.toFixed(1)}s`;

  const remaining = typeof state.remaining_sec === 'number' ? state.remaining_sec : Math.max(0.0, 300.0 - elapsed);
  const remTime = document.getElementById('val-remaining-time');
  if (remTime) remTime.textContent = `${remaining.toFixed(1)}s`;

  // Window Status Card with Exact Stage Context
  const winStatus = document.getElementById('val-window-status');
  if (winStatus) {
    if (elapsed < 180.0) {
      winStatus.className = 'stat-value status-badge-inactive';
      winStatus.textContent = `OBSERVING (${(180.0 - elapsed).toFixed(0)}s left)`;
    } else if (elapsed < 210.0) {
      if (state.initial_order_triggered) {
        winStatus.className = 'stat-value status-badge-active';
        winStatus.textContent = `INITIAL PLACED (${state.initial_side || 'UP/DN'})`;
      } else {
        winStatus.className = 'stat-value status-badge-active';
        winStatus.textContent = 'SNIPE OPEN (89%–91%)';
      }
    } else if (elapsed < 280.0) {
      if (state.reversal_order_triggered) {
        winStatus.className = 'stat-value status-badge-active';
        winStatus.textContent = 'REVERSAL EXECUTED';
      } else if (state.initial_order_triggered) {
        winStatus.className = 'stat-value status-badge-active';
        winStatus.textContent = 'RECOVERY MONITORING (85%–95%)';
      } else {
        winStatus.className = 'stat-value status-badge-inactive';
        winStatus.textContent = 'SNIPE EXPIRED (PASS)';
      }
    } else {
      winStatus.className = 'stat-value status-badge-inactive';
      winStatus.textContent = 'SETTLEMENT (LOCKED)';
    }
  }

  const freshEl = document.getElementById('val-data-freshness');
  if (freshEl) {
    const staleness = typeof state.staleness_sec === 'number' ? state.staleness_sec : 0.0;
    freshEl.textContent = staleness < 1 ? '< 1s ago' : `${staleness.toFixed(1)}s ago`;
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
      if (state.is_trading_allowed && state.up_prob >= 0.85 && state.up_prob <= 0.90) {
        badgeUp.className = 'outcome-state-badge badge-qualified-initial';
        badgeUp.textContent = 'INITIAL RANGE QUALIFIED [85% - 90%]';
      } else if (state.initial_side === 'Up' && state.up_bid !== null && state.up_bid !== undefined && state.up_bid <= 0.30) {
        badgeUp.className = 'outcome-state-badge badge-qualified-reversal';
        badgeUp.textContent = 'STOP-LOSS TRIGGER ZONE [≤ 30%]';
      } else if (state.initial_side === 'Down' && state.up_prob >= 0.85) {
        badgeUp.className = 'outcome-state-badge badge-qualified-reversal';
        badgeUp.textContent = 'OPPOSING SURGE [EARLY EXIT ZONE]';
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
      if (state.is_trading_allowed && state.down_prob >= 0.85 && state.down_prob <= 0.90) {
        badgeDown.className = 'outcome-state-badge badge-qualified-initial';
        badgeDown.textContent = 'INITIAL RANGE QUALIFIED [85% - 90%]';
      } else if (state.initial_side === 'Down' && state.down_bid !== null && state.down_bid !== undefined && state.down_bid <= 0.30) {
        badgeDown.className = 'outcome-state-badge badge-qualified-reversal';
        badgeDown.textContent = 'STOP-LOSS TRIGGER ZONE [≤ 30%]';
      } else if (state.initial_side === 'Up' && state.down_prob >= 0.85) {
        badgeDown.className = 'outcome-state-badge badge-qualified-reversal';
        badgeDown.textContent = 'OPPOSING SURGE [EARLY EXIT ZONE]';
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
  const titleInit = document.getElementById('title-step-initial');
  const descInit = document.getElementById('desc-step-initial');
  const stepRev = document.getElementById('step-reversal');
  const titleRev = document.getElementById('title-step-reversal');
  const descRev = document.getElementById('desc-step-reversal');

  const curBudget = (state.capital && state.capital.current_initial_budget) || 2;
  if (titleInit) titleInit.textContent = `Initial Entry ($${curBudget}.00)`;
  if (titleRev) titleRev.textContent = 'Early Exit Stop-Loss (30%)';

  if (state.initial_order_triggered) {
    if (stepInit) stepInit.className = 'exec-step-item step-done';
    if (descInit) descInit.textContent = `Filled: ${state.initial_side} ($${curBudget}.00)`;

    const hadEarlyExit = state.logs && state.logs.some(l => l.level === 'EARLY_EXIT');
    if (hadEarlyExit) {
      if (stepRev) stepRev.className = 'exec-step-item step-done';
      if (descRev) descRev.textContent = 'Early Exit Executed (Salvaged ~30%)';
    } else {
      if (stepRev) stepRev.className = 'exec-step-item step-active';
      if (descRev) descRev.textContent = `Active: Stop-loss armed (liquidate if ${state.initial_side} ≤ 0.30)`;
    }
  } else {
    if (stepInit) {
      stepInit.className = state.is_trading_allowed ? 'exec-step-item step-active' : 'exec-step-item';
    }
    if (descInit) {
      descInit.textContent = state.is_trading_allowed ? 'Active: scanning 85%–90% (Price ≤ 0.900)' : 'Waiting: gate opens at 180s';
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

  // 8. Multi-Market Fleet & Grid Rendering
  if (state.multi_market) {
    renderMultiMarket(state.multi_market);
  }
}

function renderMultiMarket(mm) {
  if (!mm) return;

  // 1. Fleet Banner Metrics
  const fleetTotalEl = document.getElementById('val-fleet-total');
  if (fleetTotalEl && mm.fleet_equity_usd !== undefined) {
    fleetTotalEl.textContent = `$${mm.fleet_equity_usd.toFixed(2)}`;
  }

  const fleetSweptEl = document.getElementById('val-fleet-swept');
  const pillSwept = document.getElementById('pill-fleet-swept');
  if (fleetSweptEl && mm.total_swept_usd !== undefined) {
    fleetSweptEl.textContent = `$${mm.total_swept_usd.toFixed(2)}`;
    if (pillSwept) pillSwept.textContent = `SWEPT: $${mm.total_swept_usd.toFixed(2)} USDC`;
  }

  const fleetStatusEl = document.getElementById('val-fleet-status');
  const fleetStatusSub = document.getElementById('val-fleet-status-sub');
  const pillActive = document.getElementById('pill-fleet-active');
  if (fleetStatusEl) {
    const act = mm.active_cells_count !== undefined ? mm.active_cells_count : 6;
    const hlt = mm.halted_cells_count || 0;
    fleetStatusEl.textContent = `${act}/6 HEALTHY`;
    if (fleetStatusSub) fleetStatusSub.textContent = `Halted Cells: ${hlt} (Bulkhead Ready)`;
    if (pillActive) pillActive.textContent = `${act}/6 ACTIVE`;
  }

  const pillArbiter = document.getElementById('pill-fleet-arbiter');
  if (pillArbiter && mm.arbiter) {
    pillArbiter.textContent = `ARBITER: ${mm.arbiter.status_text || '0/1 SLOTS (STANDBY)'}`;
  }

  // Radar
  const radar = mm.next_harvest_radar;
  if (radar) {
    const rLeader = document.getElementById('val-radar-leader');
    const rDesc = document.getElementById('val-radar-desc');
    if (rLeader) {
      rLeader.textContent = `${radar.leader_asset || 'NONE'}: $${(radar.leader_equity || 0).toFixed(2)}`;
    }
    if (rDesc) {
      rDesc.textContent = `$${(radar.distance_to_milestone || 0).toFixed(2)} to $100 Sweep (Est: +$${(radar.est_sweep_amount_usd || 0).toFixed(2)})`;
    }
  }

  // 2. Render 6 Asset Cards
  const gridContainer = document.getElementById('container-mm-matrix-grid');
  if (gridContainer && mm.matrix) {
    const order = ['btc', 'eth', 'sol', 'doge', 'xrp', 'bnb'];
    const cardsHtml = order.map(assetKey => {
      const item = mm.matrix[assetKey] || {};
      const assetUpper = (item.asset || assetKey).toUpperCase();
      const strikeStr = item.strike_k ? `$${Number(item.strike_k).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 4})}` : '--';
      const spotStr = item.spot_p ? `$${Number(item.spot_p).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 4})}` : '--';
      const diffVal = item.diff || 0.0;
      const diffStr = diffVal >= 0 ? `+${diffVal.toFixed(2)}` : diffVal.toFixed(2);
      const diffColor = diffVal >= 0 ? 'var(--color-green)' : 'var(--color-red)';

      const upProb = item.up_prob !== undefined ? item.up_prob : 50.0;
      const downProb = item.down_prob !== undefined ? item.down_prob : 50.0;

      const cellCap = item.cell_equity !== undefined ? item.cell_equity : 12.0;
      const progPct = item.progress_pct !== undefined ? item.progress_pct : ((cellCap / 100) * 100).toFixed(1);
      const initStake = item.initial_budget || 1;
      const revStake = item.reversal_budget || 11;

      const status = item.cell_status || 'ACTIVE';
      let cardClass = 'mm-asset-card card-active';
      let badgeClass = 'mm-card-badge badge-active';
      let badgeText = 'ACTIVE';

      if (status === 'HALTED_DEPLETED') {
        cardClass = 'mm-asset-card card-halted';
        badgeClass = 'mm-card-badge badge-halted';
        badgeText = 'HALTED (AWAITING REVIVER)';
      } else if (status === 'TRIGGER_ZONE') {
        cardClass = 'mm-asset-card card-trigger';
        badgeClass = 'mm-card-badge badge-trigger';
        badgeText = 'TRIGGER ZONE (89-91%)';
      } else if (status === 'POSITION_ACTIVE') {
        cardClass = 'mm-asset-card card-position';
        badgeClass = 'mm-card-badge badge-position';
        badgeText = `POSITION ACTIVE ($${initStake})`;
      } else if (status === 'REVERSAL_ACTIVE') {
        cardClass = 'mm-asset-card card-position';
        badgeClass = 'mm-card-badge badge-reversal';
        badgeText = `REVERSAL HEDGE ($${revStake})`;
      } else if (status === 'TARGET_GRADUATED') {
        cardClass = 'mm-asset-card card-graduated';
        badgeClass = 'mm-card-badge badge-graduated';
        badgeText = 'GRADUATING ($100)';
      }

      return `
        <div class="${cardClass}" id="card-asset-${assetKey}">
          <div class="mm-card-header">
            <div class="mm-card-title-group">
              <span class="mm-card-symbol">${assetUpper}/USD</span>
              <span style="font-size: 11px; color: var(--text-muted); font-family: var(--font-mono);">5M</span>
            </div>
            <span class="${badgeClass}">${badgeText}</span>
          </div>

          <div class="mm-card-prices">
            <div>Strike (K): <span class="mm-price-val">${strikeStr}</span></div>
            <div>Spot (Pt): <span class="mm-price-val">${spotStr}</span> (<span style="color: ${diffColor}; font-weight: 600;">${diffStr}</span>)</div>
          </div>

          <div class="mm-prob-container">
            <div class="mm-prob-labels">
              <span style="color: var(--color-green);">UP: ${upProb.toFixed(1)}%</span>
              <span style="color: var(--color-red);">DOWN: ${downProb.toFixed(1)}%</span>
            </div>
            <div class="mm-prob-track">
              <div class="mm-prob-fill" style="width: ${Math.min(100, Math.max(0, upProb))}%;"></div>
            </div>
          </div>

          <div class="mm-card-financials">
            <div>
              <div class="mm-fin-capital">$${Number(cellCap).toFixed(2)}</div>
              <div class="mm-fin-progress">Progress: ${progPct}% to $100</div>
            </div>
            <div style="text-align: right;">
              <div class="mm-fin-stake">$${initStake} Init / $${revStake} Rev</div>
              <div style="font-size: 10px; color: var(--text-muted); font-family: var(--font-mono);">${item.wins || 0}W - ${item.losses || 0}L</div>
            </div>
          </div>
        </div>
      `;
    }).join('');

    gridContainer.innerHTML = cardsHtml;
  }

  // 3. Render Per-Asset Table
  const tbodyAsset = document.getElementById('tbody-asset-ledger');
  if (tbodyAsset && mm.matrix) {
    const order = ['btc', 'eth', 'sol', 'doge', 'xrp', 'bnb'];
    const rowsHtml = order.map(assetKey => {
      const item = mm.matrix[assetKey] || {};
      const assetUpper = (item.asset || assetKey).toUpperCase();
      const cap = Number(item.cell_equity !== undefined ? item.cell_equity : 12.0).toFixed(2);
      const initStake = item.initial_budget || 1;
      const revStake = item.reversal_budget || 11;
      const trades = item.trades || 0;
      const wins = item.wins || 0;
      const losses = item.losses || 0;
      const winRate = item.win_rate !== undefined ? `${item.win_rate.toFixed(1)}%` : '--';
      const grossP = Number(item.gross_profit || 0).toFixed(2);
      const grossL = Number(item.gross_loss || 0).toFixed(2);
      const netPnL = Number(item.net_pnl || 0).toFixed(2);
      const pnlColor = Number(netPnL) >= 0 ? 'var(--color-green)' : 'var(--color-red)';
      const prog = item.progress_pct !== undefined ? `${item.progress_pct}%` : `${((Number(cap)/100)*100).toFixed(1)}%`;
      const status = item.cell_status || 'ACTIVE';

      return `
        <tr>
          <td><strong>${assetUpper}</strong></td>
          <td style="font-family: var(--font-mono); font-weight: 600;">$${cap}</td>
          <td style="font-family: var(--font-mono);">$${initStake} / $${revStake}</td>
          <td>${trades}</td>
          <td>${wins} / ${losses}</td>
          <td style="font-weight: 600;">${winRate}</td>
          <td style="color: var(--color-green);">+$${grossP}</td>
          <td style="color: var(--color-red);">-$${grossL}</td>
          <td style="color: ${pnlColor}; font-weight: 700;">${Number(netPnL) >= 0 ? '+' : ''}$${netPnL}</td>
          <td>
            <div style="display: flex; align-items: center; gap: 6px;">
              <div style="flex: 1; height: 6px; background: var(--border-light); border-radius: 3px; overflow: hidden;">
                <div style="width: ${prog}; height: 100%; background: #10b981;"></div>
              </div>
              <span style="font-size: 11px; font-family: var(--font-mono);">${prog}</span>
            </div>
          </td>
          <td><span class="mm-card-badge ${status === 'HALTED_DEPLETED' ? 'badge-halted' : 'badge-active'}">${status}</span></td>
        </tr>
      `;
    }).join('');

    tbodyAsset.innerHTML = rowsHtml;
  }

  // 4. Render Binance Sweeper Ledger
  const tbodySweeper = document.getElementById('tbody-sweeper-ledger');
  if (tbodySweeper) {
    const harvests = mm.recent_harvests || [];
    if (harvests.length > 0) {
      tbodySweeper.innerHTML = harvests.map(h => {
        const timeStr = h.timestamp_ts ? formatISTDateTime(h.timestamp_ts) : '--';
        const revivedStr = h.halted_revived_assets && h.halted_revived_assets.length > 0 ? h.halted_revived_assets.join(', ') : 'None (0 Halted)';
        return `
          <tr>
            <td style="font-family: var(--font-mono);">${timeStr}</td>
            <td><strong>${h.winning_asset}</strong></td>
            <td><span class="status-pill mode-paper">$100.00 MILESTONE</span></td>
            <td>${revivedStr}</td>
            <td style="font-family: var(--font-mono); font-weight: 700; color: #d97706;">+$${Number(h.sweep_amount_usd).toFixed(2)} USDC</td>
            <td><span class="status-pill feed-live">CONFIRMED (POLYGON POS)</span></td>
          </tr>
        `;
      }).join('');
    }
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
  const curInitStake = (latestState && latestState.capital && latestState.capital.current_initial_budget) || 2;
  if (splitEl) splitEl.textContent = `${metrics.initial_trades} Initial ($${curInitStake}) • Pure Early Exit`;

  // Model & markets
  const revCountEl = document.getElementById('val-reversals-count');
  const revLabelEl = document.getElementById('label-reversals-count');
  const mktCountEl = document.getElementById('val-markets-count');
  if (revLabelEl) revLabelEl.textContent = 'Protected Trade Model';
  if (revCountEl) revCountEl.textContent = 'Pure Early Exit';
  if (mktCountEl) mktCountEl.textContent = `${performanceData.markets_observed} Markets Observed (Stop-Loss ≤ 30%)`;
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
    const isLive = document.getElementById('pill-paper-mode')?.classList.contains('mode-live');
    tbody.innerHTML = `
      <tr class="table-row-empty">
        <td colspan="13">${isLive ? 'No live trades recorded yet in this session.' : 'No paper trades recorded yet in this session.'}</td>
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

      let typeBadge = '';
      if (t.winner === 'EXIT_EARLY' || t.signal_type === 'EARLY_EXIT') {
        typeBadge = `<span class="badge-tag-early-exit">EARLY EXIT 30%</span>`;
      } else if (t.signal_type === 'REVERSAL') {
        typeBadge = `<span class="badge-tag-reversal">REVERSAL</span>`;
      } else {
        const stakeVal = t.stake_usd ? Math.round(t.stake_usd) : 2;
        typeBadge = `<span class="badge-tag-initial">INITIAL $${stakeVal}</span>`;
      }

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

function sendControl(action) {
  if (authConfig && authConfig.pin_required) {
    openPinModal(action);
  } else {
    executeControl(action);
  }
}

async function executeControl(action, pin = '') {
  try {
    const payload = { action };
    if (pin) payload.pin = pin;
    const res = await authFetch('/api/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const result = await res.json();
    if (!res.ok) {
      throw new Error(result.message || 'Action failed');
    }
    console.log('Control response:', result);
    fetchState();
    return true;
  } catch (err) {
    if (!pin) {
      alert('Failed to send control command: ' + err.message);
    }
    throw err;
  }
}

function openPinModal(action) {
  pendingAction = action;
  const overlay = document.getElementById('pin-modal-overlay');
  const prompt = document.getElementById('pin-modal-prompt');
  const input = document.getElementById('action-pin-input');
  const errBox = document.getElementById('pin-modal-error');
  if (errBox) {
    errBox.textContent = '';
    errBox.classList.add('hidden');
  }
  if (input) {
    input.value = '';
  }
  if (prompt) {
    prompt.innerHTML = `You are requesting to change bot state to <strong>${action.toUpperCase()}</strong>. Enter your 4-digit Security PIN to confirm:`;
  }
  if (overlay) {
    overlay.classList.remove('hidden');
    setTimeout(() => {
      if (input) input.focus();
    }, 100);
  }
}

function closePinModal() {
  pendingAction = null;
  const overlay = document.getElementById('pin-modal-overlay');
  if (overlay) overlay.classList.add('hidden');
}

function handlePinKey(event) {
  if (event.key === 'Enter') {
    submitPinAction();
  } else if (event.key === 'Escape') {
    closePinModal();
  }
}

async function submitPinAction() {
  const input = document.getElementById('action-pin-input');
  const errBox = document.getElementById('pin-modal-error');
  const btn = document.getElementById('btn-confirm-action-pin');
  const pin = input ? input.value.trim() : '';

  if (!pin) {
    if (errBox) {
      errBox.textContent = 'Please enter your Security PIN';
      errBox.classList.remove('hidden');
    }
    return;
  }

  if (btn) btn.disabled = true;
  try {
    await executeControl(pendingAction, pin);
    closePinModal();
  } catch (err) {
    if (errBox) {
      errBox.textContent = err.message || 'Invalid Security PIN';
      errBox.classList.remove('hidden');
    }
    if (input) {
      input.value = '';
      input.focus();
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function logoutUser() {
  try {
    await fetch(`${API_BASE}/api/auth/logout`, { method: 'POST' });
  } catch (e) {
    console.debug('Logout error:', e);
  }
  window.location.href = `${API_BASE}/login`;
}

function clearLocalLogs() {
  const container = document.getElementById('log-stream');
  if (container) {
    container.innerHTML = '<div class="log-empty">Log cleared locally.</div>';
  }
}

function exportTradesCSV() {
  window.open(`${API_BASE}/api/trades/csv`, '_blank');
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function fetchCapital() {
  try {
    const res = await authFetch('/api/capital');
    if (!res.ok) throw new Error('Capital fetch failed');
    const data = await res.json();
    renderCapital(data);
  } catch (err) {
    console.debug('Capital poll error:', err);
  }
}

function renderCapital(cap) {
  if (!cap) return;

  const isLive = (cap.execution_mode === 'live') || (typeof dashState !== 'undefined' && dashState && dashState.execution_mode === 'live');

  const totEq = Number(cap.total_equity !== undefined ? cap.total_equity : (cap.total_equity_cents ? cap.total_equity_cents / 100.0 : 0)) || 0;
  const actCap = Number(cap.active_capital !== undefined ? cap.active_capital : (cap.active_capital_cents ? cap.active_capital_cents / 100.0 : totEq)) || 0;
  const protCap = Number(cap.protected_capital !== undefined ? cap.protected_capital : 0) || 0;

  const totalEl = document.getElementById('val-cap-total');
  if (totalEl) totalEl.textContent = `$${totEq.toFixed(2)}`;

  const totalSubEl = document.getElementById('val-cap-total-sub');
  if (totalSubEl) {
    if (isLive) {
      if (totEq < 1.0) {
        totalSubEl.textContent = 'Live Wallet (Awaiting Deposit, Min $1.00)';
      } else {
        totalSubEl.textContent = 'Live Polymarket Collateral';
      }
    } else {
      totalSubEl.textContent = `Canonical Base: $11.00 (Current: $${totEq.toFixed(2)})`;
    }
  }

  const activeEl = document.getElementById('val-cap-active');
  const activeSubEl = document.getElementById('val-cap-active-sub');
  if (activeEl) {
    const spendable = cap.spendable_active_funds !== undefined ? Number(cap.spendable_active_funds) : actCap;
    activeEl.textContent = `$${spendable.toFixed(2)}`;
    if (activeSubEl) {
      const reserved = cap.active_funds_reserved !== undefined ? Number(cap.active_funds_reserved) : (Number(cap.open_exposure) || 0);
      if (isLive) {
        activeSubEl.textContent = `Reserved: $${reserved.toFixed(2)} | Live Available: $${spendable.toFixed(2)}`;
      } else {
        activeSubEl.textContent = `Reserved: $${reserved.toFixed(2)} | Total Active: $${actCap.toFixed(2)}`;
      }
    }
  }

  const protEl = document.getElementById('val-cap-protected');
  const protSubEl = document.getElementById('val-cap-protected-sub');
  if (protEl) {
    protEl.textContent = `$${protCap.toFixed(2)}`;
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

  // --- Dynamic Milestone & Tier Progression Engine ---
  const ms44 = Boolean(cap.milestone_44_activated);

  // Determine current tier (Option B Tiered Compounding: $2 / $3 / $4 / $5 / $8)
  let currentTierText = 'TIER 1 ($2 STAKE)';
  if (totEq >= 50.0) {
    currentTierText = 'TIER 5 ($8 STAKE)';
  } else if (totEq >= 35.0) {
    currentTierText = 'TIER 4 ($5 STAKE)';
  } else if (totEq >= 25.0) {
    currentTierText = 'TIER 3 ($4 STAKE)';
  } else if (totEq >= 15.0) {
    currentTierText = 'TIER 2 ($3 STAKE)';
  }

  // Determine milestone progression & targets
  let targetTotal = 15.0;
  let targetActive = 15.0;
  let prevTotal = 11.0;
  let nextMilestoneTitle = 'Next Milestone: Tier 2 ($15.00 Total)';
  let nextRewardText = 'Unlocks: <strong>$3 Initial Stakes</strong> (Tier 2, +50% Payout)';
  let activeMarkerId = 'marker-44';
  let pillText = 'TIER 1: $2 STAKE';
  let pillClass = 'status-pill mode-paper';

  if (!ms44 && totEq < 15.0) {
    targetTotal = 15.0;
    targetActive = 15.0;
    prevTotal = 11.0;
    nextMilestoneTitle = 'Next Milestone: Tier 2 ($15.00 Total)';
    nextRewardText = 'Unlocks: <strong>$3 Initial Stakes</strong> (Tier 2, +50% Payout)';
    activeMarkerId = 'marker-44';
    pillText = 'TIER 1: $2 STAKE';
    pillClass = 'status-pill mode-paper';
  } else if (totEq < 25.0) {
    targetTotal = 25.0;
    targetActive = 25.0;
    prevTotal = 15.0;
    nextMilestoneTitle = 'Next Milestone: Tier 3 ($25.00 Total)';
    nextRewardText = 'Unlocks: <strong>$4 Initial Stakes</strong> (Tier 3, +100% Payout)';
    activeMarkerId = 'marker-66';
    pillText = 'TIER 2: $3 STAKE';
    pillClass = 'status-pill status-running';
  } else if (totEq < 35.0) {
    targetTotal = 35.0;
    targetActive = 35.0;
    prevTotal = 25.0;
    nextMilestoneTitle = 'Next Milestone: Tier 4 ($35.00 Total)';
    nextRewardText = 'Unlocks: <strong>$5 Initial Stakes</strong> (Tier 4)';
    activeMarkerId = 'marker-88';
    pillText = 'TIER 3: $4 STAKE';
    pillClass = 'status-pill status-running';
  } else if (totEq < 50.0) {
    targetTotal = 50.0;
    targetActive = 50.0;
    prevTotal = 35.0;
    nextMilestoneTitle = 'Next Milestone: Tier 5 ($50.00 Total)';
    nextRewardText = 'Unlocks: <strong>$8 Initial Stakes</strong> (+300% Payout)';
    activeMarkerId = 'marker-100';
    pillText = 'TIER 4: $5 STAKE';
    pillClass = 'status-pill status-running';
  } else if (totEq < 100.0) {
    targetTotal = 100.0;
    targetActive = 50.0;
    prevTotal = 50.0;
    nextMilestoneTitle = 'Next Milestone: Century Club ($100.00 Total)';
    nextRewardText = 'Unlocks: <strong>10x Capital Return</strong> & Elite Tier';
    activeMarkerId = 'marker-100';
    pillText = 'TIER 5: $8 STAKE';
    pillClass = 'status-pill status-running';
  } else {
    targetTotal = Math.ceil(totEq / 50.0) * 50.0;
    targetActive = targetTotal / 2.0;
    prevTotal = targetTotal - 50.0;
    nextMilestoneTitle = `Next Milestone: Elite Tier ($${targetTotal.toFixed(0)} Total)`;
    nextRewardText = 'Unlocks: Extended Scale & Perpetual Compounding';
    activeMarkerId = 'marker-100';
    pillText = 'CENTURY CLUB: $100+ REACHED';
    pillClass = 'status-pill status-running';
  }

  // Calculate dynamic progress %
  const totalRange = Math.max(1, targetTotal - prevTotal);
  const currentProgress = Math.max(0, totEq - prevTotal);
  let progressPct = (currentProgress / totalRange) * 100;
  progressPct = Math.min(100, Math.max(0, progressPct));

  const remainingActive = Math.max(0, targetActive - actCap);

  // Update UI elements
  const msPill = document.getElementById('pill-cap-ms44');
  if (msPill) {
    msPill.className = pillClass;
    msPill.textContent = pillText;
  }

  const tierBadge = document.getElementById('badge-current-tier');
  if (tierBadge) tierBadge.textContent = currentTierText;

  const msTitle = document.getElementById('title-next-milestone');
  if (msTitle) msTitle.textContent = nextMilestoneTitle;

  const pctEl = document.getElementById('val-milestone-pct');
  if (pctEl) pctEl.textContent = `${progressPct.toFixed(1)}%`;

  const remainingEl = document.getElementById('val-milestone-remaining');
  if (remainingEl) {
    remainingEl.textContent = remainingActive > 0
      ? `($${remainingActive.toFixed(2)} active to unlock)`
      : `(Active target met: $${actCap.toFixed(2)})`;
  }

  const fillEl = document.getElementById('fill-milestone-progression');
  if (fillEl) fillEl.style.width = `${progressPct.toFixed(1)}%`;

  const statActive = document.getElementById('stat-active-target');
  if (statActive) {
    statActive.innerHTML = `Active Capital: <strong>$${actCap.toFixed(2)}</strong> / $${targetActive.toFixed(2)} Target`;
  }

  const statProtected = document.getElementById('stat-protected-harvest');
  if (statProtected) {
    statProtected.innerHTML = `Protected Vault: <strong>$${protCap.toFixed(2)}</strong> / $50.00 Weekly Cash Target`;
  }

  const statReward = document.getElementById('stat-next-reward');
  if (statReward) {
    statReward.innerHTML = `${nextRewardText}`;
  }

  // Update checkpoint markers on track
  const markers = [
    { id: 'marker-11', threshold: 11.0 },
    { id: 'marker-44', threshold: 44.0 },
    { id: 'marker-66', threshold: 66.0 },
    { id: 'marker-88', threshold: 88.0 },
    { id: 'marker-100', threshold: 100.0 }
  ];
  markers.forEach(m => {
    const el = document.getElementById(m.id);
    if (!el) return;
    if (totEq >= m.threshold || (m.threshold === 44.0 && ms44)) {
      el.className = 'marker marker-passed';
    } else if (m.id === activeMarkerId) {
      el.className = 'marker marker-active';
    } else {
      el.className = 'marker';
    }
  });


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
    budgetsEl.textContent = `$${cap.current_initial_budget || 2} Initial (Pure Early Exit: No Reversals)`;
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
        shutdownEl.textContent = `SHUTDOWN ACTIVE (${cap.daily_shutdown_reason || 'Daily loss limit'})`;
      }
      shutdownEl.style.color = 'var(--color-red)';
    } else {
      shutdownEl.textContent = `CLEAR (Dynamic Stop: 30% / min $6 • 3-Loss Pause)`;
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
    const res = await authFetch('/api/capital/transactions?limit=1000');
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
    const res = await authFetch('/api/capital/reconcile', { method: 'POST' });
    const result = await res.json();
    console.log('Reconciliation result:', result);
    fetchCapital();
    fetchCapitalTransactions();
  } catch (err) {
    alert('Failed to trigger reconciliation: ' + err.message);
  }
}
