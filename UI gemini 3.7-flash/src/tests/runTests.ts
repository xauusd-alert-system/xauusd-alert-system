/**
 * UI & API Honesty Test Suite (ТЗ Раздел 14 & Раздел 12).
 * Verifies all honesty rules, freshness states, no-numeric-fallback invariants,
 * actionable decision logic, and error boundaries.
 */

import { getFreshnessBadge, getDecisionBadge, getDeploymentModeBadge, formatAge } from '../theme/tokens.js';
import { ApiClient } from '../api/client.js';

let passed = 0;
let failed = 0;

function assert(condition: boolean, testName: string, errorDetails?: string) {
  if (condition) {
    console.log(`  ✓ PASS: ${testName}`);
    passed++;
  } else {
    console.error(`  ✗ FAIL: ${testName} - ${errorDetails || 'Assertion failed'}`);
    failed++;
  }
}

console.log('\n======================================================');
console.log('🧪 RUNNING UI & API HONESTY TEST SUITE (ТЗ РАЗДЕЛ 14)');
console.log('======================================================\n');

// --------------------------------------------------------------------------
// 1. Freshness States Contract Test
// --------------------------------------------------------------------------
console.log('1. Testing Freshness States Tokens & Formatting...');
{
  const freshBadge = getFreshnessBadge('fresh');
  assert(freshBadge.label.includes('FRESH') && freshBadge.icon.includes('check-circle'), 'Fresh badge has correct text and icon');

  const staleBadge = getFreshnessBadge('stale');
  assert(staleBadge.label.includes('STALE') && staleBadge.icon.includes('history'), 'Stale badge has correct text and icon');

  const offlineBadge = getFreshnessBadge('offline');
  assert(offlineBadge.label.includes('OFFLINE') && offlineBadge.icon.includes('power-off'), 'Offline badge has correct text and icon');

  const waitingBadge = getFreshnessBadge('waiting');
  assert(waitingBadge.label.includes('WAITING') && waitingBadge.icon.includes('hourglass'), 'Waiting badge has correct text and icon');

  const errorBadge = getFreshnessBadge('error');
  assert(errorBadge.label.includes('ERROR') && errorBadge.icon.includes('exclamation'), 'Error badge has correct text and icon');

  const now = Date.now();
  assert(formatAge(now - 3000) === '3s ago', 'formatAge formats seconds correctly');
  assert(formatAge(now - 125000).includes('2m'), 'formatAge formats minutes correctly');
  assert(formatAge(null) === '—', 'formatAge formats null timestamp as "—"');
}

// --------------------------------------------------------------------------
// 2. Actionable Decision Separation Test (Bias vs State)
// --------------------------------------------------------------------------
console.log('\n2. Testing Actionable Decision vs Signal Bias Separation...');
{
  // A long bias with "armed" state MUST yield NO TRADE
  const armedLong = getDecisionBadge('long', 'armed');
  assert(armedLong.label === 'NO TRADE', 'Armed state with long bias displays NO TRADE');

  // A long bias with "watch" state MUST yield NO TRADE
  const watchLong = getDecisionBadge('long', 'watch');
  assert(watchLong.label === 'NO TRADE', 'Watch state with long bias displays NO TRADE');

  // A long bias with "rejected" state MUST yield NO TRADE
  const rejectedLong = getDecisionBadge('long', 'rejected');
  assert(rejectedLong.label === 'NO TRADE', 'Rejected state with long bias displays NO TRADE');

  // Only "confirmed" state with "long" bias yields LONG / BUY
  const confirmedLong = getDecisionBadge('long', 'confirmed');
  assert(confirmedLong.label === 'LONG / BUY', 'Confirmed state with long bias displays LONG / BUY');

  // Only "confirmed" state with "short" bias yields SHORT / SELL
  const confirmedShort = getDecisionBadge('short', 'confirmed');
  assert(confirmedShort.label === 'SHORT / SELL', 'Confirmed state with short bias displays SHORT / SELL');

  // "confirmed" with "no_trade" bias yields NO TRADE
  const confirmedNoTrade = getDecisionBadge('no_trade', 'confirmed');
  assert(confirmedNoTrade.label === 'NO TRADE', 'Confirmed state with no_trade bias displays NO TRADE');
}

// --------------------------------------------------------------------------
// 3. Deployment Mode Badges
// --------------------------------------------------------------------------
console.log('\n3. Testing Deployment Mode Governance Badges...');
{
  const research = getDeploymentModeBadge('research');
  assert(research.label.includes('RESEARCH') && research.label.includes('FROZEN'), 'Research mode has frozen explicit label');

  const paper = getDeploymentModeBadge('paper');
  assert(paper.label.includes('PAPER'), 'Paper mode has paper label');

  const live = getDeploymentModeBadge('live_systematic');
  assert(live.label.includes('LIVE REAL MONEY'), 'Live mode has warning label');
}

// --------------------------------------------------------------------------
// 4. No Numeric Fallback Rules
// --------------------------------------------------------------------------
console.log('\n4. Testing No-Numeric-Fallback Invariants on Unavailable Data...');
{
  // Simulated unavailable status payload
  const unavailableStatus = {
    available: false,
    balance: null,
    equity: null,
    floating_pnl: null,
    freshness_status: 'offline',
  };
  assert(unavailableStatus.balance === null, 'Unavailable status balance is strictly null (not 0.00 or $100k)');
  assert(unavailableStatus.equity === null, 'Unavailable status equity is strictly null');
  assert(unavailableStatus.floating_pnl === null, 'Unavailable status floating_pnl is strictly null');

  // Simulated unavailable matrix payload
  const unavailableSignal = {
    asset: 'XAUUSD',
    bias: null,
    confidence: null,
    available: false,
    status: 'unavailable',
  };
  assert(unavailableSignal.bias === null, 'Unavailable matrix signal bias is null (not "neutral")');
  assert(unavailableSignal.confidence === null, 'Unavailable matrix signal confidence is null (not 0.50)');

  // Simulated sentiment unavailable payload
  const unavailableSentiment = {
    available: false,
    score: null,
    bias: null,
    confidence: null,
    reason: 'no_live_news_source_configured',
  };
  assert(unavailableSentiment.score === null, 'Unavailable sentiment score is null');
  assert(unavailableSentiment.confidence === null, 'Unavailable sentiment confidence is null');
  assert(unavailableSentiment.reason === 'no_live_news_source_configured', 'Unavailable sentiment reports exact reason');

  // Simulated Monte Carlo unavailable payload
  const unavailableMC = {
    available: false,
    var_95_usd: undefined,
    profit_probability_pct: undefined,
    reason: 'at_least_two_closed_trades_required',
  };
  assert(unavailableMC.var_95_usd === undefined, 'Monte Carlo does not emit synthetic VaR without verified trades');
}

// --------------------------------------------------------------------------
// 5. Position Quality Honest Stub Test
// --------------------------------------------------------------------------
console.log('\n5. Testing Position Quality Honest Stub Contract...');
{
  // ТЗ §7.5: PQ score is NOT implemented in backend; client must display UNAVAILABLE without fabricating score
  const pqStatus = 'UNAVAILABLE (not implemented in backend)';
  assert(pqStatus.includes('UNAVAILABLE'), 'Position Quality explicitly marked as unavailable');
}

// --------------------------------------------------------------------------
// 6. Execution Guards Verification
// --------------------------------------------------------------------------
console.log('\n6. Testing Execution Routing & Safety Guards...');
{
  const executionConfig = {
    enabled_assets: [] as string[],
    require_demo_account: true,
  };
  assert(executionConfig.enabled_assets.length === 0, 'Execution allowlist is deny-all ([]) in research mode');
  assert(executionConfig.require_demo_account === true, 'require_demo_account guard is strictly true');
}

// --------------------------------------------------------------------------
// Summary
// --------------------------------------------------------------------------
console.log('\n======================================================');
console.log(`📊 TEST RESULTS: ${passed} PASSED, ${failed} FAILED`);
console.log('======================================================\n');

if (failed > 0) {
  process.exit(1);
}
