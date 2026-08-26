"""
Interactive Web Dashboard HTML/JS Generator for Real-Time Trading System.
Provides a modern, responsive UI with live metric cards, candlestick chart,
Smart Money Concepts (SMC) & Institutional Microstructure metrics,
Monte Carlo risk engine, macro sentiment analyzer, signal matrix,
correlation heatmap, position manager, and control action buttons.
"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>XAUUSD Multi-Asset ML Trading System</title>
    <!-- Apply saved theme BEFORE first paint to avoid a flash of the wrong theme. -->
    <script>
        (function () {
            try { if (localStorage.getItem("dashboard-theme") === "light") document.documentElement.classList.add("theme-light"); } catch (e) {}
        })();
    </script>
    <!-- Vendored locally (realtime/static/) — cdn.tailwindcss.com returns 403
         from this network, which stripped ALL styling from the page. -->
    <script src="/static/tailwind.js"></script>
    <script src="/static/chart.umd.min.js"></script>
    <link href="/static/fontawesome.min.css" rel="stylesheet">
    <style>
        body { background-color: #0b1120; color: #f8fafc; font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; }
        .glass-card { background: rgba(17, 24, 39, 0.75); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 12px; }
        .pulse-live { animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
        /* Responsive: the candlestick SVG has a fixed pixel width; scale it
           down on narrow viewports instead of overflowing the card. */
        #chart-container svg { max-width: 100%; height: auto; }
        /* ------------------------------------------------------------------
           Light theme override layer (html.theme-light). The markup is dark
           by design (Tailwind slate/amber utilities), so light mode remaps
           those color families via attribute selectors. Specificity is
           (0,2,0) thanks to the html.theme-light prefix, which beats the
           Tailwind utilities (0,1,0). Semantic accent colors are darkened for
           contrast on a light background.
        ------------------------------------------------------------------ */
        html.theme-light body { background-color: #eef2f7; color: #0f172a; }
        html.theme-light .glass-card { background: rgba(255, 255, 255, 0.85); border-color: rgba(15, 23, 42, 0.12); }
        html.theme-light [class*="text-slate-"] { color: #64748b; }
        html.theme-light [class*="text-slate-200"] { color: #1e293b; }
        html.theme-light [class*="text-slate-100"] { color: #0f172a; }
        html.theme-light [class*="bg-slate-"] { background-color: #e2e8f0; }
        html.theme-light [class*="hover:bg-slate-"] { background-color: #cbd5e1; }
        html.theme-light [class*="border-slate-"] { border-color: #cbd5e1; }
        html.theme-light [class*="divide-slate-"] { border-color: #cbd5e1; }
        html.theme-light [class*="text-amber-"] { color: #b45309; }
        html.theme-light [class*="bg-amber-"] { background-color: #fef3c7; }
        html.theme-light [class*="border-amber-"] { border-color: #fcd34d; }
        html.theme-light [class*="text-emerald-"] { color: #047857; }
        html.theme-light [class*="text-rose-"] { color: #be123c; }
        html.theme-light [class*="text-cyan-"] { color: #0e7490; }
        html.theme-light [class*="text-indigo-"] { color: #4338ca; }
        html.theme-light [class*="text-violet-"] { color: #6d28d9; }
        /* Clickable KPI cards: hover affordance + flash highlight of the
           section they scroll to. */
        .kpi-card { cursor: pointer; transition: border-color .2s ease, box-shadow .2s ease; }
        .kpi-card:hover { border-color: rgba(245, 158, 11, 0.55); box-shadow: 0 0 0 1px rgba(245, 158, 11, 0.25); }
        html.theme-light .kpi-card:hover { border-color: rgba(180, 83, 9, 0.5); box-shadow: 0 0 0 1px rgba(180, 83, 9, 0.25); }
        .kpi-card:focus-visible { outline: 2px solid rgba(245, 158, 11, 0.6); outline-offset: 2px; }
        .flash-target { animation: flashTarget 1.2s ease-out 1; }
        @keyframes flashTarget { 0% { box-shadow: 0 0 0 3px rgba(245, 158, 11, 0.65); } 100% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0); } }
    </style>
</head>
<body class="p-4 md:p-8">
    <div class="max-w-7xl mx-auto space-y-6">
        <!-- Header -->
        <header class="flex flex-col md:flex-row justify-between items-start md:items-center glass-card p-6 gap-4 border-l-4 border-l-amber-500">
            <div>
                <div class="flex flex-wrap items-center gap-3">
                    <span class="w-3 h-3 rounded-full bg-emerald-500 pulse-live"></span>
                    <h1 class="text-2xl md:text-3xl font-bold bg-gradient-to-r from-amber-400 via-yellow-200 to-amber-500 bg-clip-text text-transparent">
                        xauusd-alert-system
                    </h1>
                    <span class="text-xs bg-slate-800 border border-slate-700 text-slate-300 px-2.5 py-1 rounded-full font-mono">v2.1 QUANT PRO</span>
                    <button id="theme-toggle" onclick="toggleTheme()" title="Сменить тему (тёмная/светлая)" class="ml-auto text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 px-2.5 py-1 rounded-lg transition flex items-center gap-1.5">
                        <i id="theme-icon" class="fas fa-moon"></i>
                    </button>
                </div>
                <p class="text-slate-400 text-sm mt-1">Institutional Multi-Asset ML System &bull; Smart Money Concepts &bull; Causal No-Lookahead Architecture</p>
            </div>
            
            <!-- Internal diagnostic view: never a live terminal. Mutation controls
                 are disabled server-side (web-UI spec §11/§12); execution control
                 lives only in authenticated Telegram. -->
            <div class="text-xs text-slate-400 border border-amber-600/40 bg-amber-950/20 rounded-lg px-3 py-2">
                <i class="fas fa-lock"></i> INTERNAL DIAGNOSTIC VIEW — не является live-терминалом<br>
                <span class="text-slate-500">Управление исполнением: только авторизованный Telegram (браузерные controls отключены)</span>
            </div>
        </header>

        <!-- WebSocket live-stream status + push history -->
        <div class="glass-card p-4">
            <div class="flex flex-wrap items-center justify-between gap-2 mb-2">
                <div class="flex items-center gap-2 text-sm">
                    <span id="ws-status-dot" class="w-2.5 h-2.5 rounded-full bg-slate-500"></span>
                    <span class="text-slate-400">WebSocket:</span>
                    <span id="ws-status-text" class="font-mono text-slate-200">подключение...</span>
                </div>
                <div class="text-xs text-slate-400 font-mono">
                    пуши: <span id="ws-push-count" class="text-slate-200">0</span>
                    &nbsp;·&nbsp; последний: <span id="ws-last-push" class="text-slate-200">—</span>
                </div>
            </div>
            <div id="ws-history" class="max-h-36 overflow-y-auto text-[11px] font-mono space-y-1 text-slate-400 pr-1">
                <div class="text-slate-500">Ожидание пушей...</div>
            </div>
        </div>

        <!-- KPI Metrics Grid -->
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <div class="glass-card p-5 kpi-card" role="button" tabindex="0" onclick="scrollToSection('data-disclosure')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();scrollToSection('data-disclosure');}" title="Источник данных">
                <div class="flex justify-between items-center text-slate-400 text-xs font-semibold uppercase tracking-wider">
                    <span>Режим системы</span>
                    <i class="fas fa-server text-indigo-400 text-base"></i>
                </div>
                <div id="kpi-data-mode" class="text-2xl font-bold mt-2 text-indigo-300">UNKNOWN</div>
                <div class="text-xs text-slate-400 mt-1">Фактический источник указан ниже</div>
            </div>

            <div class="glass-card p-5 kpi-card" role="button" tabindex="0" onclick="scrollToSection('metrics-grid')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();scrollToSection('metrics-grid');}" title="Статистика закрытых сделок">
                <div class="flex justify-between items-center text-slate-400 text-xs font-semibold uppercase tracking-wider">
                    <span>Баланс / Эквити</span>
                    <i class="fas fa-wallet text-emerald-400 text-base"></i>
                </div>
                <div id="kpi-balance" class="text-2xl font-bold mt-2 text-emerald-400">—</div>
                <div id="kpi-equity" class="text-xs text-slate-400 mt-1">Эквити: —</div>
            </div>

            <div class="glass-card p-5 kpi-card" role="button" tabindex="0" onclick="scrollToSection('positions-list')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();scrollToSection('positions-list');}" title="Открытые позиции MT5">
                <div class="flex justify-between items-center text-slate-400 text-xs font-semibold uppercase tracking-wider">
                    <span>Открытые позиции</span>
                    <i class="fas fa-layer-group text-amber-400 text-base"></i>
                </div>
                <div id="kpi-positions" class="text-2xl font-bold mt-2 text-amber-300">0</div>
                <div class="text-xs text-slate-400 mt-1">Лимит: макс 3 позиции</div>
            </div>

            <div class="glass-card p-5 kpi-card" role="button" tabindex="0" onclick="scrollToSection('mc-section')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();scrollToSection('mc-section');}" title="Monte Carlo VaR">
                <div class="flex justify-between items-center text-slate-400 text-xs font-semibold uppercase tracking-wider">
                    <span>Risk Manager</span>
                    <i class="fas fa-shield-alt text-cyan-400 text-base"></i>
                </div>
                <div id="kpi-risk" class="text-2xl font-bold mt-2 text-emerald-400">NORMAL</div>
                <div class="text-xs text-slate-400 mt-1">Circuit Breaker: 5% Max DD</div>
            </div>
        </div>
        <div id="data-disclosure" class="text-xs text-amber-300 bg-amber-950/30 border border-amber-800/50 rounded-lg px-4 py-2">
            Source: unavailable | Mode: implemented_not_live_verified | As of: —
        </div>

        <!-- 🌟 SMART MONEY & INSTITUTIONAL MICROSTRUCTURE METRICS BLOCK 🌟 -->
        <div class="glass-card p-6 border-l-4 border-l-cyan-500">
            <div class="flex flex-col gap-2 sm:flex-row sm:justify-between sm:items-center mb-4">
                <div class="flex items-center gap-2.5">
                    <i class="fas fa-microchip text-cyan-400 text-lg"></i>
                    <h2 class="text-lg font-bold text-slate-100">Метрики по софту на текущий момент (Smart Money Concepts)</h2>
                </div>
                <button onclick="copyMetricsText()" class="text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 px-3 py-1.5 rounded-lg transition flex items-center gap-1.5 self-start sm:self-auto">
                    <i class="fas fa-copy"></i> Копировать отчёт
                </button>
            </div>
            
            <div id="institutional-metrics-container" class="text-sm text-slate-500 p-4 bg-slate-900/40 rounded-lg border border-slate-700/60">
                Реальные закрытые свечи недоступны — значения не подменяются демонстрационными.
            </div>
        </div>

        <!-- Live Visual Chart & Macro Sentiment Grid -->
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <!-- Interactive Visual Chart -->
            <div class="glass-card p-6 lg:col-span-2">
                <div class="flex flex-col gap-3 sm:flex-row sm:justify-between sm:items-center mb-3">
                    <div class="flex items-center gap-2">
                        <i class="fas fa-chart-candlestick text-amber-400"></i>
                        <h2 class="text-lg font-bold">Живой график M5 & Уровни входа</h2>
                    </div>
                    <div class="flex flex-wrap gap-1.5">
                        <button onclick="loadChart('XAUUSD')" class="px-2.5 py-1 text-xs font-mono rounded bg-slate-800 hover:bg-slate-700 active-asset">XAUUSD</button>
                        <button onclick="loadChart('XAGUSD')" class="px-2.5 py-1 text-xs font-mono rounded bg-slate-800 hover:bg-slate-700">XAGUSD</button>
                        <button onclick="loadChart('BTCUSD')" class="px-2.5 py-1 text-xs font-mono rounded bg-slate-800 hover:bg-slate-700">BTCUSD</button>
                        <button onclick="loadChart('EURUSD')" class="px-2.5 py-1 text-xs font-mono rounded bg-slate-800 hover:bg-slate-700">EURUSD</button>
                    </div>
                </div>
                <div id="chart-container" class="w-full flex justify-center items-center min-h-[280px] bg-slate-950/60 rounded-lg p-2 border border-slate-800">
                    <div class="text-slate-500 text-sm">Загрузка графика...</div>
                </div>
            </div>

            <!-- Macro AI Sentiment & Monte Carlo Widget -->
            <div class="glass-card p-6 space-y-6">
                <!-- Macro Sentiment -->
                <div>
                    <h2 class="text-base font-bold flex items-center gap-2 mb-3">
                        <i class="fas fa-newspaper text-cyan-400"></i> Macro AI Sentiment
                    </h2>
                    <div id="sentiment-card" class="p-3.5 bg-slate-800/60 rounded-lg border border-slate-700 text-xs space-y-2">
                        <div class="flex justify-between items-center">
                            <span class="text-slate-400">Настроение золота:</span>
                            <span id="sentiment-bias" class="font-bold text-emerald-400 uppercase">—</span>
                        </div>
                        <div class="flex justify-between items-center">
                            <span class="text-slate-400">Уверенность AI:</span>
                            <span id="sentiment-conf" class="font-mono text-amber-300">—</span>
                        </div>
                        <div id="sentiment-tags" class="text-slate-400 text-[11px] pt-1">
                            Реальный источник новостей не настроен
                        </div>
                    </div>
                </div>

                <!-- Monte Carlo Stress Test -->
                <div id="mc-section">
                    <h2 class="text-base font-bold flex items-center gap-2 mb-3">
                        <i class="fas fa-dice-d20 text-indigo-400"></i> Monte Carlo VaR (1000 симуляций)
                    </h2>
                    <div class="p-3.5 bg-slate-800/60 rounded-lg border border-slate-700 text-xs space-y-2 font-mono">
                        <div class="flex justify-between">
                            <span class="text-slate-400">VaR 95% (риск):</span>
                            <span id="mc-var95" class="text-rose-400">—</span>
                        </div>
                        <div class="flex justify-between">
                            <span class="text-slate-400">Вероятность прибыли:</span>
                            <span id="mc-prob" class="text-emerald-400 font-bold">—</span>
                        </div>
                        <div class="flex justify-between">
                            <span class="text-slate-400">Риск банкротства:</span>
                            <span id="mc-ruin" class="text-emerald-400">—</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Real-Time Signal Matrix -->
        <div class="glass-card p-6">
            <div class="flex flex-wrap justify-between items-center gap-2 mb-4">
                <h2 class="text-lg font-bold flex items-center gap-2">
                    <i class="fas fa-bolt text-amber-400"></i> Мульти-активная матрица сигналов (M5)
                </h2>
                <div class="flex items-center gap-2">
                    <button onclick="refreshData()" class="text-xs text-slate-400 hover:text-slate-200 transition flex items-center gap-1">
                        <i class="fas fa-sync-alt" id="refresh-icon"></i> Обновить
                    </button>
                    <span id="last-updated" class="text-xs text-slate-500 font-mono"></span>
                </div>
            </div>
            
            <div class="overflow-x-auto">
                <table class="w-full min-w-[760px] text-left text-sm text-slate-300">
                    <thead class="text-xs text-slate-400 uppercase bg-slate-800/50 border-b border-slate-700">
                        <tr>
                            <th class="py-3 px-4">Актив</th>
                            <th class="py-3 px-4">Направление</th>
                            <th class="py-3 px-4">Уверенность ML</th>
                            <th class="py-3 px-4">Режим рынка</th>
                            <th class="py-3 px-4">Сессия</th>
                            <th class="py-3 px-4">Цели (TP1 / TP2 / TP3)</th>
                            <th class="py-3 px-4">Stop Loss</th>
                        </tr>
                    </thead>
                    <tbody id="signal-matrix-body" class="divide-y divide-slate-800 font-mono text-xs">
                        <tr><td colspan="7" class="text-center py-6 text-slate-500">Загрузка данных...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- ML Probability Panel (per-asset) -->
        <div class="glass-card p-6" id="mlprob-section">
            <div class="flex flex-wrap justify-between items-center gap-2 mb-4">
                <h2 class="text-lg font-bold flex items-center gap-2">
                    <i class="fas fa-dice text-fuchsia-400"></i>
                    <span id="mlprob-title">XAUUSD</span> · P(long) / P(short)
                    <span class="text-xs text-slate-500 font-normal">raw ML · по закрытому бару</span>
                </h2>
                <div class="flex items-center gap-3">
                    <label class="text-xs text-slate-400" for="mlprob-asset">Актив</label>
                    <select id="mlprob-asset" class="bg-slate-800/60 border border-slate-600 rounded px-2 py-1 text-xs font-mono text-slate-200 focus:outline-none focus:border-amber-500/70">
                        <option value="XAUUSD" selected>XAUUSD</option>
                        <option value="BTCUSD">BTCUSD</option>
                        <option value="EURUSD">EURUSD</option>
                    </select>
                    <span id="mlprob-verdict" class="px-3 py-1 rounded text-xs font-bold border border-slate-600 bg-slate-800/60 text-slate-400">—</span>
                </div>
            </div>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div class="space-y-3">
                    <div>
                        <div class="flex justify-between text-xs font-mono mb-1">
                            <span class="text-emerald-400 font-bold">LONG</span>
                            <span id="mlprob-plong" class="text-emerald-300">—</span>
                        </div>
                        <div class="h-3 rounded bg-slate-800 overflow-hidden">
                            <div id="mlprob-plong-bar" class="h-full bg-emerald-500/80 transition-all duration-500" style="width:0%"></div>
                        </div>
                    </div>
                    <div>
                        <div class="flex justify-between text-xs font-mono mb-1">
                            <span class="text-rose-400 font-bold">SHORT</span>
                            <span id="mlprob-pshort" class="text-rose-300">—</span>
                        </div>
                        <div class="h-3 rounded bg-slate-800 overflow-hidden">
                            <div id="mlprob-pshort-bar" class="h-full bg-rose-500/80 transition-all duration-500" style="width:0%"></div>
                        </div>
                    </div>
                    <div class="text-[11px] text-slate-400 font-mono space-y-0.5">
                        <div>Пороги: lean-short <span id="mlprob-thr-min" class="text-amber-300">0.55</span> · floor <span id="mlprob-thr-floor" class="text-amber-300">0.62</span> · alert <span id="mlprob-thr-alert" class="text-amber-300">0.60</span></div>
                        <div id="mlprob-status" class="text-slate-500">—</div>
                        <div id="mlprob-meta" class="text-slate-500"></div>
                    </div>
                </div>
                <div>
                    <div class="text-[11px] text-slate-400 uppercase mb-1">История P(short) · последние бары</div>
                    <svg id="mlprob-spark" viewBox="0 0 300 80" preserveAspectRatio="none" class="w-full h-20 rounded border border-slate-700/60 bg-slate-900/40"></svg>
                </div>
            </div>
        </div>

        <!-- Correlation Heatmap & Positions -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <!-- Correlation Matrix -->
            <div class="glass-card p-6">
                <h2 class="text-lg font-bold flex items-center gap-2 mb-4">
                    <i class="fas fa-chart-pie text-indigo-400"></i> Динамическая матрица корреляций (M5)
                </h2>
                <div class="overflow-x-auto">
                    <table class="w-full text-center text-xs font-mono">
                        <tbody id="corr-matrix-body"></tbody>
                    </table>
                </div>
                <div class="mt-4 text-xs text-slate-400">
                    <span class="inline-block w-3 h-3 bg-emerald-700/60 rounded-sm mr-1"></span> Сильная положит. (|&rho;| &ge; 0.8)
                    <span class="inline-block w-3 h-3 bg-rose-700/60 rounded-sm mr-1 ml-3"></span> Сильная отрицат. (|&rho;| &ge; 0.8)
                </div>
            </div>

            <!-- Active Positions Monitor -->
            <div class="glass-card p-6">
                <h2 class="text-lg font-bold flex items-center gap-2 mb-4">
                    <i class="fas fa-tasks text-cyan-400"></i> Открытые позиции MT5
                </h2>
                <div id="positions-list" class="space-y-3">
                    <div class="text-center py-8 text-slate-500 text-sm">
                        <i class="fas fa-inbox text-2xl mb-2 block"></i> Нет открытых позиций
                    </div>
                </div>
            </div>
        </div>

        <!-- 📊 PAIRS MODEL — Statistical Pairs Trading Analytics -->
        <div class="glass-card p-6 border-l-4 border-l-violet-500">
            <div class="flex flex-col gap-3 sm:flex-row sm:justify-between sm:items-center mb-4">
                <div class="flex items-center gap-2.5">
                    <i class="fas fa-code-branch text-violet-400 text-lg"></i>
                    <h2 class="text-lg font-bold text-slate-100">PAIRS MODEL — Statistical Pair Analytics</h2>
                </div>
                <div class="flex flex-wrap items-center gap-2">
                    <select id="pairs-pair-select" onchange="loadPairsPair()" class="bg-slate-800 text-slate-200 text-xs border border-slate-700 rounded-lg px-2 py-1.5 font-mono">
                        <option value="">Загрузка...</option>
                    </select>
                    <select id="pairs-tf-select" onchange="loadPairsData()" class="bg-slate-800 text-slate-200 text-xs border border-slate-700 rounded-lg px-2 py-1.5">
                        <option value="H1" selected>H1</option>
                        <option value="D1">D1</option>
                    </select>
                    <select id="pairs-refresh-interval" onchange="setPairsRefreshInterval()" class="bg-slate-800 text-slate-200 text-xs border border-slate-700 rounded-lg px-2 py-1.5">
                        <option value="0">OFF</option>
                        <option value="30" selected>30s</option>
                        <option value="60">1m</option>
                        <option value="180">3m</option>
                        <option value="300">5m</option>
                    </select>
                    <button onclick="loadPairsData()" class="text-xs text-slate-400 hover:text-slate-200 transition flex items-center gap-1">
                        <i class="fas fa-sync-alt" id="pairs-refresh-icon"></i>
                    </button>
                </div>
            </div>
            <div id="pairs-disclosure" class="text-xs text-slate-500 mb-3">pairs_analysis module • PairAnalyzer + SignalEngine + 6-Engine Ensemble</div>

            <div id="pairs-container" class="grid grid-cols-1 lg:grid-cols-3 gap-4">
                <!-- Left: Parameters + Signal -->
                <div class="space-y-4">
                    <div class="p-4 bg-slate-800/60 rounded-lg border border-slate-700">
                        <h3 class="text-xs font-bold text-violet-300 uppercase tracking-wider mb-3">Parameters</h3>
                        <div class="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs font-mono">
                            <div class="flex justify-between"><span class="text-slate-400">β (Kalman)</span><span id="p-beta" class="text-slate-200 font-bold">—</span></div>
                            <div class="flex justify-between"><span class="text-slate-400">z-score</span><span id="p-z" class="text-slate-200 font-bold">—</span></div>
                            <div class="flex justify-between"><span class="text-slate-400">half-life</span><span id="p-hl" class="text-slate-200 font-bold">—</span></div>
                            <div class="flex justify-between"><span class="text-slate-400">μ (spread)</span><span id="p-mu" class="text-slate-200 font-bold">—</span></div>
                            <div class="flex justify-between"><span class="text-slate-400">θ</span><span id="p-theta" class="text-slate-200 font-bold">—</span></div>
                            <div class="flex justify-between"><span class="text-slate-400">σ (spread)</span><span id="p-sigma" class="text-slate-200 font-bold">—</span></div>
                            <div class="flex justify-between"><span class="text-slate-400">ADF p</span><span id="p-adf" class="font-bold">—</span></div>
                            <div class="flex justify-between"><span class="text-slate-400">σ annual</span><span id="p-sigma-ann" class="text-slate-200 font-bold">—</span></div>
                            <div class="flex justify-between"><span class="text-slate-400">ratio</span><span id="p-ratio" class="text-slate-200 font-bold">—</span></div>
                            <div class="flex justify-between"><span class="text-slate-400">P1 / P2</span><span id="p-p1p2" class="text-slate-200 font-bold">—</span></div>
                        </div>
                        <div id="p-formula" class="text-[11px] text-slate-500 mt-2 pt-2 border-t border-slate-700 font-mono"></div>
                    </div>

                    <!-- Signal -->
                    <div class="p-4 bg-slate-800/60 rounded-lg border border-slate-700">
                        <h3 class="text-xs font-bold text-violet-300 uppercase tracking-wider mb-3">Signal</h3>
                        <div id="p-signal-bar" class="text-center py-2 px-3 rounded-lg text-sm font-bold">—</div>
                        <div class="text-[11px] text-slate-500 mt-2">Entry z: ±2.0σ • Exit z: 0.0 • Stop: ±3.0σ</div>
                    </div>

                    <!-- Math Board -->
                    <div class="p-4 bg-slate-800/60 rounded-lg border border-slate-700">
                        <h3 class="text-xs font-bold text-violet-300 uppercase tracking-wider mb-3">Math Board</h3>
                        <div class="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs font-mono">
                            <div class="flex justify-between"><span class="text-slate-400">Hurst (R/S)</span><span id="p-hurst" class="text-slate-200 font-bold">—</span></div>
                            <div class="flex justify-between"><span class="text-slate-400">ACF(1)</span><span id="p-acf1" class="text-slate-200 font-bold">—</span></div>
                            <div class="flex justify-between"><span class="text-slate-400">Skew</span><span id="p-skew" class="text-slate-200 font-bold">—</span></div>
                            <div class="flex justify-between"><span class="text-slate-400">Ex-Kurt</span><span id="p-exkurt" class="text-slate-200 font-bold">—</span></div>
                            <div class="flex justify-between"><span class="text-slate-400">σ realized</span><span id="p-rvol" class="text-slate-200 font-bold">—</span></div>
                        </div>
                        <div id="p-hurst-badge" class="mt-2 text-[11px] px-2 py-1 rounded inline-block">—</div>
                    </div>
                </div>

                <!-- Center: Z-Score Chart -->
                <div class="p-4 bg-slate-800/60 rounded-lg border border-slate-700 lg:col-span-1">
                    <h3 class="text-xs font-bold text-violet-300 uppercase tracking-wider mb-3">Z-Score (120 bars)</h3>
                    <div style="position:relative;height:300px;">
                        <canvas id="pairs-z-chart"></canvas>
                    </div>
                </div>

                <!-- Right: Ensemble -->
                <div class="p-4 bg-slate-800/60 rounded-lg border border-slate-700">
                    <h3 class="text-xs font-bold text-violet-300 uppercase tracking-wider mb-3">Ensemble — 6 Engine Forecasts</h3>
                    <div class="overflow-x-auto">
                    <table class="w-full min-w-[440px] text-xs font-mono">
                        <thead><tr class="text-slate-400 border-b border-slate-700">
                            <th class="text-left py-1.5">Engine</th>
                            <th class="text-left py-1.5">Dir</th>
                            <th class="text-left py-1.5">Conf</th>
                            <th class="text-left py-1.5">Key</th>
                        </tr></thead>
                        <tbody id="p-ensemble-body"></tbody>
                    </table>
                    </div>
                    <div id="p-ensemble-summary" class="mt-3 text-center text-sm font-bold">—</div>
                </div>
            </div>

            <!-- Equity Curve + Stats -->
            <div class="grid grid-cols-1 lg:grid-cols-4 gap-4 mt-4">
                <div class="lg:col-span-3 p-4 bg-slate-800/60 rounded-lg border border-slate-700">
                    <div class="flex justify-between items-center mb-2">
                        <h3 class="text-xs font-bold text-violet-300 uppercase tracking-wider">Equity Curve (cumulative R)</h3>
                        <span id="p-equity-count" class="text-[11px] text-slate-500"></span>
                    </div>
                    <div style="position:relative;height:140px;">
                        <canvas id="p-equity-chart"></canvas>
                    </div>
                </div>
                <div class="p-4 bg-slate-800/60 rounded-lg border border-slate-700">
                    <h3 class="text-xs font-bold text-violet-300 uppercase tracking-wider mb-3">Pair Stats</h3>
                    <div class="space-y-1.5 text-xs font-mono">
                        <div class="flex justify-between"><span class="text-slate-400">Trades</span><span id="p-st-n" class="text-slate-200 font-bold">—</span></div>
                        <div class="flex justify-between"><span class="text-slate-400">Win Rate</span><span id="p-st-wr" class="text-slate-200 font-bold">—</span></div>
                        <div class="flex justify-between"><span class="text-slate-400">Avg R</span><span id="p-st-avr" class="text-slate-200 font-bold">—</span></div>
                        <div class="flex justify-between"><span class="text-slate-400">Sum R</span><span id="p-st-sum" class="text-slate-200 font-bold">—</span></div>
                        <div class="flex justify-between"><span class="text-slate-400">Best</span><span id="p-st-best" class="text-emerald-400 font-bold">—</span></div>
                        <div class="flex justify-between"><span class="text-slate-400">Worst</span><span id="p-st-worst" class="text-rose-400 font-bold">—</span></div>
                        <div class="flex justify-between"><span class="text-slate-400">Profit Factor</span><span id="p-st-pf" class="text-slate-200 font-bold">—</span></div>
                    </div>
                </div>
            </div>
        </div>

        <!-- 📈 Real Trade Statistics (owner request 2026-08-11) -->
        <div class="glass-card p-6 border-l-4 border-l-emerald-500">
            <div class="flex flex-col gap-3 sm:flex-row sm:justify-between sm:items-center mb-4">
                <h2 class="text-lg font-bold flex items-center gap-2">
                    <i class="fas fa-chart-line text-emerald-400"></i> Реальная статистика закрытых сделок
                </h2>
                <div class="flex flex-wrap items-center gap-2">
                    <select id="metrics-period" onchange="loadMetrics()" class="bg-slate-800 text-slate-200 text-xs border border-slate-700 rounded-lg px-2 py-1.5">
                        <option value="today">Сегодня</option>
                        <option value="week" selected>7 дней</option>
                        <option value="2week">14 дней</option>
                        <option value="month">30 дней</option>
                        <option value="3month">90 дней</option>
                        <option value="all">Вся история</option>
                    </select>
                    <button onclick="loadMetrics()" class="text-xs text-slate-400 hover:text-slate-200 transition flex items-center gap-1">
                        <i class="fas fa-sync-alt"></i> Обновить
                    </button>
                </div>
            </div>
            <div id="metrics-available" class="hidden text-xs text-amber-400 mb-3">
                ⚠️ MT5 не подключён — реальных данных нет. Запустите на машине с терминалом.
            </div>
            <div id="metrics-grid" class="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs font-mono">
                <div class="p-3 bg-slate-800/60 rounded-lg border border-slate-700">
                    <div class="text-slate-400 font-sans text-[11px] uppercase">Сделок</div>
                    <div id="m-n" class="text-xl font-bold text-slate-100 mt-1">—</div>
                </div>
                <div class="p-3 bg-slate-800/60 rounded-lg border border-slate-700">
                    <div class="text-slate-400 font-sans text-[11px] uppercase">Win rate</div>
                    <div id="m-wr" class="text-xl font-bold text-emerald-400 mt-1">—</div>
                </div>
                <div class="p-3 bg-slate-800/60 rounded-lg border border-slate-700">
                    <div class="text-slate-400 font-sans text-[11px] uppercase">Profit factor</div>
                    <div id="m-pf" class="text-xl font-bold text-amber-300 mt-1">—</div>
                </div>
                <div class="p-3 bg-slate-800/60 rounded-lg border border-slate-700">
                    <div class="text-slate-400 font-sans text-[11px] uppercase">Итоговый P&L</div>
                    <div id="m-pnl" class="text-xl font-bold text-slate-100 mt-1">—</div>
                </div>
                <div class="p-3 bg-slate-800/60 rounded-lg border border-slate-700">
                    <div class="text-slate-400 font-sans text-[11px] uppercase">Средний выигрыш</div>
                    <div id="m-awin" class="text-lg font-bold text-emerald-400 mt-1">—</div>
                </div>
                <div class="p-3 bg-slate-800/60 rounded-lg border border-slate-700">
                    <div class="text-slate-400 font-sans text-[11px] uppercase">Средний убыток</div>
                    <div id="m-aloss" class="text-lg font-bold text-rose-400 mt-1">—</div>
                </div>
                <div class="p-3 bg-slate-800/60 rounded-lg border border-slate-700">
                    <div class="text-slate-400 font-sans text-[11px] uppercase">Макс. просадка</div>
                    <div id="m-dd" class="text-lg font-bold text-rose-300 mt-1">—</div>
                </div>
                <div class="p-3 bg-slate-800/60 rounded-lg border border-slate-700">
                    <div class="text-slate-400 font-sans text-[11px] uppercase">Подряд убытков</div>
                    <div id="m-consec" class="text-lg font-bold text-slate-100 mt-1">—</div>
                </div>
            </div>
            <div class="mt-3 text-[11px] text-slate-400">
                <span id="m-period-label"></span> &bull; Expectancy <span id="m-exp" class="text-slate-300 font-mono">—</span> &bull; Лучшая <span id="m-best" class="text-emerald-400 font-mono">—</span> &bull; Худшая <span id="m-worst" class="text-rose-400 font-mono">—</span>
            </div>
        </div>

        <!-- Footer -->
        <footer class="text-center text-slate-500 text-xs py-4">
            xauusd-alert-system &bull; Causal ML Inference Pipeline &bull; Purged Time-Split Calibration &bull; 204 Passed Tests
        </footer>
    </div>

    <script>
        let currentChartAsset = "XAUUSD";
        let institutionalReportText = "";

        async function fetchJSON(url) {
            // Hard 8s timeout per request so a slow endpoint (e.g. the news feed
            // behind /api/sentiment) can never stall the refresh cycle forever.
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 8000);
            try {
                const res = await fetch(url, { signal: controller.signal });
                return await res.json();
            } catch(e) {
                console.error("Fetch error for " + url, e);
                return null;
            } finally {
                clearTimeout(timer);
            }
        }

        function copyMetricsText() {
            if (!institutionalReportText) {
                alert("Реальные institutional metrics сейчас недоступны.");
                return;
            }
            navigator.clipboard.writeText(institutionalReportText).then(() => {
                alert("✅ Отчёт скопирован в буфер обмена!");
            });
        }

        async function loadChart(asset) {
            currentChartAsset = asset;
            const container = document.getElementById("chart-container");
            try {
                const res = await fetch("/api/chart/" + asset);
                const contentType = res.headers.get("content-type") || "";
                if (!res.ok || !contentType.includes("image/svg+xml")) {
                    let reason = "реальные свечи недоступны";
                    try {
                        const payload = await res.json();
                        reason = payload.detail?.reason || payload.detail || reason;
                    } catch (_) {}
                    container.innerHTML = `<div class="text-amber-400 text-sm">График недоступен: ${reason}</div>`;
                    return;
                }
                container.innerHTML = await res.text();
            } catch(e) {
                container.innerHTML = '<div class="text-rose-400 text-sm">Ошибка загрузки графика</div>';
            }
        }        // Live status applied from BOTH sources: the WebSocket push stream
        // (instant) and the /api/status REST fallback when WS is unavailable.
        function applyStatus(status) {
            if (!status) return;
            document.getElementById("kpi-data-mode").innerText = String(status.data_mode || "unknown").toUpperCase();
            document.getElementById("kpi-balance").innerText = status.balance == null ? "—" : "$" + status.balance.toLocaleString('en-US', {minimumFractionDigits: 2});
            document.getElementById("kpi-equity").innerText = status.equity == null ? "Эквити: —" : "Эквити: $" + status.equity.toLocaleString('en-US', {minimumFractionDigits: 2});
            document.getElementById("data-disclosure").innerText = "Source: " + (status.source || "unknown") + " | Data: " + (status.mode || "unknown") + " | Deployment: " + (status.deployment_mode || "unknown") + " | Strategy: " + (status.strategy_version || "unknown") + " | Config: " + String(status.config_hash || "").slice(0,12) + " | As of: " + (status.as_of_utc || "—");
            document.getElementById("kpi-positions").innerText = status.open_positions_count;
            document.getElementById("kpi-risk").innerText = status.circuit_breaker ? "HALTED" : "NORMAL";
            document.getElementById("kpi-risk").className = status.circuit_breaker ? "text-2xl font-bold mt-2 text-rose-500" : "text-2xl font-bold mt-2 text-emerald-400";
            if (status.positions) {
                renderPositions({ available: !!status.available, positions: status.positions });
            }
        }

        function renderPositions(posData) {
            const posContainer = document.getElementById("positions-list");
            if (posData && posData.available && posData.positions && posData.positions.length > 0) {
                posContainer.innerHTML = "";
                for (const pos of posData.positions) {
                    const pnlClass = pos.profit >= 0 ? "text-emerald-400" : "text-rose-400";
                    posContainer.innerHTML += `
                        <div class="p-3 bg-slate-800/50 rounded-lg border border-slate-700/50 flex justify-between items-center text-xs font-mono">
                            <div>
                                <span class="font-bold text-sm text-slate-200">${pos.symbol}</span>
                                <span class="ml-2 px-1.5 py-0.5 rounded ${pos.direction === 'buy' ? 'bg-emerald-900 text-emerald-300' : 'bg-rose-900 text-rose-300'} uppercase">${pos.direction}</span>
                                <div class="text-slate-400 mt-1">Объём: ${pos.volume} lot | Вход: ${pos.open_price}</div>
                            </div>
                            <div class="text-right">
                                <div class="text-sm font-bold ${pnlClass}">$${pos.profit.toFixed(2)}</div>
                                <div class="text-slate-500">SL: ${pos.sl || '-'} | TP: ${pos.tp || '-'}</div>
                            </div>
                        </div>
                    `;
                }
            } else if (posData && posData.available) {
                posContainer.innerHTML = `<div class="text-center py-8 text-slate-500 text-sm"><i class="fas fa-inbox text-2xl mb-2 block"></i> Нет открытых позиций</div>`;
            } else {
                posContainer.innerHTML = `<div class="text-center py-8 text-amber-400 text-sm">MT5 positions недоступны</div>`;
            }
        }

        // Fallback REST refresh of the live status when the WS stream is down.
        async function refreshStatus() {
            const s = await fetchJSON("/api/status");
            applyStatus(s);
            updateFreshness();
        }

        // ---- WebSocket status indicator + push history ---------------------
        let ws = null;
        let wsReconnectDelay = 2000;
        let wsPushCount = 0;

        function setWSStatus(stateCls, text) {
            const dot = document.getElementById("ws-status-dot");
            const txt = document.getElementById("ws-status-text");
            if (dot) dot.className = "w-2.5 h-2.5 rounded-full " + stateCls;
            if (txt) txt.innerText = text;
        }

        function renderWSPush(record) {
            const hist = document.getElementById("ws-history");
            if (!hist || !record) return;
            const ts = record.ts || new Date().toISOString();
            const time = new Date(ts).toLocaleTimeString("ru-RU");
            const money = record.balance == null ? "—" : "$" + Number(record.balance).toLocaleString('en-US', {minimumFractionDigits: 2});
            const changed = (Array.isArray(record.changed) && record.changed.length)
                ? record.changed.map(c => c.replace("open_positions_count", "позиции")).join(", ")
                : "—";
            const line = document.createElement("div");
            line.className = "flex flex-wrap gap-x-3 gap-y-0.5 justify-between border-b border-slate-800/60 pb-1";
            line.innerHTML =
                `<span class="text-slate-500">${time}</span>` +
                `<span class="text-emerald-400">${money}</span>` +
                `<span class="text-slate-400">позиции: ${record.open_positions_count ?? "—"}</span>` +
                `<span class="text-amber-300/90">изм: ${changed}</span>`;
            hist.prepend(line);
            while (hist.children.length > 20) hist.removeChild(hist.lastChild);
        }

        async function loadWSHistory() {
            const h = await fetchJSON("/api/ws-history");
            if (!h || !Array.isArray(h.pushes)) return;
            const hist = document.getElementById("ws-history");
            if (hist) hist.innerHTML = "";
            wsPushCount = h.pushes.length;
            // chronological order + prepend => newest on top
            for (const r of h.pushes.slice(-20)) renderWSPush(r);
            const cnt = document.getElementById("ws-push-count");
            if (cnt) cnt.innerText = wsPushCount;
        }

        // WebSocket push stream: instant balance/positions updates without
        // polling. Reconnects with exponential backoff (2s -> 30s max).
        function connectDashboardWS() {
            try {
                if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
            } catch(e) {}
            const proto = location.protocol === "https:" ? "wss" : "ws";
            ws = new WebSocket(proto + "://" + location.host + "/ws/dashboard");
            ws.onopen = () => {
                wsReconnectDelay = 2000;
                setWSStatus("bg-emerald-400", "онлайн");
                updateFreshness();
            };
            ws.onmessage = (ev) => {
                try {
                    const msg = JSON.parse(ev.data);
                    if (msg.type === "status" && msg.payload) {
                        applyStatus(msg.payload);
                        renderWSPush(msg.record);
                        wsPushCount++;
                        const cnt = document.getElementById("ws-push-count");
                        if (cnt) cnt.innerText = wsPushCount;
                        const lp = document.getElementById("ws-last-push");
                        if (lp) lp.innerText = new Date(msg.record?.ts || Date.now()).toLocaleTimeString("ru-RU");
                        updateFreshness();
                    }
                } catch(e) { console.error("WS message parse error", e); }
            };
            ws.onclose = () => {
                setWSStatus("bg-amber-400", "переподключение (" + Math.round(wsReconnectDelay / 1000) + "с)...");
                const d = wsReconnectDelay;
                wsReconnectDelay = Math.min(d * 2, 30000);
                setTimeout(connectDashboardWS, d);
            };
            ws.onerror = () => { try { ws.close(); } catch(e) {} };
        }

        // Signal matrix = ML asset rows + live Pairs Model rows (pulled from
        // the latest /api/pairs payload so the pairs data is visible in the
        // main matrix, not only in the dedicated PAIRS panel).
        function renderMatrix(matrix) {
            const tbody = document.getElementById("signal-matrix-body");
            if (!tbody) return;
            let rows = "";
            if (matrix && matrix.signals) {
                for (const item of matrix.signals) {
                    let biasBadge = item.available
                        ? '<span class="px-2 py-0.5 rounded bg-slate-700 text-slate-300">NEUTRAL</span>'
                        : '<span class="px-2 py-0.5 rounded bg-amber-900/60 text-amber-300">UNAVAILABLE</span>';
                    if (item.available && item.bias === "long") biasBadge = '<span class="px-2 py-0.5 rounded bg-emerald-900/60 text-emerald-300 font-bold border border-emerald-700/50">LONG / BUY</span>';
                    if (item.available && item.bias === "short") biasBadge = '<span class="px-2 py-0.5 rounded bg-rose-900/60 text-rose-300 font-bold border border-rose-700/50">SHORT / SELL</span>';

                    const confidence = item.available ? (Number(item.confidence) * 100).toFixed(1) + "%" : "—";
                    const targets = item.available && item.targets ? item.targets.map(t => t.toFixed(2)).join(" / ") : "-";
                    const sl = item.available && item.invalidation ? item.invalidation.toFixed(2) : "-";

                    rows += `
                        <tr class="hover:bg-slate-800/40 transition">
                            <td class="py-3 px-4 font-bold text-slate-200">${item.asset}</td>
                            <td class="py-3 px-4">${biasBadge}</td>
                            <td class="py-3 px-4 text-amber-300 font-semibold">${confidence}</td>
                            <td class="py-3 px-4 text-slate-300"><span class="bg-slate-800 px-2 py-0.5 rounded border border-slate-700">${item.regime}</span></td>
                            <td class="py-3 px-4 text-slate-400 uppercase">${item.session}</td>
                            <td class="py-3 px-4 text-emerald-400 font-mono">${targets}</td>
                            <td class="py-3 px-4 text-rose-400 font-mono">${sl}</td>
                        </tr>
                    `;
                }
            }
            // Pairs Model group — data pulled live from /api/pairs
            if (pairsData && pairsData.available && pairsData.pairs && pairsData.pairs.length) {
                const tf = (document.getElementById('pairs-tf-select') || {value:'H1'}).value;
                rows += '<tr class="bg-slate-800/30"><td colspan="7" class="py-2 px-4 text-[11px] font-bold uppercase tracking-wider text-violet-300">'
                    + 'Pairs Model (' + pairsData.pairs.length + ' пар · ' + tf + ')</td></tr>';
                for (const p of pairsData.pairs) {
                    const dir = p.signal_direction || p.ensemble_direction || "neutral";
                    let badge = '<span class="px-2 py-0.5 rounded bg-slate-700 text-slate-300">NEUTRAL</span>';
                    if (dir === "long") badge = '<span class="px-2 py-0.5 rounded bg-emerald-900/60 text-emerald-300 font-bold border border-emerald-700/50">LONG / BUY</span>';
                    if (dir === "short") badge = '<span class="px-2 py-0.5 rounded bg-rose-900/60 text-rose-300 font-bold border border-rose-700/50">SHORT / SELL</span>';
                    const z = (p.z == null) ? NaN : Number(p.z);
                    const zStr = isNaN(z) ? "—" : z.toFixed(2) + "σ";
                    const zCls = (!isNaN(z) && Math.abs(z) >= 2) ? "text-amber-300 font-bold" : "text-slate-300";
                    const hLabel = (p.hurst == null) ? "—" : (p.hurst < 0.5 ? "mean-revert" : p.hurst > 0.5 ? "trending" : "random");
                    const ensConf = (p.ensemble_confidence != null) ? p.ensemble_confidence.toFixed(0) + "%" : "—";
                    rows += '<tr class="hover:bg-slate-800/40 transition">' +
                        '<td class="py-3 px-4 font-bold text-violet-200">' + (p.name || "?") + '</td>' +
                        '<td class="py-3 px-4">' + badge + '</td>' +
                        '<td class="py-3 px-4 font-mono ' + zCls + '">' + zStr +
                        ' <span class="text-slate-500 text-[10px]">(' + ensConf + ')</span></td>' +
                        '<td class="py-3 px-4"><span class="bg-violet-900/40 text-violet-300 px-2 py-0.5 rounded border border-violet-700/50">' + hLabel + '</span></td>' +
                        '<td class="py-3 px-4 text-slate-500">pair</td>' +
                        '<td class="py-3 px-4 text-slate-500">—</td>' +
                        '<td class="py-3 px-4 text-slate-500">—</td>' +
                        '</tr>';
                }
            }
            tbody.innerHTML = rows || '<tr><td colspan="7" class="text-center py-6 text-slate-500">Загрузка данных...</td></tr>';
        }

        let _refreshing = false;
        async function refreshData() {
            if (_refreshing) return;  // skip if previous cycle still running
            _refreshing = true;
            const icon = document.getElementById("refresh-icon");
            if(icon) icon.classList.add("fa-spin");

            // Fire ALL data requests in parallel (each has its own 8s timeout)
            // so one slow endpoint never freezes the whole cycle and the
            // dashboard always shows the freshest data available.
            const _results = await Promise.allSettled([
                fetchJSON("/api/status"),
                fetchJSON("/api/institutional-metrics"),
                fetchJSON("/api/sentiment"),
                fetchJSON("/api/monte-carlo"),
                fetchJSON("/api/matrix"),
                fetchJSON("/api/correlation"),
                fetchJSON("/api/positions"),
                fetchJSON("/api/ml-prob"),
            ]);
            const _ok = (p) => (p && p.status === "fulfilled") ? p.value : null;
            const status  = _ok(_results[0]);
            const inst    = _ok(_results[1]);
            const sent    = _ok(_results[2]);
            const mc      = _ok(_results[3]);
            const matrix  = _ok(_results[4]);
            const corr    = _ok(_results[5]);
            const posData = _ok(_results[6]);
            const mlp     = _ok(_results[7]);

            applyStatus(status);

            // Institutional metrics — real candles only.
            const instContainer = document.getElementById("institutional-metrics-container");
            if (inst && inst.available && inst.metrics) {
                institutionalReportText = inst.report_text || "";
                instContainer.innerHTML = Object.entries(inst.metrics).map(([key, value]) =>
                    `<div class="mb-2"><span class="text-cyan-300 font-mono">${key}</span>: ` +
                    `<span class="text-slate-200">${value.display ?? "—"}</span> ` +
                    `<span class="text-slate-400">${value.text ?? ""}</span></div>`
                ).join("") + `<div class="text-[11px] text-slate-500 mt-3">Source: ${inst.source} | As of: ${inst.as_of_utc}</div>`;
            } else {
                institutionalReportText = "";
                instContainer.innerHTML = '<span class="text-amber-400">Реальные institutional metrics недоступны; демонстрационные значения отключены.</span>';
            }

            // Sentiment — no sample headlines are presented as live.
            const biasEl = document.getElementById("sentiment-bias");
            if (sent && sent.available && sent.bias != null && sent.score != null) {
                biasEl.innerText = sent.bias.toUpperCase() + " (" + (sent.score > 0 ? "+" : "") + Number(sent.score).toFixed(2) + ")";
                biasEl.className = sent.bias === "bullish" ? "font-bold text-emerald-400 uppercase" : (sent.bias === "bearish" ? "font-bold text-rose-400 uppercase" : "font-bold text-slate-400 uppercase");
                document.getElementById("sentiment-conf").innerText = sent.confidence == null ? "—" : (Number(sent.confidence) * 100).toFixed(1) + "%";
                const terms = Array.isArray(sent.matched_terms) ? sent.matched_terms : [];
                document.getElementById("sentiment-tags").innerHTML = 'Ключевые факторы: <span class="text-slate-300 font-mono">' + (terms.join(", ") || "нейтральный фон") + '</span>';
            } else {
                biasEl.innerText = "—";
                biasEl.className = "font-bold text-slate-500 uppercase";
                document.getElementById("sentiment-conf").innerText = "—";
                var sentReason = (sent && sent.reason) || "news_feed_unavailable";
                var sentErr = (sent && sent.feed && sent.feed.error) ? " · " + sent.feed.error : "";
                document.getElementById("sentiment-tags").innerHTML = '<span class="text-slate-500">Данные недоступны: ' + sentReason + sentErr + '</span>';
            }

            // Monte Carlo — persisted executed trades only.
            if (mc && mc.available && mc.var_95_usd != null) {
                document.getElementById("mc-var95").innerText = "$" + Number(mc.var_95_usd).toFixed(2);
                document.getElementById("mc-prob").innerText = Number(mc.profit_probability_pct).toFixed(1) + "%";
                document.getElementById("mc-ruin").innerText = Number(mc.prob_of_ruin_pct).toFixed(1) + "%";
            } else {
                document.getElementById("mc-var95").innerText = "—";
                document.getElementById("mc-prob").innerText = "—";
                document.getElementById("mc-ruin").innerText = "—";
            }

            // Matrix — fall back to the last good payload if this fetch timed
            // out (slow cold start), so asset rows never disappear from the table.
            if (matrix && matrix.signals) lastMatrix = matrix;
            renderMatrix(matrix || lastMatrix);

            // XAUUSD raw ML probabilities — same last-good fallback.
            if (mlp && mlp.available) lastMLProb = mlp;
            renderMLProb(mlp || lastMLProb);

            // Correlation
            if (corr && corr.available && Array.isArray(corr.matrix) && corr.matrix.length) {
                const tbody = document.getElementById("corr-matrix-body");
                tbody.innerHTML = "";
                const assets = corr.assets;
                
                let headHtml = '<tr class="border-b border-slate-700"><th class="p-2 text-slate-400"></th>';
                for(const a of assets) headHtml += `<th class="p-2 text-slate-300 font-bold">${a}</th>`;
                headHtml += '</tr>';
                tbody.innerHTML += headHtml;

                for(let i=0; i<assets.length; i++) {
                    let rowHtml = `<tr class="border-b border-slate-800"><td class="p-2 font-bold text-slate-300 text-left">${assets[i]}</td>`;
                    for(let j=0; j<assets.length; j++) {
                        const val = corr.matrix[i][j];
                        let bg = "bg-slate-800/30 text-slate-400";
                        if (i === j) bg = "bg-slate-700/50 text-slate-200 font-bold";
                        else if (val >= 0.8) bg = "bg-emerald-900/60 text-emerald-300 font-bold";
                        else if (val <= -0.8) bg = "bg-rose-900/60 text-rose-300 font-bold";
                        else if (val > 0.4) bg = "bg-emerald-950/40 text-emerald-400";
                        else if (val < -0.4) bg = "bg-rose-950/40 text-rose-400";

                        rowHtml += `<td class="p-2 ${bg}">${val.toFixed(2)}</td>`;
                    }
                    rowHtml += '</tr>';
                    tbody.innerHTML += rowHtml;
                }
            } else {
                document.getElementById("corr-matrix-body").innerHTML = '<tr><td class="p-4 text-amber-400">Реальная корреляция недоступна</td></tr>';
            }

            // Positions
            renderPositions(posData);

            if(icon) setTimeout(() => icon.classList.remove("fa-spin"), 400);
            _refreshing = false;
            updateFreshness();
        }

        // NOTE: browser mutation controls are disabled server-side by design
        // (web-UI spec §11/§12). No control buttons are rendered and no
        // /api/control calls are issued from this page.

        // ====================================================================
        // ML PROBABILITY PANEL (per-asset)
        // ====================================================================
        let mlProbAsset = "XAUUSD";      // currently selected asset
        let lastMLProb = null;           // latest /api/ml-prob payload (raw ML P long/short)

        function selectedMLProbAsset() {
            const sel = document.getElementById("mlprob-asset");
            if (sel && sel.value) { mlProbAsset = sel.value; return sel.value; }
            return mlProbAsset;
        }

        function renderMLProb(p) {
            const verdict = document.getElementById("mlprob-verdict");
            if (!p || p.available === false) {
                if (verdict) {
                    verdict.innerText = "недоступно";
                    verdict.className = "px-3 py-1 rounded text-xs font-bold border border-slate-600 bg-slate-800/60 text-slate-400";
                }
                return;
            }
            const lat = p.latest || {};
            const thr = p.thresholds || {};
            const leanThr = (thr.min_ml_probability != null) ? thr.min_ml_probability : 0.55;
            const pct = (v) => (v == null) ? "—" : (v * 100).toFixed(1) + "%";

            const set = (id, v) => { const el = document.getElementById(id); if (el) el.innerText = v; };
            const bar = (id, v) => { const el = document.getElementById(id); if (el) el.style.width = (v == null ? 0 : v * 100) + "%"; };
            set("mlprob-plong", pct(lat.p_long));
            set("mlprob-pshort", pct(lat.p_short));
            bar("mlprob-plong-bar", lat.p_long);
            bar("mlprob-pshort-bar", lat.p_short);
            set("mlprob-thr-min", (thr.min_ml_probability != null) ? thr.min_ml_probability.toFixed(2) : "0.55");
            set("mlprob-thr-floor", (thr.ml_confidence_floor != null) ? thr.ml_confidence_floor.toFixed(2) : "0.62");
            set("mlprob-thr-alert", (thr.min_confidence_to_alert != null) ? thr.min_confidence_to_alert.toFixed(2) : "0.60");

            // Ensemble verdict (the gate the live trader actually applies).
            const eb = lat.ensemble_bias;
            const conf = (lat.ensemble_confidence != null) ? (lat.ensemble_confidence * 100).toFixed(0) + "%" : "";
            if (eb === "short") {
                verdict.innerText = "SHORT · " + conf;
                verdict.className = "px-3 py-1 rounded text-xs font-bold border border-rose-500/60 bg-rose-950/40 text-rose-300";
            } else if (eb === "long") {
                verdict.innerText = "LONG · " + conf;
                verdict.className = "px-3 py-1 rounded text-xs font-bold border border-emerald-500/60 bg-emerald-950/40 text-emerald-300";
            } else {
                verdict.innerText = "NO TRADE";
                verdict.className = "px-3 py-1 rounded text-xs font-bold border border-slate-600 bg-slate-800/60 text-slate-400";
            }

            // "Модель начинает сигналить шорт" — raw ML leaning, before the gate.
            const statusEl = document.getElementById("mlprob-status");
            if (statusEl) {
                if (lat.p_short != null && lat.p_short > leanThr) {
                    statusEl.innerHTML = '<span class="text-amber-300 font-bold">⚠ модель смещается в шорт</span> (P(short) &gt; ' + leanThr.toFixed(2) + ')';
                } else {
                    statusEl.innerText = "Нейтрально: модель не сигналит шорт";
                }
            }

            // Meta line: цена · режим · сессия · бар (бар в UTC-шкале данных
            // MT5 — без конвертации в TZ браузера, чтобы не выглядел "из будущего").
            const meta = document.getElementById("mlprob-meta");
            if (meta && lat.ts != null) {
                const t = new Date(lat.ts * 1000);
                const barUtc = t.toISOString().slice(0, 16).replace("T", " ");
                meta.innerText = "Цена " + (lat.price != null ? lat.price.toFixed(2) : "—")
                    + " · " + (lat.regime || "?") + " · " + (lat.session || "?") + " · бар " + barUtc + " UTC"
                    + " · обновлено " + (p.as_of_utc ? new Date(p.as_of_utc).toLocaleTimeString("ru-RU") : "—");
            }

            renderMLProbSpark((p.history || []).map(h => h.p_short), leanThr);
        }

        function renderMLProbSpark(pts, leanThr) {
            const svg = document.getElementById("mlprob-spark");
            if (!svg) return;
            const W = 300, H = 80;
            const vals = pts.filter(v => v != null);
            if (vals.length < 2) {
                svg.innerHTML = '<text x="10" y="44" fill="#64748b" font-size="11">нет истории (нужно &gt;1 закрытый бар)</text>';
                return;
            }
            const lo = Math.min(0.40, Math.min.apply(null, vals));
            const hi = Math.max(0.60, Math.max.apply(null, vals));
            const x = (i) => (vals.length === 1 ? W / 2 : (i / (vals.length - 1)) * W);
            const y = (v) => H - ((v - lo) / (hi - lo)) * H;
            const poly = vals.map((v, i) => x(i).toFixed(1) + "," + y(v).toFixed(1)).join(" ");
            const yMid = y(0.5), yThr = y(Math.min(Math.max(leanThr, lo), hi));
            const lastX = x(vals.length - 1), lastY = y(vals[vals.length - 1]);
            svg.innerHTML =
                '<line x1="0" y1="' + yMid.toFixed(1) + '" x2="' + W + '" y2="' + yMid.toFixed(1) + '" stroke="#475569" stroke-width="1" stroke-dasharray="3 3"/>' +
                '<line x1="0" y1="' + yThr.toFixed(1) + '" x2="' + W + '" y2="' + yThr.toFixed(1) + '" stroke="#f59e0b" stroke-width="1" stroke-dasharray="2 4" opacity="0.7"/>' +
                '<polyline points="' + poly + '" fill="none" stroke="#f43f5e" stroke-width="1.8"/>' +
                '<circle cx="' + lastX.toFixed(1) + '" cy="' + lastY.toFixed(1) + '" r="2.6" fill="#f43f5e"/>';
        }

        // ====================================================================
        // PAIRS MODEL
        // ====================================================================
        let pairsData = null;
        let lastMatrix = null;   // latest /api/matrix payload (ML assets)
        let pairsZChart = null;
        let pairsRefreshTimer = null;

        function setPairsRefreshInterval() {
            if (pairsRefreshTimer) { clearInterval(pairsRefreshTimer); pairsRefreshTimer = null; }
            const sec = Number((document.getElementById('pairs-refresh-interval') || {value:30}).value);
            if (sec > 0) pairsRefreshTimer = setInterval(() => { loadPairsData(); loadPairsEquity(); }, sec * 1000);
        }

        async function loadPairsData() {
            const icon = document.getElementById('pairs-refresh-icon');
            if (icon) icon.classList.add('fa-spin');
            const tf = (document.getElementById('pairs-tf-select') || {value:'H1'}).value;
            try {
                const res = await fetch('/api/pairs?timeframe=' + tf);
                const data = await res.json();
                if (!data.available || !data.pairs || data.pairs.length === 0) {
                    document.getElementById('pairs-disclosure').innerHTML =
                        '<span class="text-amber-400">pairs_analysis unavailable: ' + (data.reason || 'no pairs configured') + '</span>';
                    if (icon) icon.classList.remove('fa-spin');
                    return;
                }
                pairsData = data;
                // Pull the latest pairs data into the main signal matrix.
                renderMatrix(lastMatrix);
                // Populate pair selector
                const sel = document.getElementById('pairs-pair-select');
                const prev = sel.value;
                sel.innerHTML = '';
                data.pairs.forEach((p, i) => {
                    const opt = document.createElement('option');
                    opt.value = i; opt.text = p.name;
                    sel.appendChild(opt);
                });
                if (prev !== '' && Number(prev) < data.pairs.length) sel.value = prev;
                loadPairsPair();
                document.getElementById('pairs-disclosure').innerHTML =
                    '<span class="text-slate-500">' + data.pairs.length + ' pairs • ' + tf + ' • ' +
                    'as of ' + new Date(data.as_of_utc).toLocaleTimeString() + '</span>';
            } catch (e) {
                document.getElementById('pairs-disclosure').innerHTML =
                    '<span class="text-rose-400">Failed to load pairs: ' + e.message + '</span>';
            }
            if (icon) setTimeout(() => icon.classList.remove('fa-spin'), 400);
        }

        function loadPairsPair() {
            if (!pairsData) return;
            const idx = Number(document.getElementById('pairs-pair-select').value || 0);
            const p = pairsData.pairs[idx];
            if (!p) return;

            // Parameters
            document.getElementById('p-beta').textContent = p.beta;
            document.getElementById('p-z').textContent = p.z;
            document.getElementById('p-hl').textContent = p.half_life_days !== null ? p.half_life_days + ' d' : '∞';
            document.getElementById('p-mu').textContent = p.mu;
            document.getElementById('p-theta').textContent = p.theta;
            document.getElementById('p-sigma').textContent = p.sigma;
            const adfEl = document.getElementById('p-adf');
            adfEl.textContent = p.adf_p;
            adfEl.className = p.adf_p < 0.05 ? 'font-bold text-emerald-400' : 'font-bold text-rose-400';
            document.getElementById('p-sigma-ann').textContent = p.sigma_annual;
            document.getElementById('p-ratio').textContent = p.ratio;
            document.getElementById('p-p1p2').textContent = p.p1_last + ' / ' + p.p2_last;
            document.getElementById('p-formula').textContent = p.formula;

            // Signal
            const sigBar = document.getElementById('p-signal-bar');
            sigBar.textContent = p.signal_reason;
            sigBar.className = 'text-center py-2 px-3 rounded-lg text-sm font-bold ' +
                (p.signal_direction === 'long' ? 'bg-emerald-900/40 text-emerald-400 border border-emerald-700/50' :
                 p.signal_direction === 'short' ? 'bg-rose-900/40 text-rose-400 border border-rose-700/50' :
                 'bg-slate-800/60 text-slate-400 border border-slate-700');

            // Math Board
            document.getElementById('p-hurst').textContent = p.hurst;
            document.getElementById('p-acf1').textContent = p.acf1;
            document.getElementById('p-skew').textContent = p.skew;
            document.getElementById('p-exkurt').textContent = p.ex_kurtosis;
            document.getElementById('p-rvol').textContent = p.realized_vol_pct + '%';
            const hb = document.getElementById('p-hurst-badge');
            hb.textContent = p.hurst < 0.5 ? 'H=' + p.hurst + ' < 0.5 → mean-reverting' :
                             p.hurst > 0.5 ? 'H=' + p.hurst + ' ≥ 0.5 → trending' :
                             'H=' + p.hurst + ' ≈ 0.5 → random walk';
            hb.className = 'mt-2 text-[11px] px-2 py-1 rounded inline-block ' +
                (p.hurst < 0.5 ? 'bg-emerald-900/30 text-emerald-400' :
                 p.hurst > 0.5 ? 'bg-rose-900/30 text-rose-400' : 'bg-slate-800/60 text-slate-400');

            // Ensemble table
            const tbody = document.getElementById('p-ensemble-body');
            tbody.innerHTML = '';
            if (p.ensemble_engines) {
                for (const e of p.ensemble_engines) {
                    const dClass = e.direction === 'long' ? 'text-emerald-400' :
                                   e.direction === 'short' ? 'text-rose-400' : 'text-slate-400';
                    const entries = Object.entries(e).filter(([k]) => !['name','direction','confidence'].includes(k));
                    const key = entries[0];
                    const keyStr = key ? key[0] + '=' + (typeof key[1] === 'number' ? key[1].toFixed(3) : key[1]) : '';
                    const confW = Math.round(e.confidence * 0.6);
                    const confColor = e.confidence > 60 ? '#3fb950' : e.confidence > 40 ? '#d29922' : '#8b949e';
                    tbody.innerHTML += '<tr class="border-b border-slate-800/50">' +
                        '<td class="py-1.5">' + e.name + '</td>' +
                        '<td class="py-1.5 ' + dClass + ' font-bold">' + e.direction.toUpperCase() + '</td>' +
                        '<td class="py-1.5"><span style="display:inline-block;height:5px;border-radius:3px;min-width:2px;width:' + confW + 'px;background:' + confColor + '"></span> ' + e.confidence.toFixed(1) + '%</td>' +
                        '<td class="py-1.5 text-slate-500 text-[11px]">' + keyStr + '</td>' +
                        '</tr>';
                }
            }
            const ensSummary = document.getElementById('p-ensemble-summary');
            ensSummary.textContent = p.ensemble_line + ' (confidence ' + p.ensemble_confidence + '%)';
            ensSummary.className = 'mt-3 text-center text-sm font-bold ' +
                (p.ensemble_direction === 'long' ? 'text-emerald-400' :
                 p.ensemble_direction === 'short' ? 'text-rose-400' : 'text-slate-400');

            // Z-Score chart
            renderPairsChart(p);
        }

        function renderPairsChart(pair) {
            if (pairsZChart) pairsZChart.destroy();
            const ctx = document.getElementById('pairs-z-chart');
            if (!ctx || !pair.z_values || !pair.z_dates) return;
            const zVals = pair.z_values;
            const zLabels = pair.z_dates;
            const entryZ = 2.0, stopZ = 3.0;
            pairsZChart = new Chart(ctx.getContext('2d'), {
                type: 'line',
                data: {
                    labels: zLabels,
                    datasets: [
                        { label: 'z-score', data: zVals, borderColor: '#f8fafc', borderWidth: 1.5, pointRadius: 0, fill: false, tension: 0.1 },
                        { label: '+2σ', data: Array(zVals.length).fill(entryZ), borderColor: '#f85149', borderWidth: 1, borderDash: [4,4], pointRadius: 0, fill: false },
                        { label: '-2σ', data: Array(zVals.length).fill(-entryZ), borderColor: '#3fb950', borderWidth: 1, borderDash: [4,4], pointRadius: 0, fill: false },
                        { label: '+3σ', data: Array(zVals.length).fill(stopZ), borderColor: '#f8514966', borderWidth: 1, borderDash: [2,4], pointRadius: 0, fill: false },
                        { label: '-3σ', data: Array(zVals.length).fill(-stopZ), borderColor: '#3fb95066', borderWidth: 1, borderDash: [2,4], pointRadius: 0, fill: false },
                        { label: '0', data: Array(zVals.length).fill(0), borderColor: '#30363d', borderWidth: 1, pointRadius: 0, fill: false },
                    ]
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
                    scales: {
                        x: { display: true, ticks: { color: '#8b949e', maxTicksLimit: 8, font: { size: 10 } }, grid: { color: '#30363d33' } },
                        y: { ticks: { color: '#8b949e', font: { size: 10 } }, grid: { color: '#30363d33' } }
                    }
                }
            });
        }

        // ---- Pair Equity Curve ----
        let equityChart = null;

        async function loadPairsEquity() {
            try {
                const res = await fetch('/api/pairs/equity');
                const data = await res.json();
                if (!data.available || !data.equity || data.equity.length === 0) {
                    document.getElementById('p-equity-count').textContent = '0 trades';
                    return;
                }
                const eq = data.equity;
                const stats = data.stats || {};
                document.getElementById('p-equity-count').textContent = eq.length + ' trades';

                // Stats
                const money = v => v != null ? (v >= 0 ? '+' : '') + v.toFixed(1) + 'R' : '—';
                document.getElementById('p-st-n').textContent = stats.total_trades ?? eq.length;
                document.getElementById('p-st-wr').textContent = stats.win_rate_pct != null ? stats.win_rate_pct.toFixed(1) + '%' : '—';
                document.getElementById('p-st-wr').className = 'font-bold ' + ((stats.win_rate_pct || 0) >= 50 ? 'text-emerald-400' : 'text-rose-400');
                document.getElementById('p-st-avr').textContent = money(stats.avg_r);
                const sumEl = document.getElementById('p-st-sum');
                sumEl.textContent = money(stats.sum_r);
                sumEl.className = 'font-bold ' + ((stats.sum_r || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400');
                document.getElementById('p-st-best').textContent = money(stats.max_r);
                document.getElementById('p-st-worst').textContent = money(stats.min_r);
                const pf = stats.wins > 0 && stats.losses > 0 ? (stats.sum_r / Math.abs(stats.min_r * stats.losses)).toFixed(2) : (stats.losses === 0 ? '∞' : '—');
                document.getElementById('p-st-pf').textContent = pf;

                // Chart
                if (equityChart) equityChart.destroy();
                const ctx = document.getElementById('p-equity-chart');
                if (!ctx) return;
                const labels = eq.map(e => e.date);
                const cumR = eq.map(e => e.cum_r);
                const colors = eq.map(e => e.r >= 0 ? '#3fb950' : '#f85149');
                equityChart = new Chart(ctx.getContext('2d'), {
                    type: 'line',
                    data: {
                        labels: labels,
                        datasets: [
                            { label: 'Cumulative R', data: cumR, borderColor: '#a78bfa', borderWidth: 1.5, pointRadius: 2, pointBackgroundColor: colors, pointBorderColor: colors, fill: { target: 'origin', above: 'rgba(63,185,80,0.08)', below: 'rgba(248,81,73,0.08)' }, tension: 0.2 },
                            { label: '0', data: Array(cumR.length).fill(0), borderColor: '#30363d', borderWidth: 1, borderDash: [3,3], pointRadius: 0, fill: false },
                        ]
                    },
                    options: {
                        responsive: true, maintainAspectRatio: false,
                        plugins: { legend: { display: false }, tooltip: { callbacks: { title: (items) => {
                            const i = items[0].dataIndex;
                            const e = eq[i];
                            return e.date + ' #' + e.num + ' ' + e.pair + ' ' + e.direction.toUpperCase();
                        }, label: (item) => {
                            const i = item.dataIndex;
                            const e = eq[i];
                            return 'R' + (e.r >= 0 ? '+' : '') + e.r + ' → cum ' + e.cum_r + ' (' + e.exit_reason + ')';
                        }}, mode: 'index', intersect: false } },
                        scales: {
                            x: { display: true, ticks: { color: '#8b949e', maxTicksLimit: 10, font: { size: 9 } }, grid: { color: '#30363d33' } },
                            y: { ticks: { color: '#8b949e', font: { size: 10 }, callback: v => v + 'R' }, grid: { color: '#30363d33' } }
                        }
                    }
                });
            } catch (e) {
                console.error('Pairs equity load failed:', e);
            }
        }

        // Load equity on pair switch
        const _origLoadPairsPair = loadPairsPair;
        loadPairsPair = function() { _origLoadPairsPair(); loadPairsEquity(); };

        async function loadMetrics() {
            const period = (document.getElementById("metrics-period") || {value:"week"}).value;
            const m = await fetchJSON("/api/metrics?period=" + period);
            const availEl = document.getElementById("metrics-available");
            if (!m) return;
            if (m.available === false) {
                availEl.classList.remove("hidden");
                return;
            }
            availEl.classList.add("hidden");
            const set = (id, val, cls) => { const el = document.getElementById(id); if(el){ el.innerText = val; if(cls) el.className = cls; } };
            const money = (v) => "$" + Number(v||0).toLocaleString('en-US', {minimumFractionDigits:2});
            set("m-n", m.n ?? "—");
            set("m-wr", (m.win_rate_pct!=null ? m.win_rate_pct.toFixed(1) + "%" : "—"), "text-xl font-bold text-emerald-400 mt-1");
            set("m-pf", (m.profit_factor==null ? "—" : (m.profit_factor===Infinity ? "∞" : Number(m.profit_factor).toFixed(2))), "text-xl font-bold text-amber-300 mt-1");
            const pnlColor = m.total_pnl>=0 ? "text-emerald-400" : "text-rose-400";
            set("m-pnl", money(m.total_pnl), "text-xl font-bold " + pnlColor + " mt-1");
            set("m-awin", money(m.avg_win), "text-lg font-bold text-emerald-400 mt-1");
            set("m-aloss", money(m.avg_loss), "text-lg font-bold text-rose-400 mt-1");
            set("m-dd", money(m.max_drawdown), "text-lg font-bold text-rose-300 mt-1");
            set("m-consec", m.max_consec_losses ?? "—");
            set("m-exp", money(m.expectancy));
            set("m-best", money(m.best_trade));
            set("m-worst", money(m.worst_trade));
            set("m-period-label", m.period_label ? ("Период: " + m.period_label) : "");
        }

        // ---- Clickable KPI cards -> smooth-scroll to the detail section ----
        function scrollToSection(id) {
            const el = document.getElementById(id);
            if (!el) return;
            document.querySelectorAll(".flash-target").forEach(e => e.classList.remove("flash-target"));
            el.scrollIntoView({ behavior: "smooth", block: "start" });
            el.classList.add("flash-target");
            setTimeout(() => el.classList.remove("flash-target"), 1400);
        }

        // ---- Theme toggle (persisted in localStorage) ----------------------
        function toggleTheme() {
            const root = document.documentElement;
            const light = root.classList.toggle("theme-light");
            try { localStorage.setItem("dashboard-theme", light ? "light" : "dark"); } catch(e) {}
            updateThemeIcon();
        }
        function updateThemeIcon() {
            const light = document.documentElement.classList.contains("theme-light");
            const icon = document.getElementById("theme-icon");
            if (icon) icon.className = "fas " + (light ? "fa-sun" : "fa-moon");
        }

        // Freshness heartbeat: "обновлено HH:MM:SS · Nс назад", green < 15s,
        // amber < 60s, red if the data went stale.
        let lastFreshTs = Date.now();
        function updateFreshness() {
            lastFreshTs = Date.now();
            const el = document.getElementById("last-updated");
            if (el) el.innerText = "Обновлено " + new Date().toLocaleTimeString("ru-RU");
        }
        setInterval(() => {
            const el = document.getElementById("last-updated");
            if (!el) return;
            const age = Math.round((Date.now() - lastFreshTs) / 1000);
            el.innerText = "Обновлено " + new Date().toLocaleTimeString("ru-RU") + " · " + age + "с назад";
            el.className = "text-xs font-mono " + (age < 15 ? "text-emerald-400" : age < 60 ? "text-amber-400" : "text-rose-400");
        }, 1000);

        // Initialize chart and polling
        updateThemeIcon();
        loadChart("XAUUSD");
        refreshData();
        loadMetrics();
        loadPairsData().then(() => loadPairsEquity());
        loadWSHistory();
        // Live balance/positions come over the WebSocket push stream (instant,
        // no polling). The heavy analytics sections refresh every 30s while
        // the tab is visible; a 10s REST fallback covers the case where the
        // WS stream is unreachable.
        connectDashboardWS();
        setInterval(() => { if (!document.hidden) refreshData(); }, 30000);
        setInterval(() => {
            if (!document.hidden && (!ws || ws.readyState !== WebSocket.OPEN)) refreshStatus();
        }, 10000);
        setPairsRefreshInterval();
        // Live candlestick chart: auto-reload every 30s while visible.
        setInterval(() => { if (!document.hidden) loadChart(currentChartAsset); }, 30000);
        document.addEventListener("visibilitychange", () => {
            if (!document.hidden) {
                refreshData();
                loadChart(currentChartAsset);
                loadWSHistory();
                loadPairsData();
                loadPairsEquity();
            }
        });
    </script>
</body>
</html>
"""
