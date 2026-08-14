import inspect
import scripts.deflated_sharpe as m

s = inspect.getsource(m.decision_gate)
needle = 'cscv.get("is_oos_slope") is None'
print("decision_gate has new is_oos_slope handling:", needle in s)

if needle not in s:
    for i, line in enumerate(s.splitlines()):
        if "is_oos_slope" in line:
            print(f"  line {i}: {line}")