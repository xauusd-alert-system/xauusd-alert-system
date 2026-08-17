/**
 * @deprecated: mock generator, only for local dev without Python backend.
 * Production and staging traffic is proxied directly to realtime/app.py.
 */
export interface MonteCarloResult {
  n_simulations: number;
  horizon_trades: number;
  initial_balance: number;
  median_ending_equity: number;
  mean_ending_equity: number;
  profit_probability_pct: number;
  var_95_usd: number;
  var_99_usd: number;
  cvar_95_usd: number;
  cvar_99_usd: number;
  max_drawdown_median_pct: number;
  max_drawdown_95_pct: number;
  max_drawdown_99_pct: number;
  prob_of_ruin_pct: number;
}

export class MonteCarloSimulator {
  private tradePnls: number[];
  private initialBalance: number;
  private nSimulations: number;
  private horizonTrades: number;

  constructor(
    tradePnls: number[] = [],
    initialBalance: number = 10000.0,
    nSimulations: number = 1000,
    horizonTrades: number = 100
  ) {
    this.tradePnls = tradePnls.length > 0 ? tradePnls : [
      45.2, -32.1, 78.5, -24.0, 112.0, -45.0, 65.4, 88.2, -50.0, 34.0,
      -18.0, 92.5, -60.0, 42.1, 15.0, -28.0, 71.3, 105.0, -40.0, 55.0
    ];
    this.initialBalance = initialBalance;
    this.nSimulations = nSimulations;
    this.horizonTrades = horizonTrades;
  }

  public runSimulation(): MonteCarloResult {
    const endingPnls: number[] = [];
    const maxDrawdownsPct: number[] = [];
    let ruinCount = 0;
    const ruinThreshold = this.initialBalance * 0.5;

    for (let sim = 0; sim < this.nSimulations; sim++) {
      let equity = this.initialBalance;
      let peak = equity;
      let maxDdPct = 0;
      let hitRuin = false;

      for (let step = 0; step < this.horizonTrades; step++) {
        const randIdx = Math.floor(Math.random() * this.tradePnls.length);
        const pnl = this.tradePnls[randIdx];
        equity += pnl;

        if (equity > peak) {
          peak = equity;
        }
        const dd = peak > 0 ? ((peak - equity) / peak) * 100 : 0;
        if (dd > maxDdPct) {
          maxDdPct = dd;
        }
        if (equity <= ruinThreshold) {
          hitRuin = true;
        }
      }

      if (hitRuin) ruinCount++;
      endingPnls.push(equity - this.initialBalance);
      maxDrawdownsPct.push(maxDdPct);
    }

    endingPnls.sort((a, b) => a - b);
    maxDrawdownsPct.sort((a, b) => a - b);

    const percentile = (arr: number[], p: number) => {
      const idx = Math.min(Math.floor((p / 100) * arr.length), arr.length - 1);
      return arr[idx];
    };

    const var95 = percentile(endingPnls, 5);
    const var99 = percentile(endingPnls, 1);

    const tail95 = endingPnls.filter((p) => p <= var95);
    const tail99 = endingPnls.filter((p) => p <= var99);

    const cvar95 = tail95.length > 0 ? tail95.reduce((a, b) => a + b, 0) / tail95.length : var95;
    const cvar99 = tail99.length > 0 ? tail99.reduce((a, b) => a + b, 0) / tail99.length : var99;

    const profitableRuns = endingPnls.filter((p) => p > 0).length;
    const profitProbPct = (profitableRuns / this.nSimulations) * 100;
    const probOfRuinPct = (ruinCount / this.nSimulations) * 100;

    const medianEndingEquity = this.initialBalance + percentile(endingPnls, 50);
    const meanEndingEquity = this.initialBalance + endingPnls.reduce((a, b) => a + b, 0) / this.nSimulations;

    return {
      n_simulations: this.nSimulations,
      horizon_trades: this.horizonTrades,
      initial_balance: this.initialBalance,
      median_ending_equity: Number(medianEndingEquity.toFixed(2)),
      mean_ending_equity: Number(meanEndingEquity.toFixed(2)),
      profit_probability_pct: Number(profitProbPct.toFixed(1)),
      var_95_usd: Number(var95.toFixed(2)),
      var_99_usd: Number(var99.toFixed(2)),
      cvar_95_usd: Number(cvar95.toFixed(2)),
      cvar_99_usd: Number(cvar99.toFixed(2)),
      max_drawdown_median_pct: Number(percentile(maxDrawdownsPct, 50).toFixed(1)),
      max_drawdown_95_pct: Number(percentile(maxDrawdownsPct, 95).toFixed(1)),
      max_drawdown_99_pct: Number(percentile(maxDrawdownsPct, 99).toFixed(1)),
      prob_of_ruin_pct: Number(probOfRuinPct.toFixed(1)),
    };
  }
}
