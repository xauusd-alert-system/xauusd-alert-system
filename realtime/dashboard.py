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
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        body { background-color: #0b1120; color: #f8fafc; font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; }
        .glass-card { background: rgba(17, 24, 39, 0.75); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 12px; }
        .pulse-live { animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
    </style>
</head>
<body class="p-4 md:p-8">
    <div class="max-w-7xl mx-auto space-y-6">
        <!-- Header -->
        <header class="flex flex-col md:flex-row justify-between items-start md:items-center glass-card p-6 gap-4 border-l-4 border-l-amber-500">
            <div>
                <div class="flex items-center gap-3">
                    <span class="w-3 h-3 rounded-full bg-emerald-500 pulse-live"></span>
                    <h1 class="text-2xl md:text-3xl font-bold bg-gradient-to-r from-amber-400 via-yellow-200 to-amber-500 bg-clip-text text-transparent">
                        xauusd-alert-system
                    </h1>
                    <span class="text-xs bg-slate-800 border border-slate-700 text-slate-300 px-2.5 py-1 rounded-full font-mono">v2.1 QUANT PRO</span>
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

        <!-- KPI Metrics Grid -->
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <div class="glass-card p-5">
                <div class="flex justify-between items-center text-slate-400 text-xs font-semibold uppercase tracking-wider">
                    <span>Режим системы</span>
                    <i class="fas fa-server text-indigo-400 text-base"></i>
                </div>
                <div id="kpi-data-mode" class="text-2xl font-bold mt-2 text-indigo-300">UNKNOWN</div>
                <div class="text-xs text-slate-400 mt-1">Фактический источник указан ниже</div>
            </div>

            <div class="glass-card p-5">
                <div class="flex justify-between items-center text-slate-400 text-xs font-semibold uppercase tracking-wider">
                    <span>Баланс / Эквити</span>
                    <i class="fas fa-wallet text-emerald-400 text-base"></i>
                </div>
                <div id="kpi-balance" class="text-2xl font-bold mt-2 text-emerald-400">—</div>
                <div id="kpi-equity" class="text-xs text-slate-400 mt-1">Эквити: —</div>
            </div>

            <div class="glass-card p-5">
                <div class="flex justify-between items-center text-slate-400 text-xs font-semibold uppercase tracking-wider">
                    <span>Открытые позиции</span>
                    <i class="fas fa-layer-group text-amber-400 text-base"></i>
                </div>
                <div id="kpi-positions" class="text-2xl font-bold mt-2 text-amber-300">0</div>
                <div class="text-xs text-slate-400 mt-1">Лимит: макс 3 позиции</div>
            </div>

            <div class="glass-card p-5">
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
            <div class="flex justify-between items-center mb-4">
                <div class="flex items-center gap-2.5">
                    <i class="fas fa-microchip text-cyan-400 text-lg"></i>
                    <h2 class="text-lg font-bold text-slate-100">Метрики по софту на текущий момент (Smart Money Concepts)</h2>
                </div>
                <button onclick="copyMetricsText()" class="text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 px-3 py-1.5 rounded-lg transition flex items-center gap-1.5">
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
                <div class="flex justify-between items-center mb-3">
                    <div class="flex items-center gap-2">
                        <i class="fas fa-chart-candlestick text-amber-400"></i>
                        <h2 class="text-lg font-bold">Живой график M5 & Уровни входа</h2>
                    </div>
                    <div class="flex gap-1.5">
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
                <div>
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
            <div class="flex justify-between items-center mb-4">
                <h2 class="text-lg font-bold flex items-center gap-2">
                    <i class="fas fa-bolt text-amber-400"></i> Мульти-активная матрица сигналов (M5)
                </h2>
                <button onclick="refreshData()" class="text-xs text-slate-400 hover:text-slate-200 transition flex items-center gap-1">
                    <i class="fas fa-sync-alt" id="refresh-icon"></i> Обновить
                </button>
            </div>
            
            <div class="overflow-x-auto">
                <table class="w-full text-left text-sm text-slate-300">
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
            <div class="flex justify-between items-center mb-4">
                <div class="flex items-center gap-2.5">
                    <i class="fas fa-code-branch text-violet-400 text-lg"></i>
                    <h2 class="text-lg font-bold text-slate-100">PAIRS MODEL — Statistical Pair Analytics</h2>
                </div>
                <div class="flex items-center gap-2">
                    <select id="pairs-pair-select" onchange="loadPairsPair()" class="bg-slate-800 text-slate-200 text-xs border border-slate-700 rounded-lg px-2 py-1.5 font-mono">
                        <option value="">Загрузка...</option>
                    </select>
                    <select id="pairs-tf-select" onchange="loadPairsData()" class="bg-slate-800 text-slate-200 text-xs border border-slate-700 rounded-lg px-2 py-1.5">
                        <option value="H1" selected>H1</option>
                        <option value="D1">D1</option>
                    </select>
                    <select id="pairs-refresh-interval" onchange="setPairsRefreshInterval()" class="bg-slate-800 text-slate-200 text-xs border border-slate-700 rounded-lg px-2 py-1.5">
                        <option value="0">OFF</option>
                        <option value="30">30s</option>
                        <option value="60" selected>1m</option>
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
                    <table class="w-full text-xs font-mono">
                        <thead><tr class="text-slate-400 border-b border-slate-700">
                            <th class="text-left py-1.5">Engine</th>
                            <th class="text-left py-1.5">Dir</th>
                            <th class="text-left py-1.5">Conf</th>
                            <th class="text-left py-1.5">Key</th>
                        </tr></thead>
                        <tbody id="p-ensemble-body"></tbody>
                    </table>
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
            <div class="flex justify-between items-center mb-4">
                <h2 class="text-lg font-bold flex items-center gap-2">
                    <i class="fas fa-chart-line text-emerald-400"></i> Реальная статистика закрытых сделок
                </h2>
                <div class="flex items-center gap-2">
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
            try {
                const res = await fetch(url);
                return await res.json();
            } catch(e) {
                console.error("Fetch error for " + url, e);
                return null;
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
        }

        async function refreshData() {
            const icon = document.getElementById("refresh-icon");
            if(icon) icon.classList.add("fa-spin");

            // Status
            const status = await fetchJSON("/api/status");
            if (status) {
                document.getElementById("kpi-data-mode").innerText = String(status.data_mode || "unknown").toUpperCase();
                document.getElementById("kpi-balance").innerText = status.balance == null ? "—" : "$" + status.balance.toLocaleString('en-US', {minimumFractionDigits: 2});
                document.getElementById("kpi-equity").innerText = status.equity == null ? "Эквити: —" : "Эквити: $" + status.equity.toLocaleString('en-US', {minimumFractionDigits: 2});
                document.getElementById("data-disclosure").innerText = "Source: " + (status.source || "unknown") + " | Data: " + (status.mode || "unknown") + " | Deployment: " + (status.deployment_mode || "unknown") + " | Strategy: " + (status.strategy_version || "unknown") + " | Config: " + String(status.config_hash || "").slice(0,12) + " | As of: " + (status.as_of_utc || "—");
                document.getElementById("kpi-positions").innerText = status.open_positions_count;
                document.getElementById("kpi-risk").innerText = status.circuit_breaker ? "HALTED" : "NORMAL";
                document.getElementById("kpi-risk").className = status.circuit_breaker ? "text-2xl font-bold mt-2 text-rose-500" : "text-2xl font-bold mt-2 text-emerald-400";
            }

            // Institutional metrics — real candles only.
            const inst = await fetchJSON("/api/institutional-metrics");
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
            const sent = await fetchJSON("/api/sentiment");
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
            const mc = await fetchJSON("/api/monte-carlo");
            if (mc && mc.available && mc.var_95_usd != null) {
                document.getElementById("mc-var95").innerText = "$" + Number(mc.var_95_usd).toFixed(2);
                document.getElementById("mc-prob").innerText = Number(mc.profit_probability_pct).toFixed(1) + "%";
                document.getElementById("mc-ruin").innerText = Number(mc.prob_of_ruin_pct).toFixed(1) + "%";
            } else {
                document.getElementById("mc-var95").innerText = "—";
                document.getElementById("mc-prob").innerText = "—";
                document.getElementById("mc-ruin").innerText = "—";
            }

            // Matrix
            const matrix = await fetchJSON("/api/matrix");
            if (matrix && matrix.signals) {
                const tbody = document.getElementById("signal-matrix-body");
                tbody.innerHTML = "";
                for (const item of matrix.signals) {
                    let biasBadge = item.available
                        ? '<span class="px-2 py-0.5 rounded bg-slate-700 text-slate-300">NEUTRAL</span>'
                        : '<span class="px-2 py-0.5 rounded bg-amber-900/60 text-amber-300">UNAVAILABLE</span>';
                    if (item.available && item.bias === "long") biasBadge = '<span class="px-2 py-0.5 rounded bg-emerald-900/60 text-emerald-300 font-bold border border-emerald-700/50">LONG / BUY</span>';
                    if (item.available && item.bias === "short") biasBadge = '<span class="px-2 py-0.5 rounded bg-rose-900/60 text-rose-300 font-bold border border-rose-700/50">SHORT / SELL</span>';

                    const confidence = item.available ? (Number(item.confidence) * 100).toFixed(1) + "%" : "—";
                    const targets = item.available && item.targets ? item.targets.map(t => t.toFixed(2)).join(" / ") : "-";
                    const sl = item.available && item.invalidation ? item.invalidation.toFixed(2) : "-";

                    tbody.innerHTML += `
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

            // Correlation
            const corr = await fetchJSON("/api/correlation");
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
            const posData = await fetchJSON("/api/positions");
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

            if(icon) setTimeout(() => icon.classList.remove("fa-spin"), 400);
        }

        // NOTE: browser mutation controls are disabled server-side by design
        // (web-UI spec §11/§12). No control buttons are rendered and no
        // /api/control calls are issued from this page.

        // ====================================================================
        // PAIRS MODEL
        // ====================================================================
        let pairsData = null;
        let pairsZChart = null;
        let pairsRefreshTimer = null;

        function setPairsRefreshInterval() {
            if (pairsRefreshTimer) { clearInterval(pairsRefreshTimer); pairsRefreshTimer = null; }
            const sec = Number((document.getElementById('pairs-refresh-interval') || {value:60}).value);
            if (sec > 0) pairsRefreshTimer = setInterval(loadPairsData, sec * 1000);
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

        // Initialize chart and polling
        loadChart("XAUUSD");
        refreshData();
        loadMetrics();
        loadPairsData().then(() => loadPairsEquity());
        setInterval(refreshData, 5000);
        setPairsRefreshInterval();
    </script>
</body>
</html>
"""
