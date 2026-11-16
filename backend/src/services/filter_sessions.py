"""Live filtering sessions: observation prefixes, corrections and single-writer compute.

Invariants
----------
* Observations are stored by 1-based position.  Every append or correction bumps
  one session-wide ``input_revision`` and stamps only the touched rows.
* A saved prefix state (log α plus filtering output) is reusable only while
  ``max(revision of observations in that prefix) <= state.computed_revision``.
  Any later correction to that prefix raises the observation revision above the
  state's revision, so that prefix and every later one is recomputed; prefixes
  before the corrected position keep a valid chain and are not recomputed.
* A computation owns a random token.  Checkpoints and the final snapshot are
  written under a row lock only if the token and revision still match, so a slow
  old computation cannot publish over a newer observation revision.
* Progress is committed in batches.  A crash before or after any checkpoint
  leaves only complete, revision-tagged prefixes; the next computation resumes
  at the end of the longest still-valid chain.
"""
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from src.config.settings import FILTER_CHECKPOINT_POSITIONS, FILTER_INLINE_COMPUTE, FILTER_LEASE_SECONDS, FILTER_MAX_POSITIONS
from src.constants.enums import FilterComputeState
from src.models import FilterObservation, FilterPrefixState, FilterSession, FilterSnapshot, ModelProfile, SequenceStudy
from src.services.audit import record
from src.services.filtering import filter_prefixes
from src.utils.serialize import row

CHECKPOINT = max(1, FILTER_CHECKPOINT_POSITIONS)
MAX_POSITIONS = FILTER_MAX_POSITIONS
LEASE_SECONDS = FILTER_LEASE_SECONDS


def _utcnow():
    return datetime.now(timezone.utc)


def _seconds_since(value):
    """MySQL returns aware datetimes; SQLite returns naive ones treated as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (_utcnow() - value).total_seconds()


def _symbol_map(profile_snapshot):
    return {str(symbol).strip().lower(): str(symbol) for symbol in profile_snapshot['symbols']}


def validate_tokens(profile_snapshot, tokens):
    mapping = _symbol_map(profile_snapshot)
    canonical = []
    for offset, token in enumerate(tokens):
        if type(token) is not str:
            raise ValueError(f'第{offset + 1}个观测码须为字符串')
        found = mapping.get(token.strip().lower())
        if found is None:
            raise ValueError(f'第{offset + 1}个观测码“{token}”不在声明字母表 {"/".join(profile_snapshot["symbols"])} 中')
        canonical.append(found)
    return canonical


def _observations(db, session_id, through=None):
    query = select(FilterObservation).where(FilterObservation.session_id == session_id)
    if through is not None:
        query = query.where(FilterObservation.position <= through)
    return list(db.scalars(query.order_by(FilterObservation.position)))


def _valid_chain(db, session):
    """Longest contiguous run of reusable prefixes.

    Returns ``(length, log_alpha_or_None, first_invalid_position_or_None)``.
    Revisions are monotonic: a correction to position k stamps it with a
    revision greater than any prefix state computed before the correction, so
    every prefix containing k fails ``running_revision <= computed_revision``.
    """
    observations = _observations(db, session.id)
    revisions = {state.position: state.computed_revision for state in db.scalars(select(FilterPrefixState).where(FilterPrefixState.session_id == session.id))}
    running = 0
    valid_length = 0
    first_invalid = None
    for item in observations:
        running = max(running, item.revision)
        computed = revisions.get(item.position)
        if computed is None or computed < running:
            first_invalid = item.position
            break
        valid_length = item.position
    log_alpha = None
    if valid_length:
        log_alpha = db.scalar(select(FilterPrefixState.log_alpha).where(FilterPrefixState.session_id == session.id, FilterPrefixState.position == valid_length))
    return valid_length, log_alpha, first_invalid


def _lock(db, session_id):
    return db.scalar(select(FilterSession).where(FilterSession.id == session_id).with_for_update())


def _claim(db, session_id, token=None):
    session = _lock(db, session_id)
    if not session:
        db.commit()
        return None
    if token is not None and session.compute_token != token:
        db.commit()
        return {'outcome': 'superseded'}
    claimed_token = token or uuid4().hex
    session.compute_token = claimed_token
    session.compute_state = FilterComputeState.RUNNING.value
    session.compute_revision = session.input_revision
    session.compute_started_at = _utcnow()
    session.updated_at = _utcnow()
    claim = {
        'outcome': 'claimed',
        'session_id': session.id,
        'token': claimed_token,
        'revision': session.input_revision,
        'through': session.position_count,
        'profile': session.profile_snapshot,
        'study_id': session.study_id,
        'created_by': session.created_by,
    }
    db.commit()
    return claim


def _write_checkpoint(db, session_id, token, revision, records, observations):
    """Persist a batch only while the claim is still the newest revision."""
    session = _lock(db, session_id)
    if not session:
        db.commit()
        return False
    if session.compute_token != token or session.input_revision != revision:
        db.commit()
        record(db, 'worker', 'filter.compute.superseded', {'session_id': session_id, 'revision': revision, 'checkpoint_through': records[-1]['position'] if records else None})
        return False
    positions = [item['position'] for item in records]
    existing = {state.position: state for state in db.scalars(select(FilterPrefixState).where(FilterPrefixState.session_id == session_id, FilterPrefixState.position.in_(positions)))}
    running = 0
    revision_at = {}
    for item in observations:
        running = max(running, item.revision)
        revision_at[item.position] = running
    for item in records:
        state = existing.get(item['position']) or FilterPrefixState(session_id=session_id, position=item['position'])
        if item['position'] not in existing:
            db.add(state)
        state.obs_revision = revision_at.get(item['position'], revision)
        state.computed_revision = revision
        state.cumulative_log_likelihood = float(item['cumulative_log_likelihood'])
        state.posterior = item['posterior']
        state.marginal_state = int(item['marginal_state'])
        state.log_alpha = item['log_alpha']
        state.updated_at = _utcnow()
    session.computed_through = max(positions)
    session.updated_at = _utcnow()
    db.commit()
    return True


def _payload(session, observations, states, failure, resumed_from, reused):
    series = [{
        'position': state.position,
        'token': observations[state.position - 1].token,
        'cumulative_log_likelihood': state.cumulative_log_likelihood,
        'posterior': state.posterior,
        'marginal_state': state.marginal_state,
        'observation_revision': observations[state.position - 1].revision,
    } for state in states]
    payload = {
        'session_id': session.id,
        'revision': session.input_revision,
        'profile_name': session.profile_snapshot['name'],
        'profile_version': session.profile_snapshot['version'],
        'states': session.profile_snapshot['states'],
        'symbols': session.profile_snapshot['symbols'],
        'series': series,
        'computed_through': len(series),
        'observed_count': session.position_count,
        'blocked': bool(failure),
        'failure_position': failure['position'] if failure else None,
        'failure_message': failure['message'] if failure else '',
        'resumed_from': resumed_from,
        'reused_prefix_count': reused,
        'filtering_only': True,
        'note': '每个位置只用该位置及以前的观测；后验为滤波后验，未做后向平滑，不借用未来观测。',
    }
    digest_source = json.dumps({
        'profile_id': session.profile_id,
        'profile_revision': session.profile_snapshot,
        'revision': payload['revision'],
        'observed_count': payload['observed_count'],
        'blocked': payload['blocked'],
        'failure_position': payload['failure_position'],
        'series': [[item['position'], item['token'], item['cumulative_log_likelihood'], item['posterior']] for item in series],
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    payload['digest'] = hashlib.sha256(digest_source.encode()).hexdigest()
    return payload


def _finalize(db, session_id, token, revision, failure, observations, resumed_from, reused):
    session = _lock(db, session_id)
    if not session:
        db.commit()
        return {'outcome': 'missing'}
    if session.compute_token != token or session.input_revision != revision:
        db.commit()
        record(db, 'worker', 'filter.compute.superseded', {'session_id': session_id, 'revision': revision, 'stage': 'finalize'})
        return {'outcome': 'superseded'}
    saved = list(db.scalars(select(FilterPrefixState).where(FilterPrefixState.session_id == session_id).order_by(FilterPrefixState.position)))
    running = 0
    valid = []
    for state in saved:
        # Only an unbroken chain from position 1 may be published as one whole result.
        if state.position != len(valid) + 1 or state.position > session.position_count:
            break
        running = max(running, observations[state.position - 1].revision)
        if state.computed_revision < running:
            break
        valid.append(state)
    if failure:
        valid = [state for state in valid if state.position < failure['position']]
    if failure and not valid:
        failure = {'position': failure['position'], 'message': '第1码在当前模型下概率为0' if failure['position'] == 1 else failure['message']}
    payload = _payload(session, observations, valid, failure, resumed_from, reused)
    snapshot = db.scalar(select(FilterSnapshot).where(FilterSnapshot.session_id == session_id, FilterSnapshot.revision == revision))
    if snapshot is None:
        snapshot = FilterSnapshot(session_id=session_id, revision=revision)
        db.add(snapshot)
    snapshot.position_count = len(valid)
    snapshot.digest = payload['digest']
    snapshot.payload = payload
    snapshot.created_at = _utcnow()
    session.computed_revision = revision
    session.computed_through = len(valid)
    session.failure_position = failure['position'] if failure else None
    session.failure_message = failure['message'] if failure else ''
    session.compute_state = FilterComputeState.IDLE.value
    session.compute_token = None
    session.compute_started_at = None
    session.updated_at = _utcnow()
    db.commit()
    record(db, 'worker', 'filter.compute.finalized', {'session_id': session_id, 'revision': revision, 'through': len(valid), 'resumed_from': resumed_from, 'reused_prefix_count': reused, 'blocked': bool(failure)})
    return {'outcome': 'finalized', 'revision': revision, 'through': len(valid), 'payload': payload}


def compute_session(db, session_id, token=None):
    claimed = _claim(db, session_id, token)
    if claimed is None:
        return {'outcome': 'missing'}
    if claimed['outcome'] != 'claimed':
        return claimed
    token = claimed['token']
    revision = claimed['revision']
    session = db.get(FilterSession, session_id)
    profile = claimed['profile']
    observations = _observations(db, session_id, session.position_count)
    db.commit()
    try:
        tokens = validate_tokens(profile, [item.token for item in observations])
    except ValueError as error:
        return _finalize(db, session_id, token, revision, {'position': 1, 'message': str(error)[:1000]}, observations, 0, 0)
    start, log_alpha, _ = _valid_chain(db, session)
    db.commit()
    indices = [_index_of(profile, token) for token in tokens]
    parameters = profile['parameters']
    try:
        computed = filter_prefixes(indices, parameters['pi'], parameters['transition'], parameters['emission'], start_position=start, start_log_alpha=log_alpha)
    except ValueError as error:
        return _finalize(db, session_id, token, revision, {'position': start + 1, 'message': str(error)[:1000]}, observations, start, start)
    failure = None
    if computed['failure']:
        failure = {'position': computed['failure']['position'], 'message': f'不可能序列：到第{computed["failure"]["position"]}码所有路径概率为0'}
    for offset in range(0, len(computed['records']), CHECKPOINT):
        if not _write_checkpoint(db, session_id, token, revision, computed['records'][offset:offset + CHECKPOINT], observations):
            return {'outcome': 'superseded'}
    return _finalize(db, session_id, token, revision, failure, observations, start, start)


def _index_of(profile, token):
    for index, symbol in enumerate(profile['symbols']):
        if str(symbol) == token:
            return index
    raise ValueError('观测码不在模型字母表中')


def safe_compute(db, session_id, token=None):
    """Run a computation and always release the lease, even on unexpected errors."""
    try:
        return compute_session(db, session_id, token)
    except Exception as error:
        db.rollback()
        session = _lock(db, session_id)
        if session and (token is None or session.compute_token == token):
            session.compute_state = FilterComputeState.IDLE.value
            session.compute_token = None
            session.compute_started_at = None
            session.updated_at = _utcnow()
            db.commit()
        record(db, 'worker', 'filter.compute.error', {'session_id': session_id, 'error': str(error)[:1000]})
        return {'outcome': 'error', 'error': str(error)[:1000]}


def _queue(session_id, token):
    try:
        from src.worker import filter_session
        filter_session.delay(session_id, token)
        return True, ''
    except Exception as error:
        return False, str(error)[:300]


def _dispatch(db, session_id, token, actor):
    """Queue the compute task; fall back to inline compute when allowed.

    FILTER_INLINE_COMPUTE=true computes inside the HTTP request (used when no
    broker is present), false only enqueues, auto (default) enqueues and falls
    back to inline if the broker rejects the task.
    """
    mode = str(FILTER_INLINE_COMPUTE).strip().lower()
    queued, error = False, ''
    if mode not in ('true', '1', 'yes', 'inline'):
        queued, error = _queue(session_id, token)
    if queued:
        return {'queued': True, 'inline': False, 'outcome': 'queued'}
    if mode == 'false' or mode in ('0', 'no', 'queue'):
        record(db, actor, 'filter.queue_unavailable', {'session_id': session_id, 'error': error})
        return {'queued': False, 'inline': False, 'outcome': 'queue_unavailable'}
    if error:
        record(db, actor, 'filter.inline_compute', {'session_id': session_id, 'reason': error})
    outcome = safe_compute(db, session_id, token)
    return {'queued': False, 'inline': True, 'outcome': outcome.get('outcome')}


def _bump(session, revision):
    session.input_revision = revision
    session.compute_token = uuid4().hex
    session.compute_state = FilterComputeState.QUEUED.value
    session.compute_revision = revision
    session.compute_enqueued_at = _utcnow()
    session.updated_at = _utcnow()
    return session.compute_token


def create_session(db, data, actor):
    study = db.get(SequenceStudy, data.study_id)
    if not study:
        raise HTTPException(404, '研究不存在')
    profile = db.get(ModelProfile, data.profile_id)
    if not profile or profile.study_id != study.id:
        raise HTTPException(422, '模型版本不属于该研究')
    if db.scalar(select(FilterSession).where(FilterSession.name == data.name)):
        raise HTTPException(409, '实时滤波会话名已存在')
    profile_snapshot = row(profile)
    try:
        canonical = validate_tokens(profile_snapshot, data.initial_tokens)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    if len(canonical) > MAX_POSITIONS:
        raise HTTPException(422, f'会话最多{MAX_POSITIONS}个观测码')
    session = FilterSession(study_id=study.id, profile_id=profile.id, name=data.name, profile_snapshot=profile_snapshot, input_revision=1 if canonical else 0, position_count=len(canonical), compute_state=FilterComputeState.IDLE.value, computed_through=0, created_by=actor)
    db.add(session)
    db.flush()
    for position, token in enumerate(canonical, start=1):
        db.add(FilterObservation(session_id=session.id, position=position, token=token, revision=session.input_revision))
    db.commit()
    record(db, actor, 'filter.created', {'session_id': session.id, 'profile_id': profile.id, 'tokens': len(canonical)})
    dispatch = {'queued': False, 'inline': False, 'outcome': 'empty'}
    if canonical:
        token = _bump(session, session.input_revision)
        db.commit()
        dispatch = _dispatch(db, session.id, token, actor)
    return {'session_id': session.id, 'revision': session.input_revision, **dispatch}


def append_tokens(db, session_id, data, actor):
    session = _lock(db, session_id)
    if not session:
        db.commit()
        raise HTTPException(404, '实时滤波会话不存在')
    if data.expected_revision != session.input_revision:
        current = session.input_revision
        db.commit()
        raise HTTPException(409, f'观测修订已变化（当前为第{current}修订），请刷新后重新追加')
    try:
        canonical = validate_tokens(session.profile_snapshot, data.tokens)
    except ValueError as error:
        db.commit()
        raise HTTPException(422, str(error)) from error
    if session.position_count + len(canonical) > MAX_POSITIONS:
        db.commit()
        raise HTTPException(422, f'会话最多{MAX_POSITIONS}个观测码')
    revision = session.input_revision + 1
    for offset, token in enumerate(canonical):
        db.add(FilterObservation(session_id=session.id, position=session.position_count + offset + 1, token=token, revision=revision))
    session.position_count += len(canonical)
    token = _bump(session, revision)
    db.commit()
    record(db, actor, 'filter.appended', {'session_id': session.id, 'revision': revision, 'tokens': len(canonical), 'position_count': session.position_count})
    dispatch = _dispatch(db, session.id, token, actor)
    return {'session_id': session.id, 'revision': revision, 'queued': dispatch['queued'], 'inline': dispatch['inline'], 'outcome': dispatch['outcome']}


def correct_positions(db, session_id, data, actor):
    session = _lock(db, session_id)
    if not session:
        db.commit()
        raise HTTPException(404, '实时滤波会话不存在')
    if data.expected_revision != session.input_revision:
        current = session.input_revision
        db.commit()
        raise HTTPException(409, f'观测修订已变化（当前为第{current}修订），请刷新后重新更正')
    rows = {item.position: item for item in _observations(db, session.id)}
    for correction in data.corrections:
        if correction.position > session.position_count:
            db.commit()
            raise HTTPException(422, f'第{correction.position}位尚未接收，不能更正')
    try:
        canonical = validate_tokens(session.profile_snapshot, [item.token for item in data.corrections])
    except ValueError as error:
        db.commit()
        raise HTTPException(422, str(error)) from error
    revision = session.input_revision + 1
    changed = 0
    for correction, token in zip(data.corrections, canonical):
        item = rows[correction.position]
        if item.token == token:
            continue
        item.token = token
        item.revision = revision
        item.updated_at = _utcnow()
        changed += 1
    if not changed:
        db.commit()
        raise HTTPException(422, '更正内容与当前观测相同，未改变任何位置')
    token = _bump(session, revision)
    db.commit()
    first = min(correction.position for correction in data.corrections)
    record(db, actor, 'filter.corrected', {'session_id': session.id, 'revision': revision, 'first_position': first, 'positions': [correction.position for correction in data.corrections]})
    dispatch = _dispatch(db, session.id, token, actor)
    return {'session_id': session.id, 'revision': revision, 'invalidated_from': first, 'queued': dispatch['queued'], 'inline': dispatch['inline'], 'outcome': dispatch['outcome']}


def sessions(db):
    summaries = []
    for session in db.scalars(select(FilterSession).order_by(FilterSession.id.desc())):
        latest = db.scalar(select(FilterSnapshot).where(FilterSnapshot.session_id == session.id).order_by(FilterSnapshot.revision.desc()))
        summaries.append({**row(session), 'profile_name': session.profile_snapshot['name'], 'profile_version': session.profile_snapshot['version'], 'result_revision': latest.revision if latest else None, 'stale': latest is None or latest.revision != session.input_revision, 'computed_through': latest.position_count if latest else 0, 'compute_token': None})
    return summaries


def request_compute(db, session_id, actor, force=False):
    session = _lock(db, session_id)
    if not session:
        db.commit()
        raise HTTPException(404, '实时滤波会话不存在')
    if not session.position_count:
        db.commit()
        raise HTTPException(422, '会话还没有观测码，先追加一批')
    stamp = session.compute_started_at or session.compute_enqueued_at
    active = session.compute_state != FilterComputeState.IDLE.value
    fresh = not force and active and session.compute_revision == session.input_revision and stamp is not None and _seconds_since(stamp) < LEASE_SECONDS
    if fresh:
        db.commit()
        return {'session_id': session_id, 'queued': True, 'inline': False, 'outcome': 'already'}
    # A missing or overdue computation (crash, lost task) is re-claimed here.
    token = _bump(session, session.input_revision)
    db.commit()
    record(db, actor, 'filter.compute.requested', {'session_id': session_id, 'revision': session.input_revision, 'force': bool(force)})
    dispatch = _dispatch(db, session_id, token, actor)
    return {'session_id': session_id, 'revision': session.input_revision, 'queued': dispatch['queued'], 'inline': dispatch['inline'], 'outcome': dispatch['outcome']}


def session_detail(db, session_id):
    session = db.get(FilterSession, session_id)
    if not session:
        raise HTTPException(404, '实时滤波会话不存在')
    observations = _observations(db, session.id)
    latest = db.scalar(select(FilterSnapshot).where(FilterSnapshot.session_id == session.id).order_by(FilterSnapshot.revision.desc()))
    valid_length, _, invalidated_from = _valid_chain(db, session)
    db.commit()
    stale = latest is None or latest.revision != session.input_revision
    appended = corrected = 0
    if latest is not None:
        for item in observations:
            if item.revision > latest.revision:
                if item.position > latest.position_count:
                    appended += 1
                else:
                    corrected += 1
    stamp = session.compute_started_at or session.compute_enqueued_at
    active = session.compute_state != FilterComputeState.IDLE.value
    overdue = active and stamp is not None and _seconds_since(stamp) >= LEASE_SECONDS
    fresh_claim = active and session.compute_revision == session.input_revision and not overdue and stamp is not None
    detail = {
        **row(session),
        'observations': [{'position': item.position, 'token': item.token, 'revision': item.revision} for item in observations],
        'observed_count': session.position_count,
        'revision': session.input_revision,
        'result': latest.payload if latest else None,
        'result_revision': latest.revision if latest else None,
        'stale': stale,
        'staleness': {'appended_positions': appended, 'corrected_positions': corrected},
        'valid_prefix_count': valid_length,
        'invalidated_from': invalidated_from,
        'compute_state': session.compute_state,
        'compute_revision': session.compute_revision,
        'compute_overdue': overdue,
        'compute_active': bool(fresh_claim and stale),
    }
    detail.pop('profile_snapshot', None)
    detail.pop('compute_token', None)
    return detail
