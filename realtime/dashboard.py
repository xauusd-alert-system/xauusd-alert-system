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
            
            <!-- Broker controls intentionally live only in authenticated Telegram. -->
            <div class="text-xs text-slate-400 border border-slate-700 rounded-lg px-3 py-2">
                <i class="fas fa-lock"></i> Управление исполнением: только авторизованный Telegram
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
                document.getElementById("sentiment-tags").innerHTML = '<span class="text-slate-500">Данные недоступны: реальный источник новостей не настроен</span>';
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

        async function sendControl(action) {
            if(!confirm("Подтвердите действие: " + action.toUpperCase())) return;
            try {
                const res = await fetch("/api/control/" + action, { method: "POST" });
                const json = await res.json();
                if (!res.ok) {
                    alert("Действие не выполнено: " + (json.detail || res.statusText));
                    return;
                }
                alert(json.message || "Действие выполнено");
                refreshData();
            } catch(e) {
                alert("Ошибка выполнения действия: " + e);
            }
        }

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
        setInterval(refreshData, 5000);
    </script>
</body>
</html>
"""
