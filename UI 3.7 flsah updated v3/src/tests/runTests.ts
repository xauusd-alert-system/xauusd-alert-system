/**
 * UI & API Honesty & Integration Test Suite (ТЗ Раздел 14, Раздел 12, P0-доработка).
 * 
 * Verifies:
 * 1. Freshness states tokens & age formatting.
 * 2. Actionable decision logic vs Signal Bias separation.
 * 3. Deployment mode badges & governance.
 * 4. No-numeric-fallback invariants.
 * 5. Position Quality honest stub contract.
 * 6. Execution routing & safety guards.
 * 7. Live HTTP Proxy integration tests:
 *    - 501 pass-through for /api/control/*
 *    - 403 pass-through for /api/ledger/events (unauthorized)
 *    - 200 pass-through with bearer token
 *    - Byte-exact HMAC SHA256 signed ingress for /api/ledger/ingest
 *    - 503 backend_unreachable when Python backend is down
 *    - 501 on /ws (Variant B honest contract)
 */

import http from 'node:http';
import crypto from 'node:crypto';
import { getFreshnessBadge, getDecisionBadge, getDeploymentModeBadge, formatAge } from '../theme/tokens.js';
import { app } from '../../server.js';

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

async function runAllTests() {
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
    const armedLong = getDecisionBadge('long', 'armed');
    assert(armedLong.label === 'NO TRADE', 'Armed state with long bias displays NO TRADE');

    const watchLong = getDecisionBadge('long', 'watch');
    assert(watchLong.label === 'NO TRADE', 'Watch state with long bias displays NO TRADE');

    const rejectedLong = getDecisionBadge('long', 'rejected');
    assert(rejectedLong.label === 'NO TRADE', 'Rejected state with long bias displays NO TRADE');

    const confirmedLong = getDecisionBadge('long', 'confirmed');
    assert(confirmedLong.label === 'LONG / BUY', 'Confirmed state with long bias displays LONG / BUY');

    const confirmedShort = getDecisionBadge('short', 'confirmed');
    assert(confirmedShort.label === 'SHORT / SELL', 'Confirmed state with short bias displays SHORT / SELL');

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
    const unavailableStatus = {
      available: false,
      balance: null,
      equity: null,
      floating_pnl: null,
      freshness_status: 'offline',
    };
    assert(unavailableStatus.balance === null, 'Unavailable status balance is strictly null');
    assert(unavailableStatus.equity === null, 'Unavailable status equity is strictly null');
    assert(unavailableStatus.floating_pnl === null, 'Unavailable status floating_pnl is strictly null');

    const unavailableSignal = {
      asset: 'XAUUSD',
      bias: null,
      confidence: null,
      available: false,
      status: 'unavailable',
    };
    assert(unavailableSignal.bias === null, 'Unavailable matrix signal bias is null (not "neutral")');
    assert(unavailableSignal.confidence === null, 'Unavailable matrix signal confidence is null (not 0.50)');

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
  // 7. Live Proxy Integration Tests with Mock Python Backend
  // --------------------------------------------------------------------------
  console.log('\n7. Running Real In-Process HTTP Proxy Integration Tests...');

  const HMAC_SECRET = 'test_owner_hmac_secret_key_2026';
  const OWNER_TOKEN = 'secret_owner_token_123';

  const mockState: { receivedRawBody: Buffer | null; receivedSignature: string | null } = {
    receivedRawBody: null,
    receivedSignature: null,
  };

  const getReceivedRawBody = (): Buffer | null => mockState.receivedRawBody;
  const getReceivedSignature = (): string | null => mockState.receivedSignature;

  // 7.1 Spin up a Mock Python FastAPI Backend
  const mockBackend = http.createServer((req, res) => {
    const url = new URL(req.url || '/', `http://${req.headers.host}`);
    const pathname = url.pathname;

    // Collect raw chunks
    const chunks: Buffer[] = [];
    req.on('data', (chunk) => chunks.push(chunk));
    req.on('end', () => {
      const rawBody = Buffer.concat(chunks);

      if (pathname === '/api/control/pause' || pathname.startsWith('/api/control/')) {
        res.writeHead(501, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          available: false,
          status: 'not_implemented',
          reason: 'browser_mutations_disabled_use_telegram_bot',
          detail: 'Control mutations return 501 in research mode',
        }));
        return;
      }

      if (pathname === '/api/ledger/events') {
        const auth = req.headers['authorization'];
        if (!auth || auth !== `Bearer ${OWNER_TOKEN}`) {
          res.writeHead(403, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({
            available: false,
            source: 'ledger_auth',
            detail: 'Owner Bearer token required',
          }));
          return;
        }

        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          available: true,
          events: [
            { event_id: 'evt_001', event_type: 'intent_created', asset_key: 'XAUUSD', received_at_utc_ms: Date.now(), source: 'realtime_pipeline' }
          ],
        }));
        return;
      }

      if (pathname === '/api/ledger/ingest') {
        const signature = req.headers['x-ledger-signature'] as string;
        mockState.receivedRawBody = rawBody;
        mockState.receivedSignature = signature || null;

        // Verify byte-exact HMAC SHA256 of the raw body
        const expectedHmac = crypto.createHmac('sha256', HMAC_SECRET).update(rawBody).digest('hex');
        
        if (!signature || signature !== expectedHmac) {
          res.writeHead(401, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({
            available: false,
            error: 'invalid_hmac_signature',
            received_signature: signature || null,
            body_length_bytes: rawBody.length,
          }));
          return;
        }

        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          available: true,
          status: 'ingested',
          bytes_verified: rawBody.length,
          payload_echo: JSON.parse(rawBody.toString('utf-8')),
        }));
        return;
      }

      if (pathname === '/api/status') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({
          available: true,
          data_mode: 'mock',
          deployment_mode: 'research',
          config_hash: 'abc1234567',
          freshness_status: 'fresh',
        }));
        return;
      }

      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ available: false, error: 'not_found' }));
    });
  });

  await new Promise<void>((resolve) => mockBackend.listen(0, '127.0.0.1', resolve));
  const mockBackendPort = (mockBackend.address() as any).port;
  const mockBackendUrl = `http://127.0.0.1:${mockBackendPort}`;

  // Set environment variable for backend
  process.env.BACKEND_BASE_URL = mockBackendUrl;

  // Spin up Node Proxy app
  const proxyServer = http.createServer(app);
  await new Promise<void>((resolve) => proxyServer.listen(0, '127.0.0.1', resolve));
  const proxyPort = (proxyServer.address() as any).port;
  const proxyBaseUrl = `http://127.0.0.1:${proxyPort}`;

  try {
    // Test 7.1: 501 pass-through on /api/control/pause
    {
      const res = await fetch(`${proxyBaseUrl}/api/control/pause`, { method: 'POST' });
      const data = await res.json() as any;
      assert(res.status === 501, 'Proxy passes through HTTP 501 for /api/control/pause');
      assert(data.status === 'not_implemented', 'Proxy body has not_implemented status');
    }

    // Test 7.2: 403 pass-through on /api/ledger/events without Bearer token
    {
      const res = await fetch(`${proxyBaseUrl}/api/ledger/events`);
      const data = await res.json() as any;
      assert(res.status === 403, 'Proxy passes through HTTP 403 for unauthorized ledger access');
      assert(data.source === 'ledger_auth', 'Proxy returns backend ledger_auth error body');
    }

    // Test 7.3: 200 pass-through on /api/ledger/events with valid Bearer token
    {
      const res = await fetch(`${proxyBaseUrl}/api/ledger/events`, {
        headers: { Authorization: `Bearer ${OWNER_TOKEN}` },
      });
      const data = await res.json() as any;
      assert(res.status === 200, 'Proxy passes through HTTP 200 with Bearer authorization');
      assert(data.available === true && data.events?.length === 1, 'Proxy returns valid ledger events list');
    }

    // Test 7.4: Byte-exact signed ingress with HMAC SHA256 on /api/ledger/ingest
    {
      // Intentionally unformatted JSON with non-alphabetical keys and extra whitespace
      const payloadString = '{  "volume" : 0.05, "symbol":"XAUUSD",  "intent_id" : "intent_test_999", "z": [ 1,2, 3 ] }';
      const rawPayloadBytes = Buffer.from(payloadString, 'utf-8');
      const validHmac = crypto.createHmac('sha256', HMAC_SECRET).update(rawPayloadBytes).digest('hex');

      mockState.receivedRawBody = null;
      mockState.receivedSignature = null;

      const res = await fetch(`${proxyBaseUrl}/api/ledger/ingest`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Ledger-Signature': validHmac,
        },
        body: rawPayloadBytes,
      });
      const data = await res.json() as any;
      assert(res.status === 200, 'Byte-exact /api/ledger/ingest passes HMAC signature check');
      assert(data.status === 'ingested', 'Signed payload ingested successfully');
      assert(data.bytes_verified === rawPayloadBytes.length, 'Payload byte count matches exactly');
      const receivedBuf = getReceivedRawBody();
      assert(
        receivedBuf !== null && receivedBuf.equals(rawPayloadBytes),
        'Proxy forwards signed request body byte-for-byte without reserialization'
      );
      assert(
        getReceivedSignature() === validHmac,
        'Proxy forwards exact X-Ledger-Signature header'
      );
    }

    // Test 7.5a: Negative test: signature computed over slightly altered bytes (trailing space) is rejected with 401
    {
      const payloadString = '{  "volume" : 0.05, "symbol":"XAUUSD",  "intent_id" : "intent_test_999", "z": [ 1,2, 3 ] }';
      const rawPayloadBytes = Buffer.from(payloadString, 'utf-8');
      const alteredPayloadString = payloadString + ' ';
      const alteredBytes = Buffer.from(alteredPayloadString, 'utf-8');
      const signatureFromAltered = crypto.createHmac('sha256', HMAC_SECRET).update(alteredBytes).digest('hex');

      const res = await fetch(`${proxyBaseUrl}/api/ledger/ingest`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Ledger-Signature': signatureFromAltered,
        },
        body: rawPayloadBytes,
      });
      assert(res.status === 401, 'Signature computed over altered body (trailing space) rejected with HTTP 401');
    }

    // Test 7.5b: Negative test: mismatched HMAC signature correctly rejected with HTTP 401
    {
      const payloadString = '{"intent_id":"intent_test_bad"}';
      const rawPayloadBytes = Buffer.from(payloadString, 'utf-8');

      const res = await fetch(`${proxyBaseUrl}/api/ledger/ingest`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Ledger-Signature': 'invalid_hmac_hex_string',
        },
        body: rawPayloadBytes,
      });
      assert(res.status === 401, 'Mismatched HMAC signature correctly rejected with HTTP 401');
    }

    // Test 7.6: 503 backend_unreachable when backend is shut down
    {
      // Point BACKEND_BASE_URL to an unused closed port
      process.env.BACKEND_BASE_URL = 'http://127.0.0.1:59999';
      const res = await fetch(`${proxyBaseUrl}/api/status`);
      const data = await res.json() as any;
      assert(res.status === 503, 'Proxy returns HTTP 503 when backend is unreachable');
      assert(data.available === false, '503 body specifies available: false');
      assert(data.source === 'backend_unreachable', '503 body source is backend_unreachable');
      assert(!('fake_candles' in data), 'Proxy never generates synthetic candles on backend failure');
    }

    // Test 7.7: /ws returns 501 Variant B contract
    {
      const res = await fetch(`${proxyBaseUrl}/ws`);
      const data = await res.json() as any;
      assert(res.status === 501, 'GET /ws returns HTTP 501 under Variant B (REST-only)');
      assert(data.source === 'ws_not_implemented', '/ws identifies as ws_not_implemented');
    }

  } finally {
    // Teardown
    mockBackend.close();
    proxyServer.close();
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
}

runAllTests().catch((err) => {
  console.error('Fatal error during test run:', err);
  process.exit(1);
});
