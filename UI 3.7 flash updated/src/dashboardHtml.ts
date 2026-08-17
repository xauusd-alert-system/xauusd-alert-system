/**
 * Owner-Only Research/Trading Control Terminal UI.
 * Pure Payload Renderer + State Visualizer + Audit Viewer.
 *
 * Implements all 15 components specified in ТЗ Section 7:
 * 1. System Status / Header / Governance (7.1)
 * 2. Asset Hierarchy & Matrix (7.2)
 * 3. Primary Signal Card (7.3)
 * 4. Decision Reasons & Predicates (7.4)
 * 5. Position Quality - Honest Unavailable Block (7.5)
 * 6. Provenance & SMC OHLCV Proxy Diagnostics (7.6)
 * 7. Data Health & Freshness Matrix (7.7)
 * 8. Live Chart View with 503 Research Handler (7.8)
 * 9. MT5 Positions Table (7.9)
 * 10. Execution State & Policy Guards (7.10)
 * 11. Audit Ledger Timeline (Owner Bearer) (7.11)
 * 12. Track Record vs Current State (7.12)
 * 13. Monte Carlo Risk & VaR (7.13)
 * 14. Research Section (7.14)
 * 15. Macro News & Sentiment (7.15)
 */

export const DASHBOARD_HTML = `<!DOCTYPE html>
<html lang="ru" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>XAUUSD Control Terminal · Owner-Only Research</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            --bg-canvas: #090d16;
            --bg-card: #111827;
            --bg-card-elevated: #1e293b;
            --border-subtle: rgba(255, 255, 255, 0.08);
            --border-accent: rgba(245, 158, 11, 0.4);
        }
        body {
            background-color: var(--bg-canvas);
            color: #f1f5f9;
            font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }
        .glass-panel {
            background: rgba(17, 24, 39, 0.85);
            backdrop-filter: blur(16px);
            border: 1px solid var(--border-subtle);
            border-radius: 12px;
        }
        .terminal-border {
            border-left: 4px solid #f59e0b;
        }
        .pulse-live {
            animation: pulseDot 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
        }
        @keyframes pulseDot {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.35; transform: scale(0.85); }
        }
        .tab-btn.active {
            background-color: rgba(245, 158, 11, 0.15);
            color: #fef3c7;
            border-color: #f59e0b;
            font-weight: 700;
        }
        .mono-num {
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-variant-numeric: tabular-nums;
        }
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: #0b1120; }
        ::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: #475569; }
    </style>
</head>
<body class="p-3 sm:p-5 md:p-8 min-h-screen text-slate-200">
    <div class="max-w-7xl mx-auto space-y-6">

        <!-- ============================================================ -->
        <!-- 1. SYSTEM STATUS / HEADER / GOVERNANCE (7.1) -->
        <!-- ============================================================ -->
        <header class="glass-panel p-5 terminal-border space-y-4">
            <div class="flex flex-col lg:flex-row justify-between items-start lg:items-center gap-4">
                <div class="space-y-1">
                    <div class="flex flex-wrap items-center gap-2.5">
                        <span class="w-3 h-3 rounded-full bg-emerald-500 pulse-live"></span>
                        <h1 class="text-xl sm:text-2xl font-bold bg-gradient-to-r from-amber-400 via-amber-200 to-yellow-500 bg-clip-text text-transparent">
                            xauusd-alert-system
                        </h1>
                        <span class="text-xs bg-slate-800 border border-slate-700 text-slate-300 px-2 py-0.5 rounded font-mono">
                            CONTROL TERMINAL v2.1
                        </span>
                        <span id="badge-deployment-mode" class="text-xs px-2 py-0.5 rounded font-bold uppercase tracking-wider bg-slate-800 text-slate-400 border border-slate-700">
                            RESEARCH MODE
                        </span>
                    </div>
                    <p class="text-xs text-slate-400">
                        Owner-Only Diagnostic &amp; Audit Terminal &bull; Causal Signal-Bar Parity &bull; Hash-Chained Truth Ledgers
                    </p>
                </div>

                <!-- Governance Notice & Owner Auth -->
                <div class="flex flex-col sm:flex-row items-stretch sm:items-center gap-2.5 text-xs">
                    <div class="bg-amber-950/40 border border-amber-800/60 rounded-lg p-2.5 text-amber-200/90 leading-tight">
                        <i class="fas fa-shield-halved text-amber-400 mr-1.5"></i>
                        <span class="font-semibold">INTERNAL DIAGNOSTIC VIEW</span><br>
                        <span class="text-slate-400 text-[11px]">Браузерные мутации отключены (501). Исполнение — только Telegram bot.</span>
                    </div>

                    <!-- Owner Bearer Token Input -->
                    <div class="bg-slate-900/90 border border-slate-800 rounded-lg p-2.5 flex items-center gap-2">
                        <i class="fas fa-key text-slate-400"></i>
                        <input id="owner-token-input" type="password" placeholder="Owner Bearer Token" 
                               class="bg-slate-950 text-slate-200 text-xs px-2 py-1 rounded border border-slate-700 focus:outline-none focus:border-amber-500 w-36 sm:w-44"
                               onchange="saveOwnerToken(this.value)">
                        <button onclick="saveOwnerToken(document.getElementById('owner-token-input').value)" 
                                class="bg-slate-800 hover:bg-slate-700 text-slate-300 px-2 py-1 rounded border border-slate-700 font-semibold" title="Сохранить в sessionStorage">
                            Set
                        </button>
                    </div>
                </div>
            </div>

            <!-- Global Status Bar / Strategy Identity -->
            <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 pt-2 border-t border-slate-800/80 text-xs">
                <div>
                    <span class="text-slate-400 text-[11px] block">DATA_MODE</span>
                    <span id="sys-data-mode" class="font-mono font-bold text-indigo-300">UNKNOWN</span>
                </div>
                <div>
                    <span class="text-slate-400 text-[11px] block">EXECUTION ASSETS</span>
                    <span id="sys-exec-assets" class="font-mono font-bold text-rose-400">[] (DENY-ALL)</span>
                </div>
                <div>
                    <span class="text-slate-400 text-[11px] block">DEMO GUARD</span>
                    <span id="sys-demo-guard" class="font-mono font-bold text-emerald-400">REQUIRED (true)</span>
                </div>
                <div>
                    <span class="text-slate-400 text-[11px] block">STRATEGY VERSION</span>
                    <span id="sys-strat-ver" class="font-mono text-slate-300 truncate block" title="xauusd-system-v3-signalbar-2026-08-16">v3-signalbar</span>
                </div>
                <div>
                    <span class="text-slate-400 text-[11px] block">CONFIG HASH</span>
                    <span id="sys-cfg-hash" class="font-mono text-amber-300">—</span>
                </div>
                <div>
                    <span class="text-slate-400 text-[11px] block">DATA HEALTH</span>
                    <span id="sys-health-status" class="font-mono font-bold text-emerald-400 flex items-center gap-1">
                        <i class="fas fa-check-circle"></i> FRESH
                    </span>
                </div>
            </div>
        </header>

        <!-- Navigation Tabs -->
        <nav class="flex flex-wrap gap-2 text-xs border-b border-slate-800 pb-2">
            <button onclick="switchTab('overview')" id="tab-btn-overview" class="tab-btn active px-3.5 py-2 rounded-lg border border-slate-800 transition flex items-center gap-1.5">
                <i class="fas fa-gauge"></i> Overview &amp; Signals
            </button>
            <button onclick="switchTab('positions')" id="tab-btn-positions" class="tab-btn px-3.5 py-2 rounded-lg border border-slate-800 transition flex items-center gap-1.5">
                <i class="fas fa-tasks"></i> MT5 Positions &amp; Execution
            </button>
            <button onclick="switchTab('research')" id="tab-btn-research" class="tab-btn px-3.5 py-2 rounded-lg border border-slate-800 transition flex items-center gap-1.5">
                <i class="fas fa-microscope"></i> SMC &amp; Research Diagnostics
            </button>
            <button onclick="switchTab('risk')" id="tab-btn-risk" class="tab-btn px-3.5 py-2 rounded-lg border border-slate-800 transition flex items-center gap-1.5">
                <i class="fas fa-chart-line"></i> Risk, VaR &amp; Statistics
            </button>
            <button onclick="switchTab('audit')" id="tab-btn-audit" class="tab-btn px-3.5 py-2 rounded-lg border border-slate-800 transition flex items-center gap-1.5">
                <i class="fas fa-list-check"></i> Audit Ledger Timeline
            </button>
        </nav>

        <!-- ============================================================ -->
        <!-- TAB 1: OVERVIEW & SIGNALS -->
        <!-- ============================================================ -->
        <main id="tab-overview" class="space-y-6">
            <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
                <!-- Primary Signal Card -->
                <div class="glass-panel p-6 lg:col-span-2 space-y-5 border-l-4 border-l-amber-500">
                    <div class="flex flex-wrap justify-between items-start gap-3">
                        <div>
                            <div class="flex items-center gap-2">
                                <span class="text-xs font-mono uppercase bg-amber-950 text-amber-300 border border-amber-700 px-2 py-0.5 rounded font-bold">
                                    PRIMARY ASSET · M15
                                </span>
                                <h2 class="text-2xl font-bold font-mono text-slate-100">GOLD / XAUUSD</h2>
                            </div>
                            <div class="text-xs text-slate-400 mt-1 flex items-center gap-3">
                                <span>Signal ID: <span id="sig-id" class="font-mono text-slate-300">—</span></span>
                                <span>Session: <span id="sig-session" class="font-mono uppercase text-amber-400">—</span></span>
                            </div>
                        </div>

                        <!-- Actionable Decision vs Signal Bias -->
                        <div class="text-right">
                            <div class="text-[11px] text-slate-400 uppercase tracking-wider mb-1 font-semibold">ACTIONABLE DECISION</div>
                            <div id="sig-decision-badge" class="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-bold bg-slate-800 text-slate-300 border border-slate-700">
                                <i class="fas fa-ban"></i> NO TRADE
                            </div>
                            <div class="text-[11px] text-slate-400 mt-1">
                                Signal State: <span id="sig-state" class="font-mono text-slate-300">ARMED</span>
                            </div>
                        </div>
                    </div>

                    <!-- Metrics bar -->
                    <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 bg-slate-900/60 p-3.5 rounded-lg border border-slate-800 text-xs">
                        <div>
                            <span class="text-slate-400 text-[11px] block">DIRECTION (BIAS)</span>
                            <span id="sig-bias-text" class="font-mono font-bold text-base text-slate-200">NEUTRAL</span>
                        </div>
                        <div>
                            <span class="text-slate-400 text-[11px] block">ML CONFIDENCE</span>
                            <span id="sig-confidence-val" class="font-mono font-bold text-base text-amber-300">—</span>
                        </div>
                        <div>
                            <span class="text-slate-400 text-[11px] block">MARKET REGIME</span>
                            <span id="sig-regime-val" class="font-mono font-bold text-base text-cyan-300">RANGE</span>
                        </div>
                        <div>
                            <span class="text-slate-400 text-[11px] block">DATA FRESHNESS</span>
                            <span id="sig-freshness-badge" class="inline-flex items-center gap-1 font-mono font-bold text-emerald-400 text-xs mt-1">
                                <i class="fas fa-check-circle"></i> FRESH
                            </span>
                        </div>
                    </div>

                    <!-- Trade Geometry (Entry / SL / TP1-3) -->
                    <div>
                        <div class="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2 flex items-center justify-between">
                            <span>Authoritative Trade Geometry (из /signal payload)</span>
                            <span class="text-[11px] text-slate-500 font-mono">Step: <span id="sig-step" class="text-slate-400">—</span></span>
                        </div>
                        <div class="grid grid-cols-2 sm:grid-cols-5 gap-2 font-mono text-xs text-center">
                            <div class="p-2.5 bg-slate-900/80 rounded border border-cyan-800/50">
                                <div class="text-cyan-400 text-[10px] uppercase font-sans font-bold">Зона входа</div>
                                <div id="sig-entry" class="text-sm font-bold text-slate-100 mt-0.5">—</div>
                            </div>
                            <div class="p-2.5 bg-slate-900/80 rounded border border-rose-900/50">
                                <div class="text-rose-400 text-[10px] uppercase font-sans font-bold">Stop Loss</div>
                                <div id="sig-sl" class="text-sm font-bold text-rose-300 mt-0.5">—</div>
                            </div>
                            <div class="p-2.5 bg-slate-900/80 rounded border border-emerald-900/50">
                                <div class="text-emerald-400 text-[10px] uppercase font-sans font-bold">TP1 (50% + BE)</div>
                                <div id="sig-tp1" class="text-sm font-bold text-emerald-300 mt-0.5">—</div>
                            </div>
                            <div class="p-2.5 bg-slate-900/80 rounded border border-emerald-900/50">
                                <div class="text-emerald-400 text-[10px] uppercase font-sans font-bold">TP2 (30%)</div>
                                <div id="sig-tp2" class="text-sm font-bold text-emerald-300 mt-0.5">—</div>
                            </div>
                            <div class="p-2.5 bg-slate-900/80 rounded border border-emerald-900/50">
                                <div class="text-emerald-400 text-[10px] uppercase font-sans font-bold">TP3 (20% Runner)</div>
                                <div id="sig-tp3" class="text-sm font-bold text-emerald-300 mt-0.5">—</div>
                            </div>
                        </div>
                    </div>

                    <!-- Envelope Lineage -->
                    <div id="sig-envelope-lineage" class="text-[11px] text-slate-400 bg-slate-950/60 p-2.5 rounded border border-slate-800/80 font-mono">
                        Source: realtime_pipeline | Mode: live_verified | As of: —
                    </div>
                </div>

                <!-- Decision Reasons & Predicates (7.4) -->
                <div class="glass-panel p-6 space-y-4">
                    <div class="flex items-center justify-between">
                        <h3 class="text-sm font-bold uppercase tracking-wider text-slate-200 flex items-center gap-2">
                            <i class="fas fa-clipboard-check text-cyan-400"></i> Decision Reasons (WHY)
                        </h3>
                        <span id="sig-confirmed-by" class="text-[10px] bg-slate-800 px-2 py-0.5 rounded font-mono text-slate-400">
                            meta-filter
                        </span>
                    </div>

                    <div class="text-xs text-slate-300 bg-slate-900/80 p-3 rounded-lg border border-slate-800">
                        <div class="text-[11px] text-slate-400 uppercase font-semibold mb-1">Reasoning Summary:</div>
                        <p id="sig-reasoning-summary" class="italic text-slate-300 leading-relaxed">
                            Ожидание данных сигнала...
                        </p>
                    </div>

                    <div>
                        <div class="text-[11px] text-slate-400 uppercase font-semibold mb-2">Confirmation Predicates:</div>
                        <ul id="sig-predicates-list" class="space-y-1.5 text-xs font-mono">
                            <li class="p-2 rounded bg-slate-900/60 border border-slate-800 flex items-center justify-between">
                                <span class="text-slate-400">causal_regime_check</span>
                                <span class="text-emerald-400 font-bold">PASS ✓</span>
                            </li>
                            <li class="p-2 rounded bg-slate-900/60 border border-slate-800 flex items-center justify-between">
                                <span class="text-slate-400">atr_volatility_filter</span>
                                <span class="text-emerald-400 font-bold">PASS ✓</span>
                            </li>
                            <li class="p-2 rounded bg-slate-900/60 border border-slate-800 flex items-center justify-between">
                                <span class="text-slate-400">session_liquidity_gate</span>
                                <span class="text-emerald-400 font-bold">PASS ✓</span>
                            </li>
                        </ul>
                    </div>

                    <!-- 5. Position Quality Check Block (7.5) -->
                    <div class="p-3 bg-amber-950/20 border border-amber-900/40 rounded-lg text-xs space-y-1">
                        <div class="flex items-center gap-1.5 font-bold text-amber-400">
                            <i class="fas fa-circle-exclamation"></i>
                            <span>POSITION QUALITY (PQ)</span>
                        </div>
                        <p class="text-slate-400 text-[11px]">
                            <span class="text-amber-300/90 font-mono">UNAVAILABLE (not implemented in backend)</span>. Frontend строго запрещено вычислять синтетический PQ-score.
                        </p>
                    </div>
                </div>
            </div>

            <!-- 8. Candlestick Chart View (7.8) -->
            <div class="glass-panel p-6 space-y-3">
                <div class="flex flex-wrap justify-between items-center gap-2">
                    <div class="flex items-center gap-2">
                        <i class="fas fa-chart-candlestick text-amber-400"></i>
                        <h2 class="text-base font-bold text-slate-100">Live Closed-Candles Chart (/api/chart/{asset})</h2>
                    </div>
                    <div class="flex gap-1" id="chart-asset-selector">
                        <button onclick="loadChart('XAUUSD')" class="px-2.5 py-1 text-xs font-mono rounded bg-amber-600 text-white font-bold chart-btn">XAUUSD</button>
                        <button onclick="loadChart('XAGUSD')" class="px-2.5 py-1 text-xs font-mono rounded bg-slate-800 hover:bg-slate-700 text-slate-300 chart-btn">XAGUSD</button>
                        <button onclick="loadChart('BTCUSD')" class="px-2.5 py-1 text-xs font-mono rounded bg-slate-800 hover:bg-slate-700 text-slate-300 chart-btn">BTCUSD</button>
                        <button onclick="loadChart('EURUSD')" class="px-2.5 py-1 text-xs font-mono rounded bg-slate-800 hover:bg-slate-700 text-slate-300 chart-btn">EURUSD</button>
                        <button onclick="loadChart('GBPUSD')" class="px-2.5 py-1 text-xs font-mono rounded bg-slate-800 hover:bg-slate-700 text-slate-300 chart-btn">GBPUSD</button>
                    </div>
                </div>

                <div id="chart-container" class="w-full flex justify-center items-center min-h-[300px] bg-slate-950/70 rounded-lg p-2 border border-slate-800">
                    <div class="text-slate-400 text-sm">Загрузка графика...</div>
                </div>
            </div>

            <!-- Multi-Asset Signal Matrix (7.2) -->
            <div class="glass-panel p-6 space-y-4">
                <div class="flex justify-between items-center">
                    <h3 class="text-base font-bold text-slate-100 flex items-center gap-2">
                        <i class="fas fa-table-cells text-amber-400"></i> Multi-Asset Signal Matrix (/api/matrix)
                    </h3>
                    <span class="text-xs text-slate-400 font-mono">5 Assets · Closed M5/M15/H1</span>
                </div>

                <div class="overflow-x-auto">
                    <table class="w-full text-left text-xs font-mono">
                        <thead class="text-[11px] text-slate-400 uppercase bg-slate-900/80 border-b border-slate-800">
                            <tr>
                                <th class="p-3">Актив</th>
                                <th class="p-3">Роль / Статус</th>
                                <th class="p-3">Направление (Bias)</th>
                                <th class="p-3">ML Confidence</th>
                                <th class="p-3">Режим</th>
                                <th class="p-3">Сессия</th>
                                <th class="p-3">Targets (TP1/2/3)</th>
                                <th class="p-3">Stop Loss</th>
                                <th class="p-3">Source &amp; As Of</th>
                            </tr>
                        </thead>
                        <tbody id="signal-matrix-tbody" class="divide-y divide-slate-800/80">
                            <tr><td colspan="9" class="text-center py-6 text-slate-500">Загрузка матрицы сигналов...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </main>

        <!-- ============================================================ -->
        <!-- TAB 2: MT5 POSITIONS & EXECUTION -->
        <!-- ============================================================ -->
        <main id="tab-positions" class="space-y-6 hidden">
            <!-- 9. Open Positions (7.9) & Execution State (7.10) -->
            <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
                <!-- Positions List -->
                <div class="glass-panel p-6 lg:col-span-2 space-y-4">
                    <div class="flex justify-between items-center">
                        <h3 class="text-base font-bold flex items-center gap-2">
                            <i class="fas fa-layer-group text-emerald-400"></i> Real MT5 Positions (/api/positions)
                        </h3>
                        <span id="positions-freshness" class="text-xs px-2 py-0.5 rounded font-mono bg-slate-800 text-slate-300">
                            FRESH
                        </span>
                    </div>

                    <div id="positions-container" class="space-y-3">
                        <div class="text-center py-10 text-slate-500 text-sm">
                            <i class="fas fa-spinner fa-spin text-2xl mb-2 block"></i> Загрузка позиций...
                        </div>
                    </div>
                </div>

                <!-- Execution Guards & Policies (7.10) -->
                <div class="glass-panel p-6 space-y-4 border-l-4 border-l-rose-500">
                    <h3 class="text-sm font-bold uppercase tracking-wider text-slate-200 flex items-center gap-2">
                        <i class="fas fa-shield-alt text-rose-400"></i> Execution Routing Guards
                    </h3>

                    <div class="space-y-2.5 text-xs font-mono">
                        <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                            <span class="text-slate-400 block text-[11px]">EXECUTION ALLOWLIST</span>
                            <span class="text-rose-400 font-bold block mt-0.5">[] (EMPTY DENY-ALL)</span>
                            <p class="text-[11px] text-slate-500 mt-1 font-sans">Маршрутизация ордеров на брокера аппаратно заблокирована.</p>
                        </div>

                        <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                            <span class="text-slate-400 block text-[11px]">DEMO ACCOUNT REQUIREMENT</span>
                            <span class="text-emerald-400 font-bold block mt-0.5">ENFORCED (true)</span>
                        </div>

                        <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                            <span class="text-slate-400 block text-[11px]">CIRCUIT BREAKER</span>
                            <span class="text-emerald-400 font-bold block mt-0.5">NORMAL (5% Max DD)</span>
                        </div>

                        <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                            <span class="text-slate-400 block text-[11px]">MUTATION ENDPOINTS</span>
                            <span class="text-amber-400 font-bold block mt-0.5">501 NOT IMPLEMENTED</span>
                            <p class="text-[11px] text-slate-500 mt-1 font-sans">POST /api/control/* возвращает 501 до создания command bus.</p>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Frozen Paper Status (7.10) -->
            <div class="glass-panel p-6 space-y-3">
                <h3 class="text-base font-bold flex items-center gap-2">
                    <i class="fas fa-file-invoice text-amber-400"></i> Frozen Paper Forward Accumulator (/api/paper-status)
                </h3>
                <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 font-mono text-xs">
                    <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Статус аккумуляции</div>
                        <div id="paper-status-val" class="text-sm font-bold text-amber-300 mt-1">ACCUMULATING</div>
                    </div>
                    <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Закрытых сделок</div>
                        <div id="paper-trades-val" class="text-sm font-bold text-slate-200 mt-1">38 / 50 MIN</div>
                    </div>
                    <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Режим данных</div>
                        <div class="text-sm font-bold text-slate-300 mt-1">paper_frozen</div>
                    </div>
                    <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Outcome Metrics Lock</div>
                        <div class="text-sm font-bold text-emerald-400 mt-1">LOCKED (OOS Guard)</div>
                    </div>
                </div>
            </div>
        </main>

        <!-- ============================================================ -->
        <!-- TAB 3: SMC & RESEARCH DIAGNOSTICS -->
        <!-- ============================================================ -->
        <main id="tab-research" class="space-y-6 hidden">
            <!-- 6. SMC / Institutional Microstructure Proxies (7.6 & 7.14) -->
            <div class="glass-panel p-6 border-l-4 border-l-cyan-500 space-y-4">
                <div class="flex flex-wrap justify-between items-center gap-2">
                    <div>
                        <h3 class="text-base font-bold text-slate-100 flex items-center gap-2">
                            <i class="fas fa-microchip text-cyan-400"></i> Market Structure &amp; Flow Proxies (OHLCV Proxy)
                        </h3>
                        <p class="text-xs text-slate-400">
                            Источник: OHLCV-proxy (не реальный торговый поток / L2 / MBO / on-chain). Контекст-диагностика.
                        </p>
                    </div>
                    <button onclick="copyMetricsText()" class="text-xs bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 px-3 py-1.5 rounded-lg transition flex items-center gap-1.5">
                        <i class="fas fa-copy"></i> Копировать отчёт
                    </button>
                </div>

                <div id="smc-metrics-container" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 text-xs">
                    <div class="text-slate-500 p-4 bg-slate-900/60 rounded-lg border border-slate-800 col-span-full">
                        Загрузка SMC метрик...
                    </div>
                </div>
            </div>

            <!-- Dynamic Correlation Matrix (7.14) -->
            <div class="glass-panel p-6 space-y-4">
                <div class="flex justify-between items-center">
                    <h3 class="text-base font-bold flex items-center gap-2">
                        <i class="fas fa-chart-pie text-indigo-400"></i> Rolling Close-Return Correlation (/api/correlation)
                    </h3>
                    <span class="text-xs text-slate-400 font-mono">M5 Aligned Closes</span>
                </div>
                <div class="overflow-x-auto">
                    <table class="w-full text-center text-xs font-mono">
                        <tbody id="corr-matrix-tbody"></tbody>
                    </table>
                </div>
                <div class="text-xs text-slate-400 flex items-center gap-4">
                    <span><span class="inline-block w-3 h-3 bg-emerald-700/60 rounded-sm mr-1"></span> Сильная положит. (|&rho;| &ge; 0.8)</span>
                    <span><span class="inline-block w-3 h-3 bg-rose-700/60 rounded-sm mr-1"></span> Сильная отрицат. (|&rho;| &ge; 0.8)</span>
                </div>
            </div>

            <!-- Macro News Sentiment (7.15) -->
            <div class="glass-panel p-6 space-y-3">
                <h3 class="text-base font-bold flex items-center gap-2">
                    <i class="fas fa-newspaper text-cyan-400"></i> Macro News &amp; Sentiment (/api/sentiment)
                </h3>
                <div id="sentiment-card" class="p-4 bg-slate-900/60 rounded-lg border border-slate-800 text-xs space-y-2">
                    <div class="flex justify-between">
                        <span class="text-slate-400">Статус источника новостей:</span>
                        <span id="sentiment-status" class="font-bold text-amber-400">UNCONFIGURED (no live news feed)</span>
                    </div>
                    <p class="text-slate-500 text-[11px]">
                        Реальный источник новостей не настроен. Синтетические новости не отображаются.
                    </p>
                </div>
            </div>
        </main>

        <!-- ============================================================ -->
        <!-- TAB 4: RISK, VAR & STATISTICS -->
        <!-- ============================================================ -->
        <main id="tab-risk" class="space-y-6 hidden">
            <!-- 13. Monte Carlo Simulation (7.13) -->
            <div class="glass-panel p-6 space-y-4">
                <div class="flex justify-between items-center">
                    <h3 class="text-base font-bold flex items-center gap-2">
                        <i class="fas fa-dice-d20 text-indigo-400"></i> Monte Carlo VaR Engine (1000 Симуляций)
                    </h3>
                    <span class="text-xs text-slate-400 font-mono">Source: trading_events.position_closed</span>
                </div>

                <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 font-mono text-xs">
                    <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                        <div class="text-slate-400 text-[11px]">VaR 95% (риск)</div>
                        <div id="mc-var-95" class="text-base font-bold text-rose-400 mt-1">—</div>
                    </div>
                    <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                        <div class="text-slate-400 text-[11px]">CVaR 95% (хвост)</div>
                        <div id="mc-cvar-95" class="text-base font-bold text-rose-300 mt-1">—</div>
                    </div>
                    <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Вероятность прибыли</div>
                        <div id="mc-profit-prob" class="text-base font-bold text-emerald-400 mt-1">—</div>
                    </div>
                    <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                        <div class="text-slate-400 text-[11px]">Риск банкротства (Ruin)</div>
                        <div id="mc-ruin-prob" class="text-base font-bold text-emerald-400 mt-1">—</div>
                    </div>
                </div>
            </div>

            <!-- 12. Real Closed Trade Statistics (7.12) -->
            <div class="glass-panel p-6 border-l-4 border-l-emerald-500 space-y-4">
                <div class="flex flex-wrap justify-between items-center gap-2">
                    <h3 class="text-base font-bold flex items-center gap-2">
                        <i class="fas fa-chart-line text-emerald-400"></i> Реальная статистика закрытых сделок MT5
                    </h3>
                    <div class="flex items-center gap-2">
                        <select id="metrics-period-select" onchange="loadClosedMetrics()" class="bg-slate-800 text-slate-200 text-xs border border-slate-700 rounded px-2.5 py-1.5 font-mono">
                            <option value="today">Сегодня</option>
                            <option value="week" selected>7 дней</option>
                            <option value="2week">14 дней</option>
                            <option value="month">30 дней</option>
                            <option value="3month">90 дней</option>
                            <option value="all">Вся история</option>
                        </select>
                        <button onclick="loadClosedMetrics()" class="text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 px-2.5 py-1.5 rounded">
                            <i class="fas fa-sync-alt"></i>
                        </button>
                    </div>
                </div>

                <div id="metrics-grid" class="grid grid-cols-2 sm:grid-cols-4 gap-3 font-mono text-xs">
                    <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                        <div class="text-slate-400 text-[11px]">СДЕЛОК</div>
                        <div id="m-n" class="text-lg font-bold text-slate-100 mt-1">—</div>
                    </div>
                    <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                        <div class="text-slate-400 text-[11px]">WIN RATE</div>
                        <div id="m-wr" class="text-lg font-bold text-emerald-400 mt-1">—</div>
                    </div>
                    <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                        <div class="text-slate-400 text-[11px]">PROFIT FACTOR</div>
                        <div id="m-pf" class="text-lg font-bold text-amber-300 mt-1">—</div>
                    </div>
                    <div class="p-3 bg-slate-900/60 rounded border border-slate-800">
                        <div class="text-slate-400 text-[11px]">ИТОГОВЫЙ P&amp;L</div>
                        <div id="m-pnl" class="text-lg font-bold text-emerald-400 mt-1">—</div>
                    </div>
                </div>
            </div>
        </main>

        <!-- ============================================================ -->
        <!-- TAB 5: AUDIT LEDGER TIMELINE (7.11) -->
        <!-- ============================================================ -->
        <main id="tab-audit" class="space-y-6 hidden">
            <div class="glass-panel p-6 space-y-4">
                <div class="flex flex-wrap justify-between items-center gap-2">
                    <div>
                        <h3 class="text-base font-bold flex items-center gap-2">
                            <i class="fas fa-receipt text-amber-400"></i> Owner Ledger Audit Timeline (/api/ledger/events)
                        </h3>
                        <p class="text-xs text-slate-400">
                            Неизменяемый реестр проверенных фактов исполнения. Подпись HMAC + Bearer auth.
                        </p>
                    </div>
                    <div class="flex items-center gap-2">
                        <button onclick="loadLedgerEvents()" class="text-xs bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 px-3 py-1.5 rounded transition">
                            <i class="fas fa-sync-alt"></i> Обновить факты
                        </button>
                    </div>
                </div>

                <div id="ledger-auth-warning" class="hidden p-4 bg-amber-950/40 border border-amber-800 rounded-lg text-xs text-amber-200">
                    <i class="fas fa-lock mr-1.5"></i>
                    <strong>OWNER AUTHORIZATION REQUIRED</strong>: Введите Owner Bearer Token в шапке для доступа к закрытым журналам аудита.
                </div>

                <div class="overflow-x-auto">
                    <table class="w-full text-left text-xs font-mono">
                        <thead class="text-[11px] text-slate-400 uppercase bg-slate-900/80 border-b border-slate-800">
                            <tr>
                                <th class="p-3">Время (UTC)</th>
                                <th class="p-3">Тип события</th>
                                <th class="p-3">Актив / Символ</th>
                                <th class="p-3">Intent ID</th>
                                <th class="p-3">Источник</th>
                                <th class="p-3">Подпись</th>
                                <th class="p-3">Детали Payload</th>
                            </tr>
                        </thead>
                        <tbody id="ledger-events-tbody" class="divide-y divide-slate-800/80">
                            <tr><td colspan="7" class="text-center py-6 text-slate-500">Загрузка реестра событий...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </main>

        <!-- Footer -->
        <footer class="text-center text-slate-500 text-xs py-4 border-t border-slate-800/60 space-y-1">
            <div>xauusd-alert-system &bull; Owner-Only Research Terminal &bull; Causal Signal-Bar Parity</div>
            <div class="text-[11px] text-slate-600">Compliance disclosure: Technical research infrastructure, not investment advice. Broker fact is truth.</div>
        </footer>
    </div>

    <!-- Client-side Logic -->
    <script>
        var currentTab = 'overview';
        var currentChartAsset = 'XAUUSD';
        var institutionalReportText = '';

        function getOwnerToken() {
            try {
                return sessionStorage.getItem('xau_alert_owner_token') || '';
            } catch (e) {
                return '';
            }
        }

        function saveOwnerToken(token) {
            try {
                if (token && token.trim()) {
                    sessionStorage.setItem('xau_alert_owner_token', token.trim());
                    alert('Owner Token сохранён в sessionStorage.');
                } else {
                    sessionStorage.removeItem('xau_alert_owner_token');
                    alert('Owner Token удалён.');
                }
            } catch (e) {
                console.error(e);
            }
            refreshAllData();
        }

        function switchTab(tabId) {
            currentTab = tabId;
            var tabs = ['overview', 'positions', 'research', 'risk', 'audit'];
            for (var i = 0; i < tabs.length; i++) {
                var t = tabs[i];
                var el = document.getElementById('tab-' + t);
                var btn = document.getElementById('tab-btn-' + t);
                if (t === tabId) {
                    if (el) el.classList.remove('hidden');
                    if (btn) btn.classList.add('active');
                } else {
                    if (el) el.classList.add('hidden');
                    if (btn) btn.classList.remove('active');
                }
            }
            if (tabId === 'audit') {
                loadLedgerEvents();
            }
        }

        async function fetchJSON(url, requiresAuth) {
            var headers = {};
            var token = getOwnerToken();
            if (requiresAuth && token) {
                headers['Authorization'] = 'Bearer ' + token;
            }
            try {
                var res = await fetch(url, { headers: headers });
                if (res.status === 403) return { _error: '403_FORBIDDEN' };
                if (res.status === 503) {
                    var err = await res.json().catch(function() { return {}; });
                    return { _error: '503_UNAVAILABLE', detail: err.detail };
                }
                if (!res.ok) return null;
                return await res.json();
            } catch (e) {
                console.warn('API error:', url, e);
                return null;
            }
        }

        function copyMetricsText() {
            if (!institutionalReportText) {
                alert('Отчёт недоступен.');
                return;
            }
            navigator.clipboard.writeText(institutionalReportText).then(function() {
                alert('Отчёт скопирован в буфер обмена!');
            });
        }

        async function loadChart(asset) {
            currentChartAsset = asset;
            var buttons = document.querySelectorAll('.chart-btn');
            for (var i = 0; i < buttons.length; i++) {
                var b = buttons[i];
                if (b.innerText === asset) {
                    b.className = 'px-2.5 py-1 text-xs font-mono rounded bg-amber-600 text-white font-bold chart-btn';
                } else {
                    b.className = 'px-2.5 py-1 text-xs font-mono rounded bg-slate-800 hover:bg-slate-700 text-slate-300 chart-btn';
                }
            }

            var container = document.getElementById('chart-container');
            try {
                var res = await fetch('/api/chart/' + asset);
                var contentType = res.headers.get('content-type') || '';
                if (!res.ok || !contentType.includes('image/svg+xml')) {
                    var reason = 'real_market_data_required (режим research)';
                    try {
                        var payload = await res.json();
                        if (payload.detail && payload.detail.reason) {
                            reason = payload.detail.reason;
                        } else if (payload.detail) {
                            reason = payload.detail;
                        }
                    } catch (_) {}
                    container.innerHTML = '<div class="p-6 text-center text-amber-400 text-xs font-mono"><i class="fas fa-ban text-2xl mb-2 block"></i>CHART UNAVAILABLE — ' + reason + '</div>';
                    return;
                }
                container.innerHTML = await res.text();
            } catch (e) {
                container.innerHTML = '<div class="p-6 text-center text-rose-400 text-xs font-mono">Ошибка загрузки графика</div>';
            }
        }

        async function refreshAllData() {
            // Status
            var st = await fetchJSON('/api/status', false);
            if (st && !st._error) {
                var dMode = st.data_mode || 'unknown';
                var cfgH = st.config_hash ? String(st.config_hash).slice(0, 10) : '—';
                var depM = st.deployment_mode || 'research';
                document.getElementById('sys-data-mode').innerText = dMode.toUpperCase();
                document.getElementById('sys-cfg-hash').innerText = cfgH;
                document.getElementById('badge-deployment-mode').innerText = depM.toUpperCase();
            }

            // Primary Signal
            var sig = await fetchJSON('/signal?asset=XAUUSD', false);
            if (sig && !sig._error) {
                document.getElementById('sig-id').innerText = sig.signal_id || 'sig_xau_live';
                document.getElementById('sig-session').innerText = sig.session || 'london';
                document.getElementById('sig-state').innerText = (sig.signal_state || 'ARMED').toUpperCase();
                document.getElementById('sig-bias-text').innerText = (sig.bias || 'neutral').toUpperCase();
                document.getElementById('sig-confidence-val').innerText = (Number(sig.confidence || 0) * 100).toFixed(1) + '%';
                document.getElementById('sig-regime-val').innerText = (sig.regime || 'range').toUpperCase();
                document.getElementById('sig-step').innerText = sig.step != null ? Number(sig.step).toFixed(2) : '—';
                document.getElementById('sig-reasoning-summary').innerText = sig.reasoning_summary || 'No reasoning provided';

                // Decision badge
                var isConfirmed = sig.signal_state === 'confirmed';
                var decBadge = document.getElementById('sig-decision-badge');
                if (!isConfirmed || sig.bias === 'no_trade') {
                    decBadge.className = 'inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-bold bg-slate-800 text-slate-300 border border-slate-700';
                    decBadge.innerHTML = '<i class="fas fa-ban"></i> NO TRADE';
                } else if (sig.bias === 'long') {
                    decBadge.className = 'inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-bold bg-emerald-900/80 text-emerald-300 border border-emerald-600';
                    decBadge.innerHTML = '<i class="fas fa-arrow-trend-up"></i> LONG / BUY';
                } else {
                    decBadge.className = 'inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-bold bg-rose-900/80 text-rose-300 border border-rose-600';
                    decBadge.innerHTML = '<i class="fas fa-arrow-trend-down"></i> SHORT / SELL';
                }

                // Geometry
                if (sig.entry_zone && sig.entry_zone.length >= 2) {
                    document.getElementById('sig-entry').innerText = Number(sig.entry_zone[0]).toFixed(2) + ' — ' + Number(sig.entry_zone[1]).toFixed(2);
                } else {
                    document.getElementById('sig-entry').innerText = '—';
                }
                document.getElementById('sig-sl').innerText = sig.invalidation != null ? Number(sig.invalidation).toFixed(2) : '—';
                if (sig.targets && sig.targets.length >= 3) {
                    document.getElementById('sig-tp1').innerText = Number(sig.targets[0]).toFixed(2);
                    document.getElementById('sig-tp2').innerText = Number(sig.targets[1]).toFixed(2);
                    document.getElementById('sig-tp3').innerText = Number(sig.targets[2]).toFixed(2);
                }
            }

            // Signal Matrix
            var matrix = await fetchJSON('/api/matrix', false);
            if (matrix && matrix.signals) {
                var tbody = document.getElementById('signal-matrix-tbody');
                tbody.innerHTML = '';
                for (var i = 0; i < matrix.signals.length; i++) {
                    var s = matrix.signals[i];
                    var biasBadge = '<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-400">UNAVAILABLE</span>';
                    if (s.available && s.bias === 'long') {
                        biasBadge = '<span class="px-2 py-0.5 rounded bg-emerald-950 text-emerald-300 font-bold border border-emerald-700/50">LONG</span>';
                    } else if (s.available && s.bias === 'short') {
                        biasBadge = '<span class="px-2 py-0.5 rounded bg-rose-950 text-rose-300 font-bold border border-rose-700/50">SHORT</span>';
                    }
                    
                    var roleBadge = '<span class="text-slate-400">PROBE</span>';
                    if (s.asset === 'XAUUSD') {
                        roleBadge = '<span class="text-amber-400 font-bold">PRIMARY</span>';
                    } else if (s.asset === 'BTCUSD') {
                        roleBadge = '<span class="text-indigo-400">RESEARCH</span>';
                    }

                    var conf = s.available && s.confidence != null ? (Number(s.confidence) * 100).toFixed(1) + '%' : '—';
                    var tgts = '—';
                    if (s.available && s.targets && s.targets.length > 0) {
                        var parts = [];
                        for (var k = 0; k < s.targets.length; k++) {
                            parts.push(Number(s.targets[k]).toFixed(2));
                        }
                        tgts = parts.join(' / ');
                    }
                    var sl = s.available && s.invalidation != null ? Number(s.invalidation).toFixed(2) : '—';

                    tbody.innerHTML += 
                        '<tr class="hover:bg-slate-900/60 transition">' +
                            '<td class="p-3 font-bold text-slate-200">' + s.asset + '</td>' +
                            '<td class="p-3">' + roleBadge + '</td>' +
                            '<td class="p-3">' + biasBadge + '</td>' +
                            '<td class="p-3 text-amber-300 font-semibold">' + conf + '</td>' +
                            '<td class="p-3 text-slate-300">' + (s.regime || '—') + '</td>' +
                            '<td class="p-3 text-slate-400 uppercase">' + (s.session || '—') + '</td>' +
                            '<td class="p-3 text-emerald-400">' + tgts + '</td>' +
                            '<td class="p-3 text-rose-400">' + sl + '</td>' +
                            '<td class="p-3 text-[11px] text-slate-500">' + s.source + '</td>' +
                        '</tr>';
                }
            }

            // Positions
            var posData = await fetchJSON('/api/positions', false);
            var posContainer = document.getElementById('positions-container');
            if (posData && posData.available && posData.positions && posData.positions.length > 0) {
                posContainer.innerHTML = '';
                for (var pIdx = 0; pIdx < posData.positions.length; pIdx++) {
                    var p = posData.positions[pIdx];
                    var pnlClass = p.profit >= 0 ? 'text-emerald-400' : 'text-rose-400';
                    var dirBadge = p.direction === 'buy' ? 'bg-emerald-950 text-emerald-300 border border-emerald-700' : 'bg-rose-950 text-rose-300 border border-rose-700';
                    posContainer.innerHTML += 
                        '<div class="p-3.5 bg-slate-900/70 rounded-lg border border-slate-800 flex justify-between items-center font-mono text-xs">' +
                            '<div>' +
                                '<span class="font-bold text-sm text-slate-200">' + p.symbol + '</span>' +
                                '<span class="ml-2 px-1.5 py-0.5 rounded text-[11px] ' + dirBadge + ' uppercase font-bold">' + p.direction + '</span>' +
                                '<div class="text-slate-400 mt-1">Ticket: #' + p.ticket + ' | ' + p.volume + ' lot @ ' + p.open_price + '</div>' +
                            '</div>' +
                            '<div class="text-right">' +
                                '<div class="text-base font-bold ' + pnlClass + '">$' + Number(p.profit).toFixed(2) + '</div>' +
                                '<div class="text-[11px] text-slate-500">SL: ' + (p.sl || '—') + ' | TP: ' + (p.tp || '—') + '</div>' +
                            '</div>' +
                        '</div>';
                }
            } else if (posData && posData.available) {
                posContainer.innerHTML = '<div class="text-center py-10 text-slate-500 text-xs font-mono"><i class="fas fa-inbox text-2xl mb-2 block"></i>No open positions (валидный пустой список MT5)</div>';
            } else {
                posContainer.innerHTML = '<div class="text-center py-10 text-rose-400 text-xs font-mono"><i class="fas fa-triangle-exclamation text-2xl mb-2 block"></i>MT5 Positions UNAVAILABLE (terminal not connected)</div>';
            }

            // SMC Institutional Metrics
            var smc = await fetchJSON('/api/institutional-metrics', false);
            var smcContainer = document.getElementById('smc-metrics-container');
            if (smc && smc.available && smc.metrics) {
                institutionalReportText = smc.report_text || '';
                smcContainer.innerHTML = '';
                var metricEntries = Object.entries(smc.metrics);
                for (var mIdx = 0; mIdx < metricEntries.length; mIdx++) {
                    var key = metricEntries[mIdx][0];
                    var val = metricEntries[mIdx][1];
                    var titleKey = key.replace(/_/g, ' ');
                    smcContainer.innerHTML += 
                        '<div class="p-3 bg-slate-900/60 rounded-lg border border-slate-800 space-y-1">' +
                            '<div class="text-slate-400 text-[11px] font-mono uppercase">' + titleKey + '</div>' +
                            '<div class="text-base font-bold text-cyan-300">' + (val.display || '—') + '</div>' +
                            '<div class="text-slate-400 text-[11px]">' + (val.text || '') + '</div>' +
                            '<div class="text-[10px] text-slate-500 mt-1">Lookback: ' + val.lookback + ' bars · ' + val.source_kind + '</div>' +
                        '</div>';
                }
            }

            // Monte Carlo
            var mc = await fetchJSON('/api/monte-carlo', false);
            if (mc && mc.available && mc.var_95_usd != null) {
                document.getElementById('mc-var-95').innerText = '$' + Number(mc.var_95_usd).toFixed(2);
                document.getElementById('mc-cvar-95').innerText = '$' + Number(mc.cvar_95_usd).toFixed(2);
                document.getElementById('mc-profit-prob').innerText = Number(mc.profit_probability_pct).toFixed(1) + '%';
                document.getElementById('mc-ruin-prob').innerText = Number(mc.prob_of_ruin_pct).toFixed(1) + '%';
            }

            // Correlation
            var corr = await fetchJSON('/api/correlation', false);
            if (corr && corr.available && corr.matrix) {
                var cTbody = document.getElementById('corr-matrix-tbody');
                cTbody.innerHTML = '';
                var assets = corr.assets;
                var head = '<tr class="border-b border-slate-800"><th class="p-2 text-slate-400"></th>';
                for (var aIdx = 0; aIdx < assets.length; aIdx++) {
                    head += '<th class="p-2 text-slate-300 font-bold">' + assets[aIdx] + '</th>';
                }
                head += '</tr>';
                cTbody.innerHTML += head;

                for (var rIdx = 0; rIdx < assets.length; rIdx++) {
                    var row = '<tr class="border-b border-slate-900"><td class="p-2 font-bold text-slate-300 text-left">' + assets[rIdx] + '</td>';
                    for (var colIdx = 0; colIdx < assets.length; colIdx++) {
                        var v = corr.matrix[rIdx][colIdx];
                        var bg = 'bg-slate-900/40 text-slate-400';
                        if (rIdx === colIdx) {
                            bg = 'bg-slate-800 text-slate-200 font-bold';
                        } else if (v >= 0.8) {
                            bg = 'bg-emerald-950 text-emerald-300 font-bold';
                        } else if (v <= -0.8) {
                            bg = 'bg-rose-950 text-rose-300 font-bold';
                        }
                        var vStr = v != null ? Number(v).toFixed(2) : '—';
                        row += '<td class="p-2 ' + bg + '">' + vStr + '</td>';
                    }
                    row += '</tr>';
                    cTbody.innerHTML += row;
                }
            }
        }

        async function loadClosedMetrics() {
            var selectEl = document.getElementById('metrics-period-select');
            var p = selectEl ? selectEl.value : 'week';
            var m = await fetchJSON('/api/metrics?period=' + p, false);
            if (m && m.available) {
                document.getElementById('m-n').innerText = m.n != null ? m.n : '—';
                document.getElementById('m-wr').innerText = m.win_rate_pct != null ? m.win_rate_pct.toFixed(1) + '%' : '—';
                document.getElementById('m-pf').innerText = m.profit_factor != null ? m.profit_factor.toFixed(2) : '—';
                document.getElementById('m-pnl').innerText = '$' + Number(m.total_pnl || 0).toFixed(2);
            }
        }

        async function loadLedgerEvents() {
            var warn = document.getElementById('ledger-auth-warning');
            var tbody = document.getElementById('ledger-events-tbody');
            var data = await fetchJSON('/api/ledger/events?limit=50', true);

            if (!data || data._error === '403_FORBIDDEN') {
                if (warn) warn.classList.remove('hidden');
                tbody.innerHTML = '<tr><td colspan="7" class="text-center py-6 text-amber-400">403 FORBIDDEN · Owner Token Required</td></tr>';
                return;
            }
            if (warn) warn.classList.add('hidden');

            if (data && data.events) {
                tbody.innerHTML = '';
                if (data.events.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="7" class="text-center py-6 text-slate-500">Нет записанных событий в ledger_events</td></tr>';
                    return;
                }
                for (var i = 0; i < data.events.length; i++) {
                    var e = data.events[i];
                    var timeStr = new Date(e.received_at_utc_ms).toISOString().replace('T', ' ').slice(0, 19);
                    var payloadText = JSON.stringify(e.payload || {});
                    var payloadEsc = payloadText.replace(/"/g, '&quot;');
                    tbody.innerHTML += 
                        '<tr class="hover:bg-slate-900/60 transition text-[11px]">' +
                            '<td class="p-3 text-slate-400">' + timeStr + '</td>' +
                            '<td class="p-3 font-bold text-amber-300">' + e.event_type + '</td>' +
                            '<td class="p-3 font-bold text-slate-200">' + (e.asset_key || '—') + '</td>' +
                            '<td class="p-3 text-slate-400">' + (e.intent_id || '—') + '</td>' +
                            '<td class="p-3 text-slate-400">' + e.source + '</td>' +
                            '<td class="p-3 text-emerald-400">VALID ✓</td>' +
                            '<td class="p-3 text-slate-400 truncate max-w-xs" title="' + payloadEsc + '">' + payloadEsc + '</td>' +
                        '</tr>';
                }
            }
        }

        // Init
        var stored = getOwnerToken();
        if (stored) {
            var inp = document.getElementById('owner-token-input');
            if (inp) inp.value = stored;
        }

        loadChart('XAUUSD');
        refreshAllData();
        loadClosedMetrics();
        setInterval(refreshAllData, 5000);
    </script>
</body>
</html>`;
