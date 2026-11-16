"""Numeric checks for the forward-only filtering recursion.

The reference is brute-force enumeration of every state path over the prefix
only, so a passing test proves no future observation leaked into the result.
"""
import itertools
import math

import numpy as np
import pytest

from src.services.filtering import filter_prefixes
from src.services.hmm import infer_sequence

PI = [.6, .3, .1]
A = [[.8, .15, .05], [.1, .8, .1], [.2, .2, .6]]
B = [[.55, .35, .1, 0], [.1, .5, .3, .1], [.05, .15, .3, .5]]
OBSERVATIONS = [0, 1, 3, 0, 2]


def enumerate_prefix(obs, pi, transition, emission):
    """Exact likelihood and filtering posterior by enumerating all paths."""
    total = 0.0
    terminal = [0.0] * len(pi)
    for path in itertools.product(range(len(pi)), repeat=len(obs)):
        weight = pi[path[0]] * emission[path[0]][obs[0]]
        for t in range(1, len(obs)):
            weight *= transition[path[t - 1]][path[t]] * emission[path[t]][obs[t]]
        total += weight
        terminal[path[-1]] += weight
    posterior = [value / total for value in terminal]
    return total, posterior


def test_each_prefix_matches_path_enumeration():
    result = filter_prefixes(OBSERVATIONS, PI, A, B)
    assert len(result['records']) == len(OBSERVATIONS)
    for record in result['records']:
        prefix = OBSERVATIONS[:record['position']]
        likelihood, posterior = enumerate_prefix(prefix, PI, A, B)
        assert record['cumulative_log_likelihood'] == pytest.approx(math.log(likelihood), abs=1e-12)
        assert np.asarray(record['posterior']) == pytest.approx(np.asarray(posterior), abs=1e-12)
        assert sum(record['posterior']) == pytest.approx(1.0, abs=1e-12)


def test_prefix_results_equal_offline_smoothing_at_the_prefix_end():
    result = filter_prefixes(OBSERVATIONS, PI, A, B)
    for record in result['records']:
        offline = infer_sequence(OBSERVATIONS[:record['position']], PI, A, B)
        assert record['cumulative_log_likelihood'] == pytest.approx(offline['log_likelihood'], abs=1e-12)
        # At its own end the filtering posterior equals the smoothed posterior.
        assert np.asarray(record['posterior']) == pytest.approx(np.asarray(offline['posterior'][-1]), abs=1e-12)


def test_future_observation_never_changes_an_earlier_prefix():
    short = filter_prefixes(OBSERVATIONS[:3], PI, A, B)['records']
    long = filter_prefixes(OBSERVATIONS, PI, A, B)['records']
    for before, after in zip(short, long):
        assert before['cumulative_log_likelihood'] == after['cumulative_log_likelihood']
        assert before['posterior'] == after['posterior']


def test_resume_from_checkpoint_is_identical_to_full_run():
    full = filter_prefixes(OBSERVATIONS, PI, A, B)['records']
    resumed = filter_prefixes(OBSERVATIONS, PI, A, B, start_position=2, start_log_alpha=full[1]['log_alpha'])['records']
    assert [record['position'] for record in resumed] == [3, 4, 5]
    for before, after in zip(full[2:], resumed):
        assert before['cumulative_log_likelihood'] == after['cumulative_log_likelihood']
        assert before['posterior'] == after['posterior']


def test_impossible_prefix_stops_without_partial_future():
    pi = [1.0, 0.0]
    transition = [[.7, .3], [.4, .6]]
    emission = [[0.0, 1.0], [1.0, 0.0]]
    result = filter_prefixes([0, 1], pi, transition, emission)
    assert result['failure'] == {'position': 1, 'token_index': 0}
    assert result['records'] == []


def test_lowercase_input_is_canonicalised_by_the_service_not_the_core():
    # The core works on integer indices; case folding happens in the service.
    result = filter_prefixes([0, 0], [.5, .5], [[.9, .1], [.1, .9]], [[.5, .5], [.5, .5]])
    assert result['records'][0]['posterior'] == pytest.approx([0.5, 0.5])
