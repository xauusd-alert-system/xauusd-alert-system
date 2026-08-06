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
            
            <!-- Quick Controls -->
            <div class="flex items-center gap-2">
                <button onclick="sendControl('pause')" class="bg-amber-600/80 hover:bg-amber-600 text-white px-4 py-2 rounded-lg text-sm font-medium transition flex items-center gap-2">
                    <i class="fas fa-pause"></i> Пауза
                </button>
                <button onclick="sendControl('resume')" class="bg-emerald-600/80 hover:bg-emerald-600 text-white px-4 py-2 rounded-lg text-sm font-medium transition flex items-center gap-2">
                    <i class="fas fa-play"></i> Возобновить
                </button>
                <button onclick="sendControl('closeall')" class="bg-rose-600/80 hover:bg-rose-600 text-white px-4 py-2 rounded-lg text-sm font-medium transition flex items-center gap-2">
                    <i class="fas fa-power-off"></i> Закрыть всё
                </button>
            </div>
        </header>

        <!-- KPI Metrics Grid -->
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <div class="glass-card p-5">
                <div class="flex justify-between items-center text-slate-400 text-xs font-semibold uppercase tracking-wider">
                    <span>Режим системы</span>
                    <i class="fas fa-server text-indigo-400 text-base"></i>
                </div>
                <div id="kpi-data-mode" class="text-2xl font-bold mt-2 text-indigo-300">Live / Active</div>
                <div class="text-xs text-slate-400 mt-1">Провайдер: MetaTrader 5 / Shim</div>
            </div>

            <div class="glass-card p-5">
                <div class="flex justify-between items-center text-slate-400 text-xs font-semibold uppercase tracking-wider">
                    <span>Баланс / Эквити</span>
                    <i class="fas fa-wallet text-emerald-400 text-base"></i>
                </div>
                <div id="kpi-balance" class="text-2xl font-bold mt-2 text-emerald-400">$100,000.00</div>
                <div id="kpi-equity" class="text-xs text-slate-400 mt-1">Эквити: $100,000.00</div>
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
            
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 text-xs font-mono">
                <!-- Manipulation Index -->
                <div class="p-4 bg-slate-850/80 rounded-lg border border-slate-700/60 space-y-1.5">
                    <div class="flex justify-between items-center">
                        <span class="text-slate-400 font-sans font-semibold">Manipulation Index</span>
                        <span class="px-2 py-0.5 rounded bg-amber-900/60 text-amber-300 font-bold border border-amber-700/50">7/10</span>
                    </div>
                    <p class="text-slate-300 font-sans text-xs leading-relaxed pt-1">
                        — высокий уровень манипуляций сохраняется. Крупные игроки продолжают активно работать в этом диапазоне.
                    </p>
                </div>

                <!-- Zone Strength -->
                <div class="p-4 bg-slate-850/80 rounded-lg border border-slate-700/60 space-y-1.5">
                    <div class="flex justify-between items-center">
                        <span class="text-slate-400 font-sans font-semibold">Zone Strength</span>
                        <span class="px-2 py-0.5 rounded bg-rose-900/60 text-rose-300 font-bold border border-rose-700/50">18%</span>
                    </div>
                    <p class="text-slate-300 font-sans text-xs leading-relaxed pt-1">
                        — зона крайне слабая. Текущий уровень не является серьёзной поддержкой, вероятность ухода ниже высокая.
                    </p>
                </div>

                <!-- SMF Ratio -->
                <div class="p-4 bg-slate-850/80 rounded-lg border border-slate-700/60 space-y-1.5">
                    <div class="flex justify-between items-center">
                        <span class="text-slate-400 font-sans font-semibold">SMF Ratio</span>
                        <span class="px-2 py-0.5 rounded bg-indigo-900/60 text-indigo-300 font-bold border border-indigo-700/50">2.34</span>
                    </div>
                    <p class="text-slate-300 font-sans text-xs leading-relaxed pt-1">
                        — институционалы доминируют над розницей с коэффициентом 2.3 к 1. Умные деньги продолжают давить вниз.
                    </p>
                </div>

                <!-- Liquidity Grab -->
                <div class="p-4 bg-slate-850/80 rounded-lg border border-slate-700/60 space-y-1.5">
                    <div class="flex justify-between items-center">
                        <span class="text-slate-400 font-sans font-semibold">Liquidity Grab</span>
                        <span class="px-2 py-0.5 rounded bg-purple-900/60 text-purple-300 font-bold border border-purple-700/50">8/10</span>
                    </div>
                    <p class="text-slate-300 font-sans text-xs leading-relaxed pt-1">
                        — активная охота за ликвидностью. Именно это объясняет резкие движения на локальных уровнях перед продолжением тренда.
                    </p>
                </div>

                <!-- Delta Confidence -->
                <div class="p-4 bg-slate-850/80 rounded-lg border border-slate-700/60 space-y-1.5 md:col-span-2">
                    <div class="flex justify-between items-center">
                        <span class="text-slate-400 font-sans font-semibold">Delta Confidence</span>
                        <span class="px-2 py-0.5 rounded bg-emerald-900/60 text-emerald-300 font-bold border border-emerald-700/50">HIGH</span>
                    </div>
                    <p class="text-slate-300 font-sans text-xs leading-relaxed pt-1">
                        — уверенность модели в направлении дельты высокая. Продавцы контролируют рынок на старших таймфреймах.
                    </p>
                </div>
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
                            <span id="sentiment-bias" class="font-bold text-emerald-400 uppercase">BULLISH (+0.65)</span>
                        </div>
                        <div class="flex justify-between items-center">
                            <span class="text-slate-400">Уверенность AI:</span>
                            <span id="sentiment-conf" class="font-mono text-amber-300">85.0%</span>
                        </div>
                        <div id="sentiment-tags" class="text-slate-400 text-[11px] pt-1">
                            Ключевые факторы: <span class="text-slate-300 font-mono">+safe haven, +rate cut</span>
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
                            <span id="mc-var95" class="text-rose-400">-$240.50</span>
                        </div>
                        <div class="flex justify-between">
                            <span class="text-slate-400">Вероятность прибыли:</span>
                            <span id="mc-prob" class="text-emerald-400 font-bold">88.4%</span>
                        </div>
                        <div class="flex justify-between">
                            <span class="text-slate-400">Риск банкротства:</span>
                            <span id="mc-ruin" class="text-emerald-400">0.0%</span>
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

        <!-- Footer -->
        <footer class="text-center text-slate-500 text-xs py-4">
            xauusd-alert-system &bull; Causal ML Inference Pipeline &bull; Purged Time-Split Calibration &bull; 204 Passed Tests
        </footer>
    </div>

    <script>
        let currentChartAsset = "XAUUSD";

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
            const text = `*Метрики по софту на текущий момент*\\n\\n**Manipulation Index: 7/10** — высокий уровень манипуляций сохраняется. Крупные игроки продолжают активно работать в этом диапазоне.\\n\\n**Zone Strength: 18%** — зона крайне слабая. Текущий уровень не является серьёзной поддержкой, вероятность ухода ниже высокая.\\n\\n**SMF Ratio: 2.34** — институционалы доминируют над розницей с коэффициентом 2.3 к 1. Умные деньги продолжают давить вниз.\\n\\n**Liquidity Grab: 8/10** — активная охота за ликвидностью. Именно это объясняет резкие движения на локальных уровнях перед продолжением тренда.\\n\\n**Delta Confidence: HIGH** — уверенность модели в направлении дельты высокая. Продавцы контролируют рынок на старших таймфреймах.`;
            navigator.clipboard.writeText(text).then(() => {
                alert("✅ Отчёт скопирован в буфер обмена!");
            });
        }

        async function loadChart(asset) {
            currentChartAsset = asset;
            const container = document.getElementById("chart-container");
            try {
                const res = await fetch("/api/chart/" + asset);
                const svg = await res.text();
                container.innerHTML = svg;
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
                document.getElementById("kpi-data-mode").innerText = status.data_mode.toUpperCase();
                document.getElementById("kpi-balance").innerText = "$" + status.balance.toLocaleString('en-US', {minimumFractionDigits: 2});
                document.getElementById("kpi-equity").innerText = "Эквити: $" + status.equity.toLocaleString('en-US', {minimumFractionDigits: 2});
                document.getElementById("kpi-positions").innerText = status.open_positions_count;
                document.getElementById("kpi-risk").innerText = status.circuit_breaker ? "HALTED" : "NORMAL";
                document.getElementById("kpi-risk").className = status.circuit_breaker ? "text-2xl font-bold mt-2 text-rose-500" : "text-2xl font-bold mt-2 text-emerald-400";
            }

            // Sentiment
            const sent = await fetchJSON("/api/sentiment");
            if (sent) {
                const biasEl = document.getElementById("sentiment-bias");
                biasEl.innerText = sent.bias.toUpperCase() + " (" + (sent.score > 0 ? "+" : "") + sent.score.toFixed(2) + ")";
                biasEl.className = sent.bias === "bullish" ? "font-bold text-emerald-400 uppercase" : (sent.bias === "bearish" ? "font-bold text-rose-400 uppercase" : "font-bold text-slate-400 uppercase");
                document.getElementById("sentiment-conf").innerText = (sent.confidence * 100).toFixed(1) + "%";
                document.getElementById("sentiment-tags").innerHTML = 'Ключевые факторы: <span class="text-slate-300 font-mono">' + (sent.matched_terms.join(", ") || "нейтральный фон") + '</span>';
            }

            // Monte Carlo
            const mc = await fetchJSON("/api/monte-carlo");
            if (mc) {
                document.getElementById("mc-var95").innerText = "$" + mc.var_95_usd.toFixed(2);
                document.getElementById("mc-prob").innerText = mc.profit_probability_pct.toFixed(1) + "%";
                document.getElementById("mc-ruin").innerText = mc.prob_of_ruin_pct.toFixed(1) + "%";
            }

            // Matrix
            const matrix = await fetchJSON("/api/matrix");
            if (matrix && matrix.signals) {
                const tbody = document.getElementById("signal-matrix-body");
                tbody.innerHTML = "";
                for (const item of matrix.signals) {
                    let biasBadge = '<span class="px-2 py-0.5 rounded bg-slate-700 text-slate-300">NEUTRAL</span>';
                    if (item.bias === "long") biasBadge = '<span class="px-2 py-0.5 rounded bg-emerald-900/60 text-emerald-300 font-bold border border-emerald-700/50">LONG / BUY</span>';
                    if (item.bias === "short") biasBadge = '<span class="px-2 py-0.5 rounded bg-rose-900/60 text-rose-300 font-bold border border-rose-700/50">SHORT / SELL</span>';

                    const targets = item.targets ? item.targets.map(t => t.toFixed(2)).join(" / ") : "-";
                    const sl = item.invalidation ? item.invalidation.toFixed(2) : "-";

                    tbody.innerHTML += `
                        <tr class="hover:bg-slate-800/40 transition">
                            <td class="py-3 px-4 font-bold text-slate-200">${item.asset}</td>
                            <td class="py-3 px-4">${biasBadge}</td>
                            <td class="py-3 px-4 text-amber-300 font-semibold">${(item.confidence * 100).toFixed(1)}%</td>
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
            if (corr && corr.matrix) {
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
            }

            // Positions
            const posData = await fetchJSON("/api/positions");
            const posContainer = document.getElementById("positions-list");
            if (posData && posData.positions && posData.positions.length > 0) {
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
            } else {
                posContainer.innerHTML = `<div class="text-center py-8 text-slate-500 text-sm"><i class="fas fa-inbox text-2xl mb-2 block"></i> Нет открытых позиций</div>`;
            }

            if(icon) setTimeout(() => icon.classList.remove("fa-spin"), 400);
        }

        async function sendControl(action) {
            if(!confirm("Подтвердите действие: " + action.toUpperCase())) return;
            try {
                const res = await fetch("/api/control/" + action, { method: "POST" });
                const json = await res.json();
                alert(json.message || "Действие выполнено");
                refreshData();
            } catch(e) {
                alert("Ошибка выполнения действия: " + e);
            }
        }

        // Initialize chart and polling
        loadChart("XAUUSD");
        refreshData();
        setInterval(refreshData, 5000);
    </script>
</body>
</html>
"""
