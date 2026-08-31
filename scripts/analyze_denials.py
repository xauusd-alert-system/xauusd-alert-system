"""
Analyze denial reason patterns from mt5_trader.log.

Parses `no trade | reasons:` lines and reports avg ml_prob / ml_edge / vol_ratio
and zero-edge frequency. Safe when log is missing or empty.
"""
import re
from collections import Counter
from pathlib import Path

log_file = Path("logs/mt5_trader.log")
denials = []

if not log_file.exists():
    print(f"Log file not found: {log_file} (run trader first)")
else:
    text = log_file.read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        if "no trade" in line and "reasons:" in line:
            prob_match = re.search(r"ml_prob=([\d.]+)<([\d.]+)", line)
            edge_match = re.search(r"ml_edge=([\d.]+)<([\d.]+)", line)
            vol_match = re.search(r"vol_ratio=([\d.]+)<([\d.]+)", line)

            if prob_match and edge_match and vol_match:
                try:
                    denials.append({
                        "prob": float(prob_match.group(1)),
                        "edge": float(edge_match.group(1)),
                        "vol": float(vol_match.group(1)),
                    })
                except ValueError:
                    continue

    if not denials:
        print(f"Total denials: 0 (no matching 'no trade | reasons:' lines in {log_file})")
    else:
        print(f"Total denials: {len(denials)}")
        print(f"Avg ml_prob: {sum(d['prob'] for d in denials)/len(denials):.2f}")
        print(f"Avg ml_edge: {sum(d['edge'] for d in denials)/len(denials):.3f}")
        print(f"Avg vol_ratio: {sum(d['vol'] for d in denials)/len(denials):.2f}")
        print(f"Zero edge cases: {sum(1 for d in denials if d['edge'] < 0.01)}")

        # Optional: top denial codes frequency (if you want to see which code dominates)
        # This helps distinguish night (session/regime) vs strict thresholds
        reason_counter = Counter()
        for line in text.splitlines():
            if "reasons:" in line:
                # extract codes like ml_prob= ml_edge= vol_ratio= regime= session=
                for code in re.findall(r"(\w+)=[^|]+", line.split("reasons:")[-1]):
                    reason_counter[code.strip()] += 1
        if reason_counter:
            print("\nReason frequency:")
            for code, cnt in reason_counter.most_common(10):
                print(f"  {code}: {cnt}")
