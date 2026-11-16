import os
import tempfile
from pathlib import Path

import pytest

# Environment must be set before importing src.config, which builds the engine.
_TMP = tempfile.mkdtemp(prefix='yx-filter-tests-')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ['DATABASE_URL'] = f"sqlite:///{Path(_TMP) / 'test.db'}"
os.environ['SEED_DEMO'] = 'false'
os.environ['FILTER_INLINE_COMPUTE'] = 'true'
os.environ['ARTIFACT_DIR'] = str(Path(_TMP) / 'artifacts')

from src.config.database import Base, engine, SessionLocal  # noqa: E402
from src.models import ModelProfile, SequenceStudy, User  # noqa: E402
from src.services.parameters import normalize_parameters  # noqa: E402
from src.utils.security import hash_password  # noqa: E402

STUDY_SYMBOLS = ['A', 'B', 'C', 'D']
STATES = ['稳态假设', '缓变假设', '跃迁假设']


@pytest.fixture(scope='session', autouse=True)
def schema():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        session.add(User(email='analyst@test', name='测试分析员', role='analyst', password_hash=hash_password('Yanxi2026!')))
        session.commit()
    with SessionLocal() as session:
        yield session


@pytest.fixture()
def fixture_model(db):
    study = SequenceStudy(name='测试研究', filename='fixture.json', description='', symbols=STUDY_SYMBOLS)
    db.add(study)
    db.flush()
    params = normalize_parameters(
        [.6, .3, .1],
        [[.8, .15, .05], [.1, .8, .1], [.2, .2, .6]],
        [[.55, .35, .1, 0], [.1, .5, .3, .1], [.05, .15, .3, .5]],
    )
    profile = ModelProfile(study_id=study.id, name='测试固定模型', version='v1', states=STATES, symbols=STUDY_SYMBOLS, parameters=params, normalization_max_delta=params['max_normalization_delta'], created_by='analyst@test')
    db.add(profile)
    db.commit()
    return {'study_id': study.id, 'profile_id': profile.id, 'profile': profile, 'parameters': params}
