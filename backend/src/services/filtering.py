"""Forward-only (filtering) HMM recursion for live observation prefixes.

The offline service in ``hmm.py`` computes smoothed posteriors from the whole
sequence.  A live session must never use a future observation, so this module
only walks the forward variable from left to right:

    log α_1(s) = log π(s) + log B(s, o_1)
    log α_t(j) = log B(j, o_t) + logsumexp_i(log α_{t-1}(i) + log A(i, j))

The filtering posterior at position t is normalised α_t, and the cumulative
log-likelihood of the prefix is logsumexp_s α_t(s).  No backward pass and no
future token is involved.
"""
import numpy as np
from scipy.special import logsumexp

from .hmm import log_probabilities
from .parameters import normalize_parameters


def _log_alpha_json(vector):
    return [float(value) if np.isfinite(value) else None for value in vector]


def decode_log_alpha(values):
    """JSON uses null for exact -inf; restore it for the recursion."""
    return np.asarray([-np.inf if value is None else float(value) for value in values], dtype=float)


def _posterior(log_alpha):
    total = float(logsumexp(log_alpha))
    if not np.isfinite(total):
        return None, None
    with np.errstate(under='ignore'):
        posterior = np.exp(log_alpha - total)
    # Correct the tiny floating column-sum drift, exactly like the offline code.
    posterior = posterior / posterior.sum()
    return posterior.tolist(), int(np.argmax(posterior))


def filter_prefixes(observations, pi, transition, emission, start_position=0, start_log_alpha=None):
    """Compute every filtering prefix from ``start_position`` onward.

    ``start_position`` is the number of observations already folded into
    ``start_log_alpha`` (a checkpoint).  When it is zero the recursion starts
    at the initial distribution.  The returned records are append-only and
    each one contains the internal log-α vector needed to resume later.
    """
    normalized = normalize_parameters(pi, transition, emission)
    p = np.asarray(normalized['pi'])
    A = np.asarray(normalized['transition'])
    B = np.asarray(normalized['emission'])
    obs = np.asarray(observations)
    if obs.ndim != 1 or not 1 <= len(obs) <= 2000 or not np.issubdtype(obs.dtype, np.integer) or np.issubdtype(obs.dtype, np.bool_) or (obs < 0).any() or (obs >= B.shape[1]).any():
        raise ValueError('观测须1..2000个合法整数码索引')
    if not 0 <= start_position <= len(obs):
        raise ValueError('起始前缀位置超出观测范围')
    if start_position > 0 and start_log_alpha is None:
        raise ValueError('非零起始位置需要log α检查点')
    obs = obs.astype(int)
    lp = log_probabilities(p)
    lA = log_probabilities(A)
    lB = log_probabilities(B)
    previous = None if start_position == 0 else decode_log_alpha(start_log_alpha)
    if previous is not None and previous.shape != (len(p),):
        raise ValueError('log α检查点维度与π不匹配')
    records = []
    failure = None
    for index in range(start_position, len(obs)):
        position = index + 1
        if previous is None:
            current = lp + lB[:, obs[index]]
        else:
            current = logsumexp(previous[:, None] + lA, axis=0) + lB[:, obs[index]]
        if not np.isfinite(current).any():
            failure = {'position': position, 'token_index': int(obs[index])}
            break
        log_likelihood = float(logsumexp(current))
        posterior, marginal = _posterior(current)
        records.append({
            'position': position,
            'token_index': int(obs[index]),
            'cumulative_log_likelihood': log_likelihood,
            'posterior': posterior,
            'marginal_state': marginal,
            'log_alpha': _log_alpha_json(current),
        })
        previous = current
    return {
        'records': records,
        'failure': failure,
        'normalization': normalized['normalization'],
        'last_log_alpha': None if previous is None else _log_alpha_json(previous),
    }
