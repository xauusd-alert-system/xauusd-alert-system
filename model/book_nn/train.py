"""Training loop with Train/Validation divergence monitoring (task T-17).

Implements the book's experiment protocol (NN book pages 255-256):
time-ordered train/validation splits, per-epoch loss curves, and an explicit
overfitting alarm when the Train curve keeps improving while Validation
stalls or degrades - the exact failure mode the book demonstrates on
page 256 and the direct argument for walk-forward validation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from model.book_nn.losses import get_loss
from model.book_nn.optim import Adam

logger = logging.getLogger("book_nn.train")


@dataclass
class DivergenceConfig:
    """When to raise the Train/Val divergence alarm (task T-17)."""

    patience: int = 10            # epochs without val improvement before alarming
    min_train_progress: float = 0.01  # train must still improve by this rel. share
    val_worsen_ratio: float = 1.10    # val above 110% of its best -> degraded
    min_epochs: int = 5           # warm-up before alarms can fire


@dataclass
class TrainHistory:
    train_loss: list[float] = field(default_factory=list)
    val_loss: list[float] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    best_epoch: int = -1
    stopped_early: bool = False

    def to_rows(self) -> list[dict]:
        rows = []
        for epoch, (tr, va) in enumerate(zip(self.train_loss, self.val_loss)):
            rows.append({"epoch": epoch, "train_loss": tr, "val_loss": va})
        return rows

    @property
    def divergence_detected(self) -> bool:
        return any("divergence" in a for a in self.alerts)


def _iterate_minibatches(n: int, batch_size: int, rng: np.random.Generator):
    idx = rng.permutation(n)
    for start in range(0, n, batch_size):
        yield idx[start:start + batch_size]


def fit(network, X_train: np.ndarray, y_train: np.ndarray,
        X_val: np.ndarray | None = None, y_val: np.ndarray | None = None,
        epochs: int = 100, batch_size: int = 32, lr: float = 1e-3,
        loss: str = "mse", optimizer=None, seed: int = 42,
        divergence_cfg: DivergenceConfig | None = None,
        early_stopping: bool = False, verbose: bool = False) -> TrainHistory:
    """Mini-batch training with the book's Adam defaults (beta1=0.9,
    beta2=0.999) and Train/Val divergence monitoring.

    The validation set must be a LATER time slice than the training set
    (the caller enforces time ordering - see model/sample_generator.py);
    random shuffling happens only WITHIN the training set.
    """
    loss_fn = get_loss(loss)
    div_cfg = divergence_cfg or DivergenceConfig()
    history = TrainHistory()
    if optimizer is None:
        optimizer = Adam(lr=lr)
    rng = np.random.default_rng(seed)
    X_train = np.asarray(X_train, dtype=float)
    y_train = np.asarray(y_train, dtype=float)
    n = len(X_train)
    if n == 0:
        raise ValueError("empty training set")

    best_val = np.inf
    best_epoch = -1
    best_train_at_alarm = None
    for epoch in range(epochs):
        batch_losses = []
        for batch_idx in _iterate_minibatches(n, batch_size, rng):
            xb, yb = X_train[batch_idx], y_train[batch_idx]
            network.zero_grad()
            pred = network.forward(xb)
            _l, dpred = loss_fn(pred, yb)
            network.backward(dpred)
            optimizer.step(network.parameters())
            batch_losses.append(_l)
        train_loss = float(np.mean(batch_losses))
        history.train_loss.append(train_loss)

        if X_val is not None and y_val is not None and len(X_val) > 0:
            val_pred = network.forward(np.asarray(X_val, dtype=float))
            val_loss = loss_fn(val_pred, np.asarray(y_val, dtype=float))[0]
        else:
            val_loss = float("nan")
        history.val_loss.append(val_loss)

        # ---- divergence monitoring (T-17) -------------------------------
        if np.isfinite(val_loss):
            if val_loss < best_val:
                best_val, best_epoch = val_loss, epoch
            elif epoch >= div_cfg.min_epochs:
                stale = epoch - best_epoch
                if stale >= div_cfg.patience:
                    train_progress = (history.train_loss[max(0, epoch - div_cfg.patience)]
                                      - train_loss)
                    train_ref = max(abs(history.train_loss[max(0, epoch - div_cfg.patience)]),
                                    1e-12)
                    still_learning = train_progress / train_ref > div_cfg.min_train_progress
                    val_degraded = val_loss > best_val * div_cfg.val_worsen_ratio
                    if still_learning and (val_degraded or stale >= 2 * div_cfg.patience):
                        msg = (f"Train/Val divergence at epoch {epoch}: train "
                               f"{train_loss:.6f} still improving while val "
                               f"{val_loss:.6f} stuck above best {best_val:.6f} "
                               f"({stale} stale epochs) - overfitting / "
                               f"unrepresentative validation (book p. 256)")
                        if msg not in history.alerts:
                            history.alerts.append(msg)
                            logger.warning(msg)
                        best_train_at_alarm = train_loss
                        if early_stopping:
                            history.stopped_early = True
                            break
        if verbose and (epoch % max(1, epochs // 10) == 0 or epoch == epochs - 1):
            logger.info("epoch %d train=%.6f val=%.6f", epoch, train_loss, val_loss)

    history.best_epoch = best_epoch
    return history
