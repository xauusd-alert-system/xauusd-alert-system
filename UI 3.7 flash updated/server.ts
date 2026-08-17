import express, { Request, Response } from 'express';
import { DASHBOARD_HTML } from './src/dashboardHtml.js';

const app = express();
const PORT = 3000;
const BACKEND_BASE_URL = process.env.BACKEND_BASE_URL || 'http://127.0.0.1:8000';

app.use(express.json({ limit: '10mb' }));
app.use(express.urlencoded({ extended: true }));

// Enable CORS for owner-only research terminal
app.use((req, res, next) => {
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
 * Forwards method, path, query params, headers, and request body directly.
 * If backend is down or unreachable, returns HTTP 503 with explicit honesty payload:
 * { "available": false, "source": "backend_unreachable", "reason": "<error>" }
 * Never falls back to synthetic mock data.
 */
export async function proxyToBackend(req: Request, res: Response): Promise<void> {
  const targetUrl = `${BACKEND_BASE_URL.replace(/\/$/, '')}${req.originalUrl || req.url}`;
  
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

  if (req.method !== 'GET' && req.method !== 'HEAD' && req.body) {
    if (typeof req.body === 'object' && Object.keys(req.body).length > 0) {
      fetchOptions.body = JSON.stringify(req.body);
      if (!forwardHeaders['content-type'] && !forwardHeaders['Content-Type']) {
        forwardHeaders['Content-Type'] = 'application/json';
      }
    } else if (typeof req.body === 'string') {
      fetchOptions.body = req.body;
    }
  }

  try {
    const backendRes = await fetch(targetUrl, fetchOptions);

    // Forward status code
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
  res.send(DASHBOARD_HTML);
});

app.get('/dashboard', (req, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.send(DASHBOARD_HTML);
});

// --------------------------------------------------------------------------
// Proxy All API & Pipeline Routes to Python FastAPI Backend (realtime/app.py)
// --------------------------------------------------------------------------
app.all('/health', proxyToBackend);
app.all('/signal', proxyToBackend);
app.all('/api/*', proxyToBackend);
app.all('/ws', proxyToBackend);

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

app.listen(PORT, '0.0.0.0', () => {
  console.log(`[xauusd-control-terminal] Node.js Proxy & UI listening on port ${PORT}`);
  console.log(`[xauusd-control-terminal] Target Python FastAPI backend: ${BACKEND_BASE_URL}`);
  console.log(`[xauusd-control-terminal] Honesty invariant enforced: No synthetic data generation`);
});
