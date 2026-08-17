from scripts import train_all_assets


def test_train_all_assets_honors_retraining_freeze(monkeypatch):
    monkeypatch.setattr(
        train_all_assets,
        "load_config",
        lambda: {"retraining": {"enabled": False}},
    )
    monkeypatch.setattr(
        train_all_assets.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not train")),
    )
    assert train_all_assets.main() is None
