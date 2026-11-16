"""Session-level behaviour: invalidation, late results, crashes, API contract."""
import itertools
import math

import pytest

from src.schemas.filter import AppendInput, CorrectionInput, PositionCorrection, SessionInput
from src.services import filter_sessions as service
from src.services.parameters import normalize_parameters

PI = [.6, .3, .1]
A = [[.8, .15, .05], [.1, .8, .1], [.2, .2, .6]]
B = [[.55, .35, .1, 0], [.1, .5, .3, .1], [.05, .15, .3, .5]]
SYMBOLS = ['A', 'B', 'C', 'D']
TOKENS = ['A', 'B', 'D', 'A', 'C']


def brute(prefix_tokens, pi=PI, transition=A, emission=B, symbols=SYMBOLS):
    obs = [symbols.index(token) for token in prefix_tokens]
    total = 0.0
    terminal = [0.0] * len(pi)
    for path in itertools.product(range(len(pi)), repeat=len(obs)):
        weight = pi[path[0]] * emission[path[0]][obs[0]]
        for t in range(1, len(obs)):
            weight *= transition[path[t - 1]][path[t]] * emission[path[t]][obs[t]]
        total += weight
        terminal[path[-1]] += weight
    return total, [value / total for value in terminal]


def new_session(db, model, tokens, name='会话'):
    created = service.create_session(db, SessionInput(study_id=model['study_id'], profile_id=model['profile_id'], name=name, initial_tokens=tokens), 'analyst@test')
    return created['session_id']


def states(db, session_id):
    return {item.position: item for item in session_id}


def detail(db, session_id):
    return service.session_detail(db, session_id)


def assert_matches_brute(series, tokens):
    for item in series:
        likelihood, posterior = brute(tokens[:item['position']])
        assert item['cumulative_log_likelihood'] == pytest.approx(math.log(likelihood), abs=1e-12)
        assert item['posterior'] == pytest.approx(posterior, abs=1e-12)


def test_append_only_never_recomputes_saved_prefixes(db, fixture_model):
    session_id = new_session(db, fixture_model, TOKENS[:2])
    first = {state.position: state.computed_revision for state in db.query(service.FilterPrefixState).filter_by(session_id=session_id)}
    service.append_tokens(db, session_id, AppendInput(tokens=TOKENS[2:4], expected_revision=1), 'analyst@test')
    service.append_tokens(db, session_id, AppendInput(tokens=TOKENS[4:], expected_revision=2), 'analyst@test')
    result = detail(db, session_id)['result']
    assert result['revision'] == 3
    assert result['reused_prefix_count'] == 4
    assert result['resumed_from'] == 4
    assert result['series'][0]['observation_revision'] == 1
    assert result['series'][1]['observation_revision'] == 1
    assert result['series'][2]['observation_revision'] == 2
    assert result['series'][4]['observation_revision'] == 3
    assert_matches_brute(result['series'], TOKENS)
    after = {state.position: state for state in db.query(service.FilterPrefixState).filter_by(session_id=session_id)}
    assert after[1].computed_revision == first[1]
    assert after[2].computed_revision == first[2]


def test_correction_invalidates_only_that_position_and_later(db, fixture_model):
    session_id = new_session(db, fixture_model, TOKENS)
    untouched = {position: (state.updated_at, state.cumulative_log_likelihood) for position, state in enumerate(db.query(service.FilterPrefixState).filter_by(session_id=session_id).order_by('position'), start=1)}
    response = service.correct_positions(db, session_id, CorrectionInput(expected_revision=1, corrections=[PositionCorrection(position=3, token='B')]), 'analyst@test')
    assert response['invalidated_from'] == 3
    result = detail(db, session_id)['result']
    corrected_tokens = TOKENS[:2] + ['B'] + TOKENS[3:]
    assert_matches_brute(result['series'], corrected_tokens)
    assert result['reused_prefix_count'] == 2
    assert result['resumed_from'] == 2
    fresh = {state.position: state for state in db.query(service.FilterPrefixState).filter_by(session_id=session_id)}
    for position in (1, 2):
        assert fresh[position].updated_at == untouched[position][0]
        assert fresh[position].cumulative_log_likelihood == untouched[position][1]
    for position in (3, 4, 5):
        assert fresh[position].computed_revision == 2


def test_same_value_correction_is_rejected_without_bumping_revision(db, fixture_model):
    session_id = new_session(db, fixture_model, TOKENS)
    with pytest.raises(service.HTTPException) as error:
        service.correct_positions(db, session_id, CorrectionInput(expected_revision=1, corrections=[PositionCorrection(position=2, token='B')]), 'analyst@test')
    assert error.value.status_code == 422
    assert detail(db, session_id)['revision'] == 1


def test_correction_beyond_received_positions_is_rejected(db, fixture_model):
    session_id = new_session(db, fixture_model, TOKENS[:3])
    with pytest.raises(service.HTTPException) as error:
        service.correct_positions(db, session_id, CorrectionInput(expected_revision=1, corrections=[PositionCorrection(position=4, token='A')]), 'analyst@test')
    assert error.value.status_code == 422
    assert detail(db, session_id)['revision'] == 1


def test_stale_expected_revision_conflicts(db, fixture_model):
    session_id = new_session(db, fixture_model, TOKENS[:2])
    service.append_tokens(db, session_id, AppendInput(tokens=['C'], expected_revision=1), 'analyst@test')
    with pytest.raises(service.HTTPException) as error:
        service.append_tokens(db, session_id, AppendInput(tokens=['A'], expected_revision=1), 'analyst@test')
    assert error.value.status_code == 409


def test_late_computation_cannot_overwrite_newer_revision(db, fixture_model):
    session_id = new_session(db, fixture_model, TOKENS)
    claim = service._claim(db, session_id)  # slow old computation starts at revision 1
    service.correct_positions(db, session_id, CorrectionInput(expected_revision=1, corrections=[PositionCorrection(position=1, token='D')]), 'analyst@test')
    stale = service.compute_session(db, session_id, claim['token'])
    assert stale['outcome'] == 'superseded'
    current = detail(db, session_id)
    assert current['result']['revision'] == 2
    assert current['result']['series'][0]['token'] == 'D'
    assert_matches_brute(current['result']['series'], ['D'] + TOKENS[1:])


def test_checkpoint_write_from_stale_token_is_refused(db, fixture_model):
    session_id = new_session(db, fixture_model, TOKENS[:2])
    claim = service._claim(db, session_id)
    observations = service._observations(db, session_id)
    service.correct_positions(db, session_id, CorrectionInput(expected_revision=1, corrections=[PositionCorrection(position=1, token='C')]), 'analyst@test')
    written = service._write_checkpoint(db, session_id, claim['token'], 1, [{'position': 1, 'cumulative_log_likelihood': -1.0, 'posterior': [0.5, 0.3, 0.2], 'marginal_state': 0, 'log_alpha': [-1.0, -2.0, -3.0]}], observations)
    assert written is False
    assert detail(db, session_id)['result']['series'][0]['token'] == 'C'


def _interrupt_after(monkeypatch, kill_at):
    """Simulate a crash after the given number of checkpoint writes."""
    original = service._write_checkpoint
    calls = {'count': 0}

    def wrapped(*args, **kwargs):
        calls['count'] += 1
        if calls['count'] > kill_at:
            raise _SimulatedCrash()
        return original(*args, **kwargs)

    monkeypatch.setattr(service, '_write_checkpoint', wrapped)
    return calls


class _SimulatedCrash(RuntimeError):
    pass


def test_crash_before_any_checkpoint_resumes_from_scratch(db, monkeypatch, fixture_model):
    session_id = new_session(db, fixture_model, TOKENS[:2])
    service.append_tokens(db, session_id, AppendInput(tokens=TOKENS[2:4], expected_revision=1), 'analyst@test')
    monkeypatch.setattr(service, 'CHECKPOINT', 2)
    calls = _interrupt_after(monkeypatch, 0)
    try:
        service.correct_positions(db, session_id, CorrectionInput(expected_revision=2, corrections=[PositionCorrection(position=1, token='C')]), 'analyst@test')
    except _SimulatedCrash:
        db.rollback()
    monkeypatch.undo()
    assert calls['count'] == 1
    # The correction is committed, but no prefix state for revision 3 exists.
    assert detail(db, session_id)['revision'] == 3
    assert detail(db, session_id)['result']['revision'] == 2
    assert detail(db, session_id)['stale'] is True
    resumed = service.request_compute(db, session_id, 'analyst@test', force=True)
    assert resumed['outcome'] == 'finalized'
    result = detail(db, session_id)['result']
    assert result['revision'] == 3
    assert result['resumed_from'] == 0
    assert_matches_brute(result['series'], ['C'] + TOKENS[1:4])


def test_crash_after_one_checkpoint_resumes_from_saved_prefix(db, monkeypatch, fixture_model):
    session_id = new_session(db, fixture_model, TOKENS[:2])
    service.append_tokens(db, session_id, AppendInput(tokens=TOKENS[2:], expected_revision=1), 'analyst@test')
    monkeypatch.setattr(service, 'CHECKPOINT', 2)
    calls = _interrupt_after(monkeypatch, 1)
    try:
        service.correct_positions(db, session_id, CorrectionInput(expected_revision=2, corrections=[PositionCorrection(position=3, token='B')]), 'analyst@test')
    except _SimulatedCrash:
        db.rollback()
    monkeypatch.undo()
    assert calls['count'] == 2
    assert detail(db, session_id)['stale'] is True
    # Checkpoint 1 for revision 3 covered positions 3-4 because prefixes 1-2 were
    # still valid; the whole chain is therefore reusable without recomputation.
    assert detail(db, session_id)['valid_prefix_count'] == 4
    resumed = service.request_compute(db, session_id, 'analyst@test', force=True)
    assert resumed['outcome'] == 'finalized'
    result = detail(db, session_id)['result']
    assert result['resumed_from'] == 4
    assert result['reused_prefix_count'] == 4
    assert_matches_brute(result['series'], TOKENS[:2] + ['B'] + TOKENS[3:])


def test_session_detail_reports_staleness_without_showing_partial_results(db, fixture_model):
    session_id = new_session(db, fixture_model, TOKENS[:3])
    before = detail(db, session_id)
    assert before['stale'] is False
    assert len(before['result']['series']) == 3
    # Simulate an append whose compute has not finished yet: the stored snapshot
    # is preserved and flagged stale, never replaced by a partial series.
    db.get(service.FilterSession, session_id)
    service._bump(db.get(service.FilterSession, session_id), 2)
    session = db.get(service.FilterSession, session_id)
    session.compute_state = 'queued'
    from src.models import FilterObservation
    db.add(FilterObservation(session_id=session_id, position=4, token='A', revision=2))
    session.position_count = 4
    db.commit()
    stale = detail(db, session_id)
    assert stale['stale'] is True
    assert stale['result']['revision'] == 1
    assert stale['result']['observed_count'] == 3
    assert stale['staleness'] == {'appended_positions': 1, 'corrected_positions': 0}


def test_empty_session_can_start_with_first_append(db, fixture_model):
    session_id = new_session(db, fixture_model, [])
    assert detail(db, session_id)['result'] is None
    service.append_tokens(db, session_id, AppendInput(tokens=TOKENS, expected_revision=0), 'analyst@test')
    result = detail(db, session_id)['result']
    assert result['revision'] == 1
    assert_matches_brute(result['series'], TOKENS)


def test_unknown_symbol_is_rejected_at_ingest(db, fixture_model):
    session_id = new_session(db, fixture_model, TOKENS[:2])
    with pytest.raises(service.HTTPException) as error:
        service.append_tokens(db, session_id, AppendInput(tokens=['Z'], expected_revision=1), 'analyst@test')
    assert error.value.status_code == 422


def test_impossible_sequence_blocks_but_keeps_earlier_prefixes(db, fixture_model):
    from src.models import SequenceStudy
    study = SequenceStudy(name='受限', filename='restricted.json', symbols=['X', 'Y', 'Z', 'W'])
    db.add(study)
    db.flush()
    params = normalize_parameters([1.0, 0.0], [[1.0, 0.0], [0.0, 1.0]], [[.5, .5, 0, 0], [0, 0, .5, .5]])
    from src.models import ModelProfile
    profile = ModelProfile(study_id=study.id, name='受限模型', version='v1', states=['甲', '乙'], symbols=['X', 'Y', 'Z', 'W'], parameters=params, normalization_max_delta=0, created_by='analyst@test')
    db.add(profile)
    db.commit()
    created = service.create_session(db, SessionInput(study_id=study.id, profile_id=profile.id, name='受限会话', initial_tokens=['X', 'Y']), 'analyst@test')
    session_id = created['session_id']
    first = detail(db, session_id)['result']
    assert first['blocked'] is False and len(first['series']) == 2
    # The restricted model cannot emit W in either initial state, so the prefix
    # becomes impossible exactly at position 3; earlier prefixes stay visible.
    service.append_tokens(db, session_id, AppendInput(tokens=['W'], expected_revision=1), 'analyst@test')
    blocked = detail(db, session_id)['result']
    assert blocked['blocked'] is True
    assert blocked['failure_position'] == 3
    assert [item['position'] for item in blocked['series']] == [1, 2]
    assert blocked['observed_count'] == 3


def test_correction_of_revised_position_while_another_compute_runs(db, fixture_model):
    """A correction during compute invalidates from that position; earlier prefixes stay."""
    session_id = new_session(db, fixture_model, TOKENS)
    claim = service._claim(db, session_id)  # old computation owns revision 1
    service.append_tokens(db, session_id, AppendInput(tokens=['A', 'D'], expected_revision=1), 'analyst@test')
    # The inline append computation (revision 2) finished; the old token is stale.
    assert service.compute_session(db, session_id, claim['token'])['outcome'] == 'superseded'
    service.correct_positions(db, session_id, CorrectionInput(expected_revision=2, corrections=[PositionCorrection(position=6, token='C')]), 'analyst@test')
    result = detail(db, session_id)['result']
    assert result['revision'] == 3
    assert result['reused_prefix_count'] == 5
    assert_matches_brute(result['series'], TOKENS + ['C', 'D'])


def test_profile_is_frozen_into_the_session(db, fixture_model):
    from src.models import ModelProfile
    session_id = new_session(db, fixture_model, TOKENS)
    profile = db.get(ModelProfile, fixture_model['profile_id'])
    profile.name = '改名无效'
    db.commit()
    assert detail(db, session_id)['result']['profile_name'] == '测试固定模型'


def test_api_round_trip_requires_auth_and_writers(db):
    """Wire-level smoke test with rate limiting bypassed (no Redis in tests)."""
    from fastapi.testclient import TestClient
    import src.main as main
    from src.middlewares.rate_limit import RateLimitMiddleware

    original = RateLimitMiddleware.dispatch
    RateLimitMiddleware.dispatch = lambda self, request, call_next: call_next(request)
    try:
        client = TestClient(main.app)
        assert client.get('/filter-sessions').status_code == 401
        login = client.post('/auth/login', json={'email': 'nobody@test', 'password': 'x'})
        assert login.status_code == 401
        token = client.post('/auth/login', json={'email': 'analyst@test', 'password': 'Yanxi2026!'})
        assert token.status_code == 200
        headers = {'Authorization': 'Bearer ' + token.json()['token']}
        unauthorized = client.post('/filter-sessions', json={'study_id': 1, 'profile_id': 1, 'name': '无权', 'initial_tokens': []})
        assert unauthorized.status_code == 401
        listed = client.get('/filter-sessions', headers=headers)
        assert listed.status_code == 200
    finally:
        RateLimitMiddleware.dispatch = original
