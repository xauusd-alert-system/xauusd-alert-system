# -*- coding: utf-8 -*-
"""Pairs Model Dashboard (ТЗ §5): generates an interactive HTML panel.

Usage:
    PYTHONIOENCODING=utf-8 python -m scripts.pairs_dashboard [--timeframe D1] [--out path]
    PYTHONIOENCODING=utf-8 python -m scripts.pairs_dashboard --serve [--refresh-minutes 5]

Output: self-contained HTML with Chart.js (CDN), dark theme, monospace.
  --serve: starts a local HTTP server with auto-refresh (polls /api/data.json)."""
import argparse
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pairs_analysis import EnsembleEngine, PairAnalyzer, SignalEngine, load_config

OUT_DEFAULT = os.path.join(ROOT, "data", "backtest", "pairs_dashboard.html")


def _collect_data(tf: str) -> dict:
    """Analyze all pairs and return structured data for the dashboard."""
    cfg = load_config()
    analysis = cfg.get("analysis", {})
    thresholds = cfg.get("thresholds", {})
    bt_cfg = dict(analysis)
    bt_cfg.update(cfg.get("backtest", {}) or {})

    sig_engine = SignalEngine(thresholds, bt_cfg)
    ens_engine = EnsembleEngine(cfg)

    pairs_data = []
    for pair in cfg.get("pairs", []):
        try:
            pa = PairAnalyzer(pair, analysis)
            m = pa.analyze(tf)
            sig = sig_engine.current(m)
            ens = ens_engine.forecast(m)

            z_hist = m.zscore.dropna()
            z_tail = z_hist.iloc[-120:]
            z_values = [round(float(v), 4) for v in z_tail.values]
            z_dates = [str(d.date()) for d in z_tail.index]

            pairs_data.append({
                "name": m.name,
                "timeframe": m.timeframe,
                "n_bars": m.n_bars,
                "start": m.start,
                "end": m.end,
                "beta": round(m.beta, 4),
                "beta_method": m.beta_method,
                "half_life_days": round(m.half_life_days, 1) if np.isfinite(m.half_life_days) else None,
                "theta": round(m.theta, 5),
                "adf_p": round(m.adf_p, 4),
                "z": round(float(z_hist.iloc[-1]), 3) if len(z_hist) else 0,
                "mu": round(m.mu, 4),
                "sigma": round(m.sigma, 4),
                "sigma_annual": round(m.sigma_annual, 2),
                "ratio": round(m.ratio, 2),
                "p1_last": round(m.p1_last, 2),
                "p2_last": round(m.p2_last, 2),
                "formula": m.formula_str,
                "hurst": round(m.hurst, 3),
                "skew": round(m.skew, 3),
                "ex_kurtosis": round(m.ex_kurtosis, 3),
                "acf1": round(m.acf1, 3),
                "realized_vol_pct": round(m.realized_vol_pct, 2),
                "signal_direction": sig.direction,
                "signal_valid": sig.valid,
                "signal_reason": sig.reason,
                "ensemble_direction": ens.direction,
                "ensemble_confidence": round(ens.confidence, 1),
                "ensemble_line": ens.summary_line(),
                "ensemble_engines": [e.as_dict() for e in ens.engines],
                "z_dates": z_dates,
                "z_values": z_values,
            })
        except Exception as e:
            print(f"  {pair['name']}: ПРОПУЩЕНА — {e}", file=sys.stderr)

    return {"timeframe": tf, "pairs": pairs_data}


# HTML template — NOT an f-string, uses @@PLACEHOLDER@@ for data injection
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PAIRS MODEL — Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --border: #30363d;
    --text: #e6edf3; --dim: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --red: #f85149; --yellow: #d29922;
    --mono: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Consolas', monospace;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font-family:var(--mono); font-size:13px; }
  .header { background:var(--panel); border-bottom:1px solid var(--border); padding:12px 20px;
             display:flex; align-items:center; gap:16px; }
  .header h1 { font-size:16px; color:var(--accent); font-weight:600; }
  .header .method { color:var(--dim); font-size:12px; }
  select { background:var(--panel); color:var(--text); border:1px solid var(--border);
            padding:4px 8px; border-radius:4px; font-family:var(--mono); font-size:12px; }
  .container { display:grid; grid-template-columns:1fr 1fr; gap:1px; background:var(--border); }
  .panel { background:var(--panel); padding:14px 18px; }
  .panel h3 { color:var(--accent); font-size:11px; text-transform:uppercase; letter-spacing:1px;
               margin-bottom:10px; border-bottom:1px solid var(--border); padding-bottom:6px; }
  .params { display:grid; grid-template-columns:1fr 1fr; gap:6px 20px; }
  .param { display:flex; justify-content:space-between; padding:2px 0; }
  .param .label { color:var(--dim); }
  .param .value { color:var(--text); font-weight:600; }
  .formula { grid-column:1/-1; color:var(--dim); font-size:11px; padding:6px 0;
              border-top:1px solid var(--border); margin-top:4px; }
  .signal-bar { padding:10px 14px; border-radius:4px; font-size:14px; font-weight:700;
                 text-align:center; margin:8px 0; }
  .signal-bar.long { background:rgba(63,185,80,0.15); color:var(--green); border:1px solid var(--green); }
  .signal-bar.short { background:rgba(248,81,73,0.15); color:var(--red); border:1px solid var(--red); }
  .signal-bar.neutral { background:rgba(139,148,158,0.1); color:var(--dim); border:1px solid var(--border); }
  .chart-panel { min-height:280px; }
  .math-board { display:grid; grid-template-columns:1fr 1fr; gap:4px 16px; }
  .math-item { display:flex; justify-content:space-between; padding:2px 0; }
  .math-item .label { color:var(--dim); }
  .math-item .value { font-weight:600; }
  .hurst-label { font-size:11px; margin-top:6px; padding:4px 8px; border-radius:3px; }
  .hurst-label.trending { background:rgba(248,81,73,0.1); color:var(--red); }
  .hurst-label.meanrev { background:rgba(63,185,80,0.1); color:var(--green); }
  .hurst-label.random { background:rgba(139,148,158,0.1); color:var(--dim); }
  .ensemble-table { width:100%; border-collapse:collapse; font-size:12px; }
  .ensemble-table th { text-align:left; color:var(--dim); font-weight:400; padding:4px 6px;
                        border-bottom:1px solid var(--border); }
  .ensemble-table td { padding:4px 6px; border-bottom:1px solid var(--border); }
  .ensemble-table .dir-long { color:var(--green); }
  .ensemble-table .dir-short { color:var(--red); }
  .ensemble-table .dir-neutral { color:var(--dim); }
  .conf-bar { display:inline-block; height:6px; border-radius:3px; min-width:2px; }
  .ensemble-summary { margin-top:8px; font-size:14px; font-weight:700; text-align:center; }
  .full-width { grid-column:1/-1; }
  canvas { max-height:240px; }
</style>
</head>
<body>
<div class="header">
  <h1>PAIRS MODEL</h1>
  <select id="pairSelect" onchange="showPair()"></select>
  <select id="tfSelect" onchange="showPair()">
    <option value="D1">D1</option><option value="H1">H1</option>
  </select>
  <span class="method" id="methodLabel"></span>
</div>
<div class="container" id="main"></div>

<script>
const DATA = @@DATA_JSON@@;
let charts = {};

function showPair() {
  const sel = document.getElementById('pairSelect');
  const pair = DATA.pairs[sel.value];
  if (!pair) return;

  document.getElementById('methodLabel').textContent =
    `${pair.beta_method} · ${pair.n_bars} bars · ${pair.start} → ${pair.end}`;

  const zDir = pair.signal_direction;
  const ensDir = pair.ensemble_direction;
  const ensConf = pair.ensemble_confidence;
  const hurst = pair.hurst;
  const hurstClass = hurst < 0.5 ? 'meanrev' : hurst > 0.5 ? 'trending' : 'random';
  const hurstText = hurst < 0.5 ? 'mean-reverting' : hurst > 0.5 ? 'trending' : 'random walk';
  const hlText = pair.half_life_days !== null ? pair.half_life_days + ' d' : '∞';
  const adfColor = pair.adf_p < 0.05 ? 'var(--green)' : 'var(--red)';

  document.getElementById('main').innerHTML = `
    <div class="panel">
      <h3>Parameters</h3>
      <div class="params">
        <div class="param"><span class="label">β (${pair.beta_method})</span><span class="value">${pair.beta}</span></div>
        <div class="param"><span class="label">z-score</span><span class="value">${pair.z}</span></div>
        <div class="param"><span class="label">half-life</span><span class="value">${hlText}</span></div>
        <div class="param"><span class="label">μ (spread)</span><span class="value">${pair.mu}</span></div>
        <div class="param"><span class="label">θ</span><span class="value">${pair.theta}</span></div>
        <div class="param"><span class="label">σ (spread)</span><span class="value">${pair.sigma}</span></div>
        <div class="param"><span class="label">ADF p</span><span class="value" style="color:${adfColor}">${pair.adf_p}</span></div>
        <div class="param"><span class="label">σ annual</span><span class="value">${pair.sigma_annual}</span></div>
        <div class="param"><span class="label">ratio</span><span class="value">${pair.ratio}</span></div>
        <div class="param"><span class="label">P1/P2</span><span class="value">${pair.p1_last} / ${pair.p2_last}</span></div>
        <div class="formula">${pair.formula}</div>
      </div>
    </div>

    <div class="panel chart-panel">
      <h3>Z-Score</h3>
      <canvas id="zChart"></canvas>
    </div>

    <div class="panel">
      <h3>Signal</h3>
      <div class="signal-bar ${zDir}">${pair.signal_reason}</div>
      <div style="margin-top:8px; font-size:11px; color:var(--dim);">Entry z: ±2.0σ · Exit z: 0.0 · Stop: ±3.0σ</div>
    </div>

    <div class="panel">
      <h3>Math Board</h3>
      <div class="math-board">
        <div class="math-item"><span class="label">Hurst (R/S)</span><span class="value">${pair.hurst}</span></div>
        <div class="math-item"><span class="label">ACF(1)</span><span class="value">${pair.acf1}</span></div>
        <div class="math-item"><span class="label">Skew</span><span class="value">${pair.skew}</span></div>
        <div class="math-item"><span class="label">Ex-Kurt</span><span class="value">${pair.ex_kurtosis}</span></div>
        <div class="math-item"><span class="label">σ realized</span><span class="value">${pair.realized_vol_pct}%</span></div>
      </div>
      <div class="hurst-label ${hurstClass}">
        H = ${pair.hurst} ${pair.hurst >= 0.5 ? '≥' : '<'} 0.5 ⇒ ${hurstText} regime
      </div>
    </div>

    <div class="panel full-width">
      <h3>Model Ensemble — Forecasts</h3>
      <table class="ensemble-table">
        <tr><th>Engine</th><th>Direction</th><th>Confidence</th><th>Key metric</th></tr>
        ${pair.ensemble_engines.map(e => {
          const dClass = e.direction === 'long' ? 'dir-long' : e.direction === 'short' ? 'dir-short' : 'dir-neutral';
          const entries = Object.entries(e).filter(([k]) => !['name','direction','confidence'].includes(k));
          const key = entries[0];
          const keyStr = key ? key[0] + '=' + (typeof key[1] === 'number' ? key[1].toFixed(3) : key[1]) : '';
          const confW = Math.round(e.confidence * 0.6);
          const confColor = e.confidence > 60 ? 'var(--green)' : e.confidence > 40 ? 'var(--yellow)' : 'var(--dim)';
          return `<tr>
            <td>${e.name}</td>
            <td class="${dClass}">${e.direction.toUpperCase()}</td>
            <td><span class="conf-bar" style="width:${confW}px;background:${confColor}"></span> ${e.confidence.toFixed(1)}%</td>
            <td style="color:var(--dim);font-size:11px">${keyStr}</td>
          </tr>`;
        }).join('')}
      </table>
      <div class="ensemble-summary" style="color:${ensDir==='long'?'var(--green)':ensDir==='short'?'var(--red)':'var(--dim)'}">
        ${pair.ensemble_line} (confidence ${ensConf}%)
      </div>
    </div>
  `;

  // Z-score chart
  if (charts.zChart) charts.zChart.destroy();
  const ctx = document.getElementById('zChart').getContext('2d');
  const zVals = pair.z_values;
  const zLabels = pair.z_dates;
  const entryZ = 2.0;
  const stopZ = 3.0;

  charts.zChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: zLabels,
      datasets: [
        { label: 'z-score', data: zVals, borderColor: '#e6edf3', borderWidth: 1.5,
          pointRadius: 0, fill: false, tension: 0.1 },
        { label: '+2σ', data: Array(zVals.length).fill(entryZ), borderColor: '#f85149',
          borderWidth: 1, borderDash: [4,4], pointRadius: 0, fill: false },
        { label: '-2σ', data: Array(zVals.length).fill(-entryZ), borderColor: '#3fb950',
          borderWidth: 1, borderDash: [4,4], pointRadius: 0, fill: false },
        { label: '+3σ', data: Array(zVals.length).fill(stopZ), borderColor: '#f8514966',
          borderWidth: 1, borderDash: [2,4], pointRadius: 0, fill: false },
        { label: '-3σ', data: Array(zVals.length).fill(-stopZ), borderColor: '#3fb95066',
          borderWidth: 1, borderDash: [2,4], pointRadius: 0, fill: false },
        { label: '0', data: Array(zVals.length).fill(0), borderColor: '#30363d',
          borderWidth: 1, pointRadius: 0, fill: false },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
      scales: {
        x: { display: true, ticks: { color: '#8b949e', maxTicksLimit: 8, font: { size: 10 } },
              grid: { color: '#30363d33' } },
        y: { ticks: { color: '#8b949e', font: { size: 10 } },
              grid: { color: '#30363d33' } }
      }
    }
  });
}

// Init
const sel = document.getElementById('pairSelect');
DATA.pairs.forEach((p, i) => {
  const opt = document.createElement('option');
  opt.value = i; opt.text = p.name;
  sel.appendChild(opt);
});
if (DATA.pairs.length) showPair();
</script>
</body>
</html>"""


def _generate_html(data: dict, refresh_minutes: int = 0) -> str:
    """Generate self-contained HTML dashboard.
    If refresh_minutes > 0, embed auto-refresh polling."""
    pairs_json = json.dumps(data, ensure_ascii=False)
    html = _HTML_TEMPLATE.replace("@@DATA_JSON@@", pairs_json)
    if refresh_minutes > 0:
        # Inject auto-refresh: status bar + polling JS
        status_bar = (
            '<div id="refreshBar" style="position:fixed;top:0;right:0;padding:4px 12px;'
            'background:var(--panel);border:1px solid var(--border);border-radius:0 0 0 4px;'
            'font-size:10px;color:var(--dim);z-index:999;">'
            f'<span id="refreshStatus">● auto-refresh: ${refresh_minutes}m</span></div>')
        inject = (
            f'<script>\n'
            f'const REFRESH_MS = {refresh_minutes} * 60 * 1000;\n'
            f'const STATUS_EL = document.getElementById("refreshStatus");\n'
            f'async function refreshData() {{\n'
            f'  try {{\n'
            f'    const r = await fetch("/api/data.json?t=" + Date.now());\n'
            f'    if (!r.ok) return;\n'
            f'    const newData = await r.json();\n'
            f'    Object.assign(DATA, newData);\n'
            f'    // Rebuild pair dropdown\n'
            f'    const sel = document.getElementById("pairSelect");\n'
            f'    const prev = sel.value;\n'
            f'    sel.innerHTML = "";\n'
            f'    DATA.pairs.forEach((p, i) => {{\n'
            f'      const opt = document.createElement("option");\n'
            f'      opt.value = i; opt.text = p.name; sel.appendChild(opt);\n'
            f'    }});\n'
            f'    sel.value = prev < DATA.pairs.length ? prev : 0;\n'
            f'    showPair();\n'
            f'    STATUS_EL.textContent = "● updated " + new Date().toLocaleTimeString();\n'
            f'    STATUS_EL.style.color = "var(--green)";\n'
            f'    setTimeout(() => {{ STATUS_EL.textContent = "● auto-refresh: {refresh_minutes}m"; STATUS_EL.style.color = "var(--dim)"; }}, 3000);\n'
            f'  }} catch(e) {{ STATUS_EL.textContent = "● refresh failed"; STATUS_EL.style.color = "var(--red)"; }}\n'
            f'}}\n'
            f'setInterval(refreshData, REFRESH_MS);\n'
            f'</script>'
        )
        html = html.replace("</body>", status_bar + "\n" + inject + "\n</body>")
    return html


def _serve(tf: str, refresh_minutes: int, port: int) -> None:
    """Tiny HTTP server: serves the dashboard HTML and /api/data.json."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class DashHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/data.json"):
                try:
                    data = _collect_data(tf)
                    payload = json.dumps(data, ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception as e:
                    msg = json.dumps({"error": str(e)}).encode()
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)
            else:
                try:
                    data = _collect_data(tf)
                    html = _generate_html(data, refresh_minutes)
                    payload = html.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception as e:
                    self.send_error(500, str(e))

        def log_message(self, fmt, *args):
            print(f"{self.address_string()} {fmt % args}")

    server = HTTPServer(("127.0.0.1", port), DashHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"Dashboard server: {url}")
    print(f"Pairs: {[p['name'] for p in _collect_data(tf)['pairs']]}")
    print(f"Timeframe: {tf}, refresh: {refresh_minutes}m")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeframe", default="D1")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--serve", action="store_true", help="start HTTP server with auto-refresh")
    ap.add_argument("--refresh-minutes", type=int, default=5, help="auto-refresh interval (default: 5)")
    ap.add_argument("--port", type=int, default=8765, help="server port (default: 8765)")
    args = ap.parse_args()

    if args.serve:
        _serve(args.timeframe, args.refresh_minutes, args.port)
    else:
        print(f"Generating pairs dashboard ({args.timeframe})...")
        data = _collect_data(args.timeframe)
        html = _generate_html(data)
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Dashboard: {args.out} ({len(data['pairs'])} pairs)")


if __name__ == "__main__":
    main()
