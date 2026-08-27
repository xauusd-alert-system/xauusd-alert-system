"""Feature engineering package.

FEATURES_SCHEMA_VERSION (ТЗ 8.3) — version of the FEATURE SET CATALOG
stored in the Feature Store (features/feature_store.py).

Rules:

* Increment manually whenever the SET of features consumed by the model
  changes — a feature is added, removed, renamed, or its computation
  semantics change (same name, different meaning/values).
* Purely internal refactors that provably keep values identical do NOT
  require an increment.
* The version participates in the deterministic ``snapshot_id`` and the
  UNIQUE contract of ``feature_snapshots``, so snapshots of different
  versions never mix (see FeatureStore.get_latest / get_range).

History:

* ``v1`` — initial Feature Store catalog (Phase 3, Step 8, ТЗ 8.3).
  Covers the feature set produced by realtime/pipeline.py::_build_features
  (indicators, order_flow, candle_anatomy, structure, bifurcation,
  mtf_confluence, regime indicators) as of the refactor/master-plan branch.
"""
FEATURES_SCHEMA_VERSION = "v1"
