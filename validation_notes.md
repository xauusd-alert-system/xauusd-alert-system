# Gate revision 2026-08-15

Changed gate condition "IS->OOS slope >= 0.5" to "IS->OOS informativeness".
Reason: with near-identical variants (rho_bar ~= 0.98) the per-split OOS-on-IS
regression slope is not a meaningful test of selection transfer. It remains
informative when it passes; when it does not, the gate uses the direct
selection metrics OOS prob loss <= 0.05 and median lambda > 2.0.

This revision is pre-registered before re-running the BTCUSD gate.