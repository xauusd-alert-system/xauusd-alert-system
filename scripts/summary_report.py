import glob
import os

import pandas as pd


def main():
    files = sorted(glob.glob("logs/backtest_*.csv"))
    if not files:
        print("Файлы бэктестов не найдены в logs/")
        return

    print("=== 🏆 СВОДНЫЙ ОТЧЕТ МУЛЬТИ-АКТИВНОГО ПОРТФЕЛЯ ===\n")
    print(f"{'Актив':<10} | {'Сделок':<8} | {'WinRate':<8} | {'ProfitFactor':<13} | {'PnL ($)':<10}")
    print("-" * 60)

    total_trades = 0
    total_pnl = 0.0

    for f in files:
        asset_name = os.path.basename(f).replace("backtest_", "").replace(".csv", "").upper()
        df = pd.read_csv(f)
        trades = int(df["n_trades"].sum())
        wr = df["win_rate"].mean()
        pf = df["profit_factor"].mean()
        pnl = df["total_pnl"].sum()
        total_trades += trades
        total_pnl += pnl
        print(f"{asset_name:<10} | {trades:<8} | {wr:<7.1f}% | {pf:<13.2f} | ${pnl:<10.2f}")

    print("-" * 60)
    print(f"ОБЩИЙ ИТОГ ПОРТФЕЛЯ: {total_trades} сделок | Суммарный PnL: +${total_pnl:.2f}")


if __name__ == "__main__":
    main()
