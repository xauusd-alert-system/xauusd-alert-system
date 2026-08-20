import dotenv from 'dotenv';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

dotenv.config();

// Also load the project-root .env (LEDGER_OWNER_TOKEN etc.) so the UI picks
// the owner token up automatically instead of requiring a manual paste.
// Tries: cwd/.env, cwd/../.env (dev: run from the UI folder), and module-relative
// paths (built dist/server.cjs). First existing file wins; already-set vars win.
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const envCandidates = [
  path.join(process.cwd(), '.env'),
  path.join(process.cwd(), '..', '.env'),
  path.join(__dirname, '..', '.env'),
  path.join(__dirname, '..', '..', '.env'),
];
for (const candidate of envCandidates) {
  if (fs.existsSync(candidate)) {
    dotenv.config({ path: candidate });
    break;
  }
}

import express, { Request, Response, NextFunction } from 'express';
import { DASHBOARD_HTML } from './src/dashboardHtml.js';

const app = express();
const PORT = 3000;
const getBackendBaseUrl = () => process.env.BACKEND_BASE_URL || 'http://127.0.0.1:8000';

// Owner token auto-injection: when LEDGER_OWNER_TOKEN is present in .env, the
// served dashboard writes it into sessionStorage BEFORE the page's own init
// script runs, so the token input is prefilled and ledger calls are already
// authorized — no manual paste needed.
const OWNER_TOKEN_FROM_ENV = (process.env.LEDGER_OWNER_TOKEN || '').trim();

function renderDashboard(): string {
  if (!OWNER_TOKEN_FROM_ENV) {
    return DASHBOARD_HTML;
  }
  const injectScript =
    `<script>try { sessionStorage.setItem('xau_alert_owner_token', ${JSON.stringify(OWNER_TOKEN_FROM_ENV)}); } catch (e) {}</script>\n    `;
  const marker = '<!-- Client-side Logic -->';
  if (DASHBOARD_HTML.includes(marker)) {
    return DASHBOARD_HTML.replace(marker, marker + '\n    ' + injectScript);
  }
  return DASHBOARD_HTML.replace('</body>', injectScript + '</body>');
}

// Byte-exact raw body preservation for signed ingress (HMAC X-Ledger-Signature)
// Stores the raw incoming bytes as Buffer in req.body without parsing, pretty-printing, or modifying order
app.use(express.raw({ type: '*/*', limit: '20mb' }));

// Enable CORS for owner-only research terminal
app.use((req: Request, res: Response, next: NextFunction) => {
  res.header('Access-Control-Allow-Origin', '*');
  res.header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  res.header('Access-Control-Allow-Headers', 'Origin, X-Requested-With, Content-Type, Accept, Authorization, X-Ledger-Signature');
  if (req.method === 'OPTIONS') {
    res.sendStatus(204);
    return;
  }
  next();
});

/**
 * Universal Transparent Proxy Handler to Python FastAPI Backend (realtime/app.py).
 * 
 * Byte-Exact Forwarding Rules:
 * 1. Forwards raw Buffer body directly to Python backend without JSON.parse or JSON.stringify.
 *    This preserves exact byte-by-byte layout for HMAC verification on /api/ledger/ingest.
 * 2. Forwards all client headers (including Authorization: Bearer <token>, X-Ledger-Signature, Content-Type).
 * 3. Transparently relays backend HTTP status codes (200, 401, 403, 422, 501, etc.) and response body.
 * 4. If Python backend is unreachable, returns HTTP 503:
 *    { "available": false, "source": "backend_unreachable", "reason": "<error>" }
 *    Never generates synthetic mock signals, candles, or positions.
 */
export async function proxyToBackend(req: Request, res: Response): Promise<void> {
  const baseUrl = getBackendBaseUrl();
  const targetUrl = `${baseUrl.replace(/\/$/, '')}${req.originalUrl || req.url}`;
  
  // Strip hop-by-hop headers
  const forwardHeaders: Record<string, string> = {};
  for (const [key, value] of Object.entries(req.headers)) {
    const lowerKey = key.toLowerCase();
    if (
      lowerKey === 'host' ||
      lowerKey === 'connection' ||
      lowerKey === 'keep-alive' ||
      lowerKey === 'transfer-encoding' ||
      lowerKey === 'content-length'
    ) {
      continue;
    }
    if (typeof value === 'string') {
      forwardHeaders[key] = value;
    } else if (Array.isArray(value)) {
      forwardHeaders[key] = value.join(', ');
    }
  }

  const fetchOptions: RequestInit = {
    method: req.method,
    headers: forwardHeaders,
  };

  // Pass raw Buffer body without alteration for POST / PUT / PATCH
  if (req.method !== 'GET' && req.method !== 'HEAD' && req.body) {
    if (Buffer.isBuffer(req.body) && req.body.length > 0) {
      fetchOptions.body = req.body as any;
    } else if (typeof req.body === 'string' && req.body.length > 0) {
      fetchOptions.body = req.body;
    }
  }

  try {
    const backendRes = await fetch(targetUrl, fetchOptions);

    // Forward status code (e.g. 501 for /control, 403 for /ledger, 200, etc.)
    res.status(backendRes.status);

    // Forward response headers
    backendRes.headers.forEach((val, name) => {
      const lower = name.toLowerCase();
      if (lower !== 'transfer-encoding' && lower !== 'content-length') {
        res.setHeader(name, val);
      }
    });

    const buffer = await backendRes.arrayBuffer();
    res.send(Buffer.from(buffer));
  } catch (err: any) {
    res.status(503).json({
      available: false,
      source: 'backend_unreachable',
      reason: `Python FastAPI backend connection failed (${targetUrl}): ${err?.message || 'Connection refused or timeout'}`,
    });
  }
}

// --------------------------------------------------------------------------
// Web UI Static Routes (Owner-Only Research Terminal)
// --------------------------------------------------------------------------
app.get('/', (req, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.send(renderDashboard());
});

app.get('/dashboard', (req, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.send(renderDashboard());
});

// --------------------------------------------------------------------------
// Proxy All API & Pipeline Routes to Python FastAPI Backend (realtime/app.py)
// --------------------------------------------------------------------------
app.all('/health', proxyToBackend);
app.all('/signal', proxyToBackend);
app.all('/api/*', proxyToBackend);

// --------------------------------------------------------------------------
// WebSocket Contract: Variant B (Honest REST-Only Mode)
// Node UI port 3000 does not simulate WebSocket events. Web UI polls /api/ledger/events via REST.
// If /ws is requested via HTTP, return clear 501 response explaining honest architecture.
// --------------------------------------------------------------------------
app.all('/ws', (req, res) => {
  res.status(501).json({
    available: false,
    source: 'ws_not_implemented',
    reason: 'WebSocket proxy is not implemented on Node UI port 3000. Use REST polling for /api/ledger/events or connect directly to Python FastAPI backend WS port.',
  });
});

// --------------------------------------------------------------------------
// 404 Handler for undefined non-API routes
// --------------------------------------------------------------------------
app.use((req, res) => {
  res.status(404).json({
    available: false,
    source: 'not_found',
    reason: `Route ${req.method} ${req.originalUrl} not found`,
  });
});

export { app };

const isMainModule = process.argv[1] && (process.argv[1].endsWith('server.ts') || process.argv[1].endsWith('server.cjs') || process.argv[1].endsWith('server.js'));

if (process.env.NODE_ENV !== 'test' && isMainModule) {
  app.listen(PORT, '0.0.0.0', () => {
    console.log(`[xauusd-control-terminal] Node.js Proxy & UI listening on port ${PORT}`);
    console.log(`[xauusd-control-terminal] Target Python FastAPI backend: ${getBackendBaseUrl()}`);
    console.log(`[xauusd-control-terminal] Byte-exact signed ingress: ENABLED (raw Buffer passthrough)`);
    console.log(`[xauusd-control-terminal] WebSocket architecture: Variant B (REST-only polling)`);
    console.log(`[xauusd-control-terminal] Owner token auto-inject: ${OWNER_TOKEN_FROM_ENV ? 'ENABLED (from .env, no manual paste needed)' : 'DISABLED (set LEDGER_OWNER_TOKEN in .env)'}`);
  });
}
