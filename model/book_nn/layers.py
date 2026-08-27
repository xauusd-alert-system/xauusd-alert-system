"""Neural layers of the book NN library (pure NumPy port).

Implements the layer types the book builds in MQL5 (NN book ch. 3-6), with
exact backpropagation so the numerical gradient check (ch. 3.10, task T-11)
can validate every one of them:

* ``FCLayer``                 - fully connected layer + activation (ch. 3.6);
* ``LSTMLayer``               - recurrent block with 4 gates and BPTT (ch. 4.2);
* ``MultiHeadAttentionLayer`` - scaled dot-product attention with `heads`
  parallel heads (ch. 5.1/5.2, `descr.step` = number of heads) and a learned
  query decoder producing `window_out` outputs (the book's window ->
  window_out semantics); optional causal mask for GPT-style blocks (ch. 5.3);
* ``GPTStyleBlock``           - causal self-attention + position-wise feed-forward
  with residual connections (ch. 5.3 skeleton, task T-24).

Every layer follows the same contract:

    out = layer.forward(x, training=False)   # caches what backward needs
    dx  = layer.backward(grad_out)           # ACCUMULATES parameter grads
    layer.parameters() -> [(name, weight, grad), ...]

Shapes: batch-first. Sequence layers take/return (B, T, D); ``FCLayer``
takes (B, D) (the network flattens sequence outputs before FC layers).
Grads accumulate with ``+=`` so the training loop zeroes them between steps
(the Adam optimizer does this via ``parameters()`` references).
"""
from __future__ import annotations

import numpy as np

from model.book_nn.activations import get_activation

Params = list  # list[tuple[str, np.ndarray, np.ndarray]]


def _init_matrix(rng: np.random.Generator, fan_in: int, fan_out: int) -> np.ndarray:
    """Xavier/Glorot init (book ch. 1.3 stresses init quality)."""
    scale = np.sqrt(2.0 / (fan_in + fan_out))
    return rng.normal(0.0, scale, size=(fan_in, fan_out))


def _softmax(scores: np.ndarray, axis: int = -1) -> np.ndarray:
    shifted = scores - scores.max(axis=axis, keepdims=True)
    e = np.exp(shifted)
    return e / e.sum(axis=axis, keepdims=True)


def _softmax_backward(prob: np.ndarray, grad: np.ndarray, axis: int = -1) -> np.ndarray:
    """d Scores given d(softmax output) - standard Jacobian-vector product."""
    inner = (grad * prob).sum(axis=axis, keepdims=True)
    return prob * (grad - inner)


class FCLayer:
    """Fully connected layer: y = act(x @ W + b) (book ch. 3.6)."""

    def __init__(self, in_dim: int, out_dim: int, activation: str = "linear",
                 rng: np.random.Generator | None = None):
        rng = rng or np.random.default_rng()
        self.in_dim, self.out_dim = int(in_dim), int(out_dim)
        self.W = _init_matrix(rng, self.in_dim, self.out_dim)
        self.b = np.zeros(self.out_dim)
        self.act, self.act_deriv = get_activation(activation)
        self.activation_name = activation
        self.dW = np.zeros_like(self.W)
        self.db = np.zeros_like(self.b)
        self._x = None
        self._z = None

    def forward(self, x: np.ndarray, training: bool = False) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        self._x = x
        self._z = x @ self.W + self.b
        return self.act(self._z)

    def backward(self, grad_out: np.ndarray) -> np.ndarray:
        dz = np.asarray(grad_out, dtype=float) * self.act_deriv(self._z)
        x2d = self._x.reshape(-1, self._x.shape[-1])
        dz2d = dz.reshape(-1, dz.shape[-1])
        self.dW += x2d.T @ dz2d
        self.db += dz2d.sum(axis=0)
        return dz @ self.W.T

    def parameters(self) -> Params:
        return [("W", self.W, self.dW), ("b", self.b, self.db)]

    def output_shape(self, input_shape: tuple[int, ...]) -> tuple[int, ...]:
        return input_shape[:-1] + (self.out_dim,)


class _MHACore:
    """Scaled dot-product attention core (book ch. 5.1, p. 405).

        Score = Q K^T / sqrt(d_k);  A = Softmax(Score);  Out = A V

    Kept private: the public layers below compose one or two attention
    stages out of it.
    """

    def __init__(self, dim: int, heads: int, rng: np.random.Generator):
        if dim % heads != 0:
            raise ValueError(f"model_dim {dim} not divisible by heads {heads}")
        self.dim, self.heads = dim, heads
        self.head_dim = dim // heads
        self.scale = np.sqrt(self.head_dim)
        self.Wq = _init_matrix(rng, dim, dim)
        self.Wk = _init_matrix(rng, dim, dim)
        self.Wv = _init_matrix(rng, dim, dim)
        self.Wo = _init_matrix(rng, dim, dim)
        self.dWq = np.zeros_like(self.Wq)
        self.dWk = np.zeros_like(self.Wk)
        self.dWv = np.zeros_like(self.Wv)
        self.dWo = np.zeros_like(self.Wo)
        self._cache = None

    def split_heads(self, x: np.ndarray) -> np.ndarray:
        """(B, T, dim) -> (B, heads, T, head_dim)."""
        b, t, _ = x.shape
        return x.reshape(b, t, self.heads, self.head_dim).transpose(0, 2, 1, 3)

    def merge_heads(self, x: np.ndarray) -> np.ndarray:
        """(B, heads, T, head_dim) -> (B, T, dim)."""
        b, h, t, d = x.shape
        return x.transpose(0, 2, 1, 3).reshape(b, t, h * d)

    def forward(self, value_in: np.ndarray, query_in: np.ndarray,
                causal: bool = False) -> np.ndarray:
        """value_in: (B, Tv, dim); query_in: (B, Tq, dim).

        Returns the multi-head context (B, Tq, dim); caches intermediates.
        """
        q = self.split_heads(query_in @ self.Wq)           # (B,h,Tq,dh)
        k = self.split_heads(value_in @ self.Wk)           # (B,h,Tv,dh)
        v = self.split_heads(value_in @ self.Wv)           # (B,h,Tv,dh)
        scores = q @ k.transpose(0, 1, 3, 2) / self.scale   # (B,h,Tq,Tv)
        if causal:
            t_q, t_v = scores.shape[-2], scores.shape[-1]
            mask = np.triu(np.ones((t_q, t_v), dtype=bool), k=1)
            scores = np.where(mask, -1e9, scores)
        attn = _softmax(scores, axis=-1)                   # (B,h,Tq,Tv)
        context = attn @ v                                 # (B,h,Tq,dh)
        merged = self.merge_heads(context)                 # (B,Tq,dim)
        out = merged @ self.Wo                             # (B,Tq,dim)
        self._cache = (value_in, query_in, q, k, v, attn, merged)
        return out

    def backward(self, grad_out: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Returns (d value_in, d query_in)."""
        value_in, query_in, q, k, v, attn, merged = self._cache
        grad_out = np.asarray(grad_out, dtype=float)

        self.dWo += merged.reshape(-1, self.dim).T @ grad_out.reshape(-1, self.dim)
        d_merged = grad_out @ self.Wo.T                               # (B,Tq,dim)
        d_context = self.split_heads(d_merged)                        # (B,h,Tq,dh)
        d_v = attn.transpose(0, 1, 3, 2) @ d_context                  # (B,h,Tv,dh)
        d_attn = d_context @ v.transpose(0, 1, 3, 2)                  # (B,h,Tq,Tv)
        d_scores = _softmax_backward(attn, d_attn, axis=-1)           # (B,h,Tq,Tv)

        d_q = d_scores @ k / self.scale                               # (B,h,Tq,dh)
        d_k = d_scores.transpose(0, 1, 3, 2) @ q / self.scale         # (B,h,Tv,dh)

        d_query_in = self.merge_heads(d_q) @ self.Wq.T                # (B,Tq,dim)
        d_value_in = (self.merge_heads(d_k) @ self.Wk.T
                      + self.merge_heads(d_v) @ self.Wv.T)            # (B,Tv,dim)

        self.dWq += query_in.reshape(-1, self.dim).T @ \
            self.merge_heads(d_q).reshape(-1, self.dim)
        self.dWk += value_in.reshape(-1, self.dim).T @ \
            self.merge_heads(d_k).reshape(-1, self.dim)
        self.dWv += value_in.reshape(-1, self.dim).T @ \
            self.merge_heads(d_v).reshape(-1, self.dim)
        return d_value_in, d_query_in

    def parameters(self) -> Params:
        return [("Wq", self.Wq, self.dWq), ("Wk", self.Wk, self.dWk),
                ("Wv", self.Wv, self.dWv), ("Wo", self.Wo, self.dWo)]


class MultiHeadAttentionLayer:
    """Multi-Head Attention layer (book ch. 5.2, p. 459-515).

    Pipeline (task T-09 configuration: heads=8 -> ``step=8`` in the book's
    CLayerDescription, window_out=8):

        X (B, window, in_dim)
          -> input embedding            (B, window, model_dim)
          -> self-attention over window (heads parallel heads; causal optional)
          -> decoder with `out_len` learned queries (cross-attention)
          -> per-position output projection + activation
          -> Y (B, out_len, out_dim)

    The learned-query decoder implements the book's window -> window_out
    semantics: the layer emits ``out_len`` feature vectors that downstream
    FC layers map to the final regression targets. With ``causal=True`` the
    self-attention stage cannot see the future (GPT-style, ch. 5.3).
    """

    def __init__(self, in_dim: int, model_dim: int, heads: int, out_len: int,
                 out_dim: int, activation: str = "linear", causal: bool = False,
                 rng: np.random.Generator | None = None):
        rng = rng or np.random.default_rng()
        self.in_dim, self.model_dim = int(in_dim), int(model_dim)
        self.heads, self.out_len, self.out_dim = int(heads), int(out_len), int(out_dim)
        self.causal = bool(causal)
        self.Win = _init_matrix(rng, self.in_dim, self.model_dim)
        self.bin = np.zeros(self.model_dim)
        self.self_attn = _MHACore(self.model_dim, self.heads, rng)
        self.Wq2 = _init_matrix(rng, self.model_dim, self.model_dim)
        self.Wk2 = _init_matrix(rng, self.model_dim, self.model_dim)
        self.Wv2 = _init_matrix(rng, self.model_dim, self.model_dim)
        self.Wo2 = _init_matrix(rng, self.model_dim, self.model_dim)
        self.Qd = rng.normal(0.0, 0.5, size=(self.out_len, self.model_dim))
        self.Wf = _init_matrix(rng, self.model_dim, self.out_dim)
        self.bf = np.zeros(self.out_dim)
        self.act, self.act_deriv = get_activation(activation)
        self.activation_name = activation
        self.dWin = np.zeros_like(self.Win)
        self.dbin = np.zeros_like(self.bin)
        self.dWq2 = np.zeros_like(self.Wq2)
        self.dWk2 = np.zeros_like(self.Wk2)
        self.dWv2 = np.zeros_like(self.Wv2)
        self.dWo2 = np.zeros_like(self.Wo2)
        self.dQd = np.zeros_like(self.Qd)
        self.dWf = np.zeros_like(self.Wf)
        self.dbf = np.zeros_like(self.bf)
        self._cache = None

    def forward(self, x: np.ndarray, training: bool = False) -> np.ndarray:
        x = np.asarray(x, dtype=float)                     # (B,T,in_dim)
        emb = x @ self.Win + self.bin                      # (B,T,dm)
        encoded = self.self_attn.forward(emb, emb, causal=self.causal)  # (B,T,dm)

        # decoder: learned queries Qd attend over the encoded window
        q2 = (self.Qd @ self.Wq2).reshape(self.out_len, self.heads,
                                          self.self_attn.head_dim).transpose(1, 0, 2)  # (h,L,dh)
        k2 = self.self_attn.split_heads(encoded @ self.Wk2)   # (B,h,T,dh)
        v2 = self.self_attn.split_heads(encoded @ self.Wv2)   # (B,h,T,dh)
        scores2 = q2[None] @ k2.transpose(0, 1, 3, 2) / self.self_attn.scale  # (B,h,L,T)
        attn2 = _softmax(scores2, axis=-1)                    # (B,h,L,T)
        context2 = attn2 @ v2                                 # (B,h,L,dh)
        merged2 = self.self_attn.merge_heads(context2)        # (B,L,dm)
        decoded = merged2 @ self.Wo2                          # (B,L,dm)
        z = decoded @ self.Wf + self.bf                       # (B,L,out_dim)

        self._cache = (x, emb, encoded, q2, k2, v2, attn2, merged2, decoded, z)
        return self.act(z)

    def backward(self, grad_out: np.ndarray) -> np.ndarray:
        x, emb, encoded, q2, k2, v2, attn2, merged2, decoded, z = self._cache
        dz = np.asarray(grad_out, dtype=float) * self.act_deriv(z)  # (B,L,out_dim)

        decoded2d = decoded.reshape(-1, self.model_dim)
        dz2d = dz.reshape(-1, self.out_dim)
        self.dWf += decoded2d.T @ dz2d
        self.dbf += dz2d.sum(axis=0)
        d_decoded = dz @ self.Wf.T                            # (B,L,dm)

        merged2d = merged2.reshape(-1, self.model_dim)
        self.dWo2 += merged2d.T @ d_decoded.reshape(-1, self.model_dim)
        d_merged2 = d_decoded @ self.Wo2.T                    # (B,L,dm)
        d_context2 = self.self_attn.split_heads(d_merged2)    # (B,h,L,dh)
        d_v2 = attn2.transpose(0, 1, 3, 2) @ d_context2       # (B,h,T,dh)
        d_attn2 = d_context2 @ v2.transpose(0, 1, 3, 2)       # (B,h,L,T)
        d_scores2 = _softmax_backward(attn2, d_attn2, axis=-1)  # (B,h,L,T)

        d_q2 = d_scores2 @ k2 / self.self_attn.scale                # (B,h,L,dh)
        d_k2 = d_scores2.transpose(0, 1, 3, 2) @ q2[None] / self.self_attn.scale  # (B,h,T,dh)

        # encoded side of the cross-attention projections
        d_encoded = (self.self_attn.merge_heads(d_k2) @ self.Wk2.T
                     + self.self_attn.merge_heads(d_v2) @ self.Wv2.T)  # (B,T,dm)
        encoded2d = encoded.reshape(-1, self.model_dim)
        self.dWk2 += encoded2d.T @ self.self_attn.merge_heads(d_k2).reshape(-1, self.model_dim)
        self.dWv2 += encoded2d.T @ self.self_attn.merge_heads(d_v2).reshape(-1, self.model_dim)

        # learned queries + their projection
        d_q2_sum = d_q2.sum(axis=0)                           # (h,L,dh)
        d_q2_flat = d_q2_sum.transpose(1, 0, 2).reshape(self.out_len, self.model_dim)
        self.dQd += d_q2_flat @ self.Wq2.T
        self.dWq2 += self.Qd.T @ d_q2_flat

        # self-attention stage: value and query inputs are both `emb`
        d_emb_from_value, d_emb_from_query = self.self_attn.backward(d_encoded)
        d_emb = d_emb_from_value + d_emb_from_query           # (B,T,dm)

        x2d = x.reshape(-1, self.in_dim)
        d_emb2d = d_emb.reshape(-1, self.model_dim)
        self.dWin += x2d.T @ d_emb2d
        self.dbin += d_emb2d.sum(axis=0)
        return d_emb @ self.Win.T                             # (B,T,in_dim)

    def parameters(self) -> Params:
        params: Params = [("Win", self.Win, self.dWin), ("bin", self.bin, self.dbin)]
        params += self.self_attn.parameters()
        params += [("Wq2", self.Wq2, self.dWq2), ("Wk2", self.Wk2, self.dWk2),
                   ("Wv2", self.Wv2, self.dWv2), ("Wo2", self.Wo2, self.dWo2),
                   ("Qd", self.Qd, self.dQd), ("Wf", self.Wf, self.dWf),
                   ("bf", self.bf, self.dbf)]
        return params

    def output_shape(self, input_shape: tuple[int, ...]) -> tuple[int, ...]:
        return input_shape[:1] + (self.out_len, self.out_dim)


class LSTMLayer:
    """LSTM layer with 4 gates and backpropagation through time (book ch. 4.2).

    Gate equations (Hochreiter-Schmidhuber form used by the book, p. 338-349):

        f_t = sigma(W_f [h_{t-1}, x_t] + b_f)     (forget)
        i_t = sigma(W_i [h_{t-1}, x_t] + b_i)     (input)
        g_t = tanh (W_g [h_{t-1}, x_t] + b_g)
        c_t = f_t * c_{t-1} + i_t * g_t           (memory)
        o_t = sigma(W_o [h_{t-1}, x_t] + b_o)     (output)
        h_t = o_t * tanh(c_t)                     (hidden)

    All four gates live in one (in_dim + hidden, 4*hidden) weight set for
    speed. The final hidden state feeds a projection to out_dim.
    """

    def __init__(self, in_dim: int, hidden: int, out_dim: int,
                 activation: str = "linear", rng: np.random.Generator | None = None):
        rng = rng or np.random.default_rng()
        self.in_dim, self.hidden, self.out_dim = int(in_dim), int(hidden), int(out_dim)
        self.Wx = _init_matrix(rng, self.in_dim, 4 * self.hidden)
        self.Wh = _init_matrix(rng, self.hidden, 4 * self.hidden)
        self.b = np.zeros(4 * self.hidden)
        self.Wy = _init_matrix(rng, self.hidden, self.out_dim)
        self.by = np.zeros(self.out_dim)
        self.act, self.act_deriv = get_activation(activation)
        self.activation_name = activation
        self.dWx = np.zeros_like(self.Wx)
        self.dWh = np.zeros_like(self.Wh)
        self.db = np.zeros_like(self.b)
        self.dWy = np.zeros_like(self.Wy)
        self.dby = np.zeros_like(self.by)
        self._cache = None

    def forward(self, x: np.ndarray, training: bool = False) -> np.ndarray:
        x = np.asarray(x, dtype=float)                 # (B,T,D)
        b, t_len, _ = x.shape
        h = np.zeros((b, self.hidden))
        c = np.zeros((b, self.hidden))
        h_prevs, i_gates, f_gates, o_gates, g_gates = [], [], [], [], []
        c_states, c_hats = [], []
        for t in range(t_len):
            z = x[:, t, :] @ self.Wx + h @ self.Wh + self.b
            zi, zf, zo, zg = np.split(z, 4, axis=1)
            i_g = 1.0 / (1.0 + np.exp(-zi))
            f_g = 1.0 / (1.0 + np.exp(-zf))
            o_g = 1.0 / (1.0 + np.exp(-zo))
            g_g = np.tanh(zg)
            h_prevs.append(h)
            i_gates.append(i_g)
            f_gates.append(f_g)
            o_gates.append(o_g)
            g_gates.append(g_g)
            c = f_g * c + i_g * g_g
            c_states.append(c)
            c_hat = np.tanh(c)
            c_hats.append(c_hat)
            h = o_g * c_hat
        zy = h @ self.Wy + self.by
        self._cache = (x, h_prevs, i_gates, f_gates, o_gates, g_gates,
                       c_states, c_hats, h, zy)
        return self.act(zy)

    def backward(self, grad_out: np.ndarray) -> np.ndarray:
        x, h_prevs, i_gates, f_gates, o_gates, g_gates, c_states, c_hats, h_last, zy = self._cache
        dz = np.asarray(grad_out, dtype=float) * self.act_deriv(zy)
        self.dWy += h_last.T @ dz
        self.dby += dz.sum(axis=0)
        dh = dz @ self.Wy.T                              # (B,H)
        dc = np.zeros_like(dh)
        dx = np.zeros_like(x)                            # (B,T,D)
        t_len = x.shape[1]
        for t in range(t_len - 1, -1, -1):
            i_g, f_g, o_g, g_g = i_gates[t], f_gates[t], o_gates[t], g_gates[t]
            c_prev = c_states[t - 1] if t > 0 else np.zeros_like(dc)
            c_hat = c_hats[t]
            h_prev = h_prevs[t]

            do = dh * c_hat
            dc += dh * o_g * (1.0 - c_hat * c_hat)
            df = dc * c_prev
            di = dc * g_g
            dg = dc * i_g

            dzi = di * i_g * (1.0 - i_g)
            dzf = df * f_g * (1.0 - f_g)
            dzo = do * o_g * (1.0 - o_g)
            dzg = dg * (1.0 - g_g * g_g)
            dz_t = np.concatenate([dzi, dzf, dzo, dzg], axis=1)  # (B,4H)

            self.dWx += x[:, t, :].T @ dz_t
            self.dWh += h_prev.T @ dz_t
            self.db += dz_t.sum(axis=0)

            dx[:, t, :] = dz_t @ self.Wx.T
            dc = dc * f_g
            dh = dz_t @ self.Wh.T
        return dx

    def parameters(self) -> Params:
        return [("Wx", self.Wx, self.dWx), ("Wh", self.Wh, self.dWh),
                ("b", self.b, self.db), ("Wy", self.Wy, self.dWy),
                ("by", self.by, self.dby)]

    def output_shape(self, input_shape: tuple[int, ...]) -> tuple[int, ...]:
        return input_shape[:1] + (self.out_dim,)


class GPTStyleBlock:
    """GPT-style decoder block skeleton (book ch. 5.3, task T-24).

        x -> embed -> causal self-attention -> +residual -> affine norm
          -> position-wise FF (2 FC layers, ReLU inside, book p. 404-405)
          -> +residual -> out

    Sequence in, sequence out (same length): the autoregressive "next
    element" head is expected to be the following FC layers of the network.
    Full GPT training is deliberately deferred per TZ T-24 (needs
    walk-forward stabilization + GPU); this block exists so the causal
    attention path is built, gradient-checked and ready.
    """

    def __init__(self, in_dim: int, model_dim: int, heads: int, ff_dim: int,
                 rng: np.random.Generator | None = None):
        rng = rng or np.random.default_rng()
        self.in_dim, self.model_dim = int(in_dim), int(model_dim)
        self.embed = FCLayer(self.in_dim, self.model_dim, "linear", rng=rng)
        self.attention = _MHACore(self.model_dim, int(heads), rng)
        self.norm_w = np.ones(self.model_dim)
        self.norm_b = np.zeros(self.model_dim)
        self.ff1 = FCLayer(self.model_dim, ff_dim, "relu", rng=rng)
        self.ff2 = FCLayer(ff_dim, self.model_dim, "linear", rng=rng)
        self.dnorm_w = np.zeros_like(self.norm_w)
        self.dnorm_b = np.zeros_like(self.norm_b)
        self._cache = None

    def forward(self, x: np.ndarray, training: bool = False) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        emb = self.embed.forward(x)                                  # (B,T,dm)
        att = self.attention.forward(emb, emb, causal=True)          # (B,T,dm)
        norm_in = emb + att                                          # residual 1
        norm = norm_in * self.norm_w + self.norm_b
        ff = self.ff2.forward(self.ff1.forward(norm))                # (B,T,dm)
        out = norm + ff                                              # residual 2
        self._cache = (x, emb, att, norm_in, norm, ff)
        return out

    def backward(self, grad_out: np.ndarray) -> np.ndarray:
        _x, emb, att, norm_in, norm, _ff = self._cache
        grad_out = np.asarray(grad_out, dtype=float)

        # out = norm + ff: norm receives the direct gradient plus everything
        # that flows through the position-wise feed-forward sub-block.
        d_ff_out = grad_out
        d_ff1_out = self.ff2.backward(d_ff_out)
        d_norm = self.ff1.backward(d_ff1_out) + grad_out             # (B,T,dm)

        self.dnorm_w += (norm * d_norm).reshape(-1, self.model_dim).sum(axis=0)
        self.dnorm_b += d_norm.reshape(-1, self.model_dim).sum(axis=0)

        d_norm_in = d_norm * self.norm_w                             # (B,T,dm)
        # norm_in = emb + att
        d_att = d_norm_in
        d_emb_from_value, d_emb_from_query = self.attention.backward(d_att)
        d_emb = d_emb_from_value + d_emb_from_query + d_norm_in
        return self.embed.backward(d_emb)                            # (B,T,in_dim)

    def parameters(self) -> Params:
        return ([("norm_w", self.norm_w, self.dnorm_w),
                 ("norm_b", self.norm_b, self.dnorm_b)]
                + self.embed.parameters() + self.attention.parameters()
                + self.ff1.parameters() + self.ff2.parameters())

    def output_shape(self, input_shape: tuple[int, ...]) -> tuple[int, ...]:
        return input_shape[:1] + (input_shape[1], self.model_dim)
