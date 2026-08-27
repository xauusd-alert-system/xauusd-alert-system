"""Book NN library - pure NumPy port of the methodology of
"Neural Networks for Algorithmic Trading with MQL5" (MetaQuotes, 690 pp.).

Implements the book's experimental stack for the Python side of
xauusd-alert-system (the MQL5 side stays with the vendored NeuroBook
library, see mql5/NeuroBook/):

* activations / losses / Adam optimizer (book ch. 1.2, 1.4.1, 1.4.3);
* FC, LSTM, Multi-Head Attention and GPT-style layers with exact
  backpropagation (book ch. 3.6, 4.2, 5.1-5.3);
* CLayerDescription-style declarative architectures + model serialization
  to files (book ch. 3.4, task T-20);
* numerical gradient checking (book ch. 3.10, task T-11);
* training loop with Train/Val divergence monitoring (book p. 255-256,
  task T-17);
* architecture-ensemble voting with the TradeLevel threshold (task T-25).
"""
from model.book_nn.activations import available_activations, get_activation
from model.book_nn.gradient_check import assert_gradients_valid, gradient_check
from model.book_nn.layers import (
    FCLayer,
    GPTStyleBlock,
    LSTMLayer,
    MultiHeadAttentionLayer,
)
from model.book_nn.losses import get_loss, mae, mse
from model.book_nn.network import (
    BookNetwork,
    book_fc_baseline_description,
    book_lstm_description,
    book_mha_description,
)
from model.book_nn.optim import Adam
from model.book_nn.train import DivergenceConfig, TrainHistory, fit

__all__ = [
    "Adam", "BookNetwork", "DivergenceConfig", "FCLayer", "GPTStyleBlock",
    "LSTMLayer", "MultiHeadAttentionLayer", "TrainHistory",
    "assert_gradients_valid", "available_activations", "book_fc_baseline_description",
    "book_lstm_description", "book_mha_description", "fit", "get_activation",
    "get_loss", "gradient_check", "mae", "mse",
]
