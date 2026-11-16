from sqlalchemy.orm import Mapped,mapped_column
from sqlalchemy import ForeignKey,String,JSON,Text,DateTime,Integer,UniqueConstraint
from sqlalchemy.dialects.mysql import DOUBLE
from datetime import datetime,timezone
from src.config.database import Base

def now():return datetime.now(timezone.utc)

class FilterSession(Base):
 __tablename__='filter_sessions'
 id:Mapped[int]=mapped_column(primary_key=True)
 study_id:Mapped[int]=mapped_column(ForeignKey('sequence_studies.id'))
 profile_id:Mapped[int]=mapped_column(ForeignKey('model_profiles.id'))
 name:Mapped[str]=mapped_column(String(180),unique=True)
 profile_snapshot:Mapped[dict]=mapped_column(JSON)
 input_revision:Mapped[int]=mapped_column(Integer,default=0)
 position_count:Mapped[int]=mapped_column(Integer,default=0)
 compute_token:Mapped[str|None]=mapped_column(String(40),nullable=True)
 compute_state:Mapped[str]=mapped_column(String(24),default='idle')
 compute_revision:Mapped[int|None]=mapped_column(Integer,nullable=True)
 compute_enqueued_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
 compute_started_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
 computed_revision:Mapped[int|None]=mapped_column(Integer,nullable=True)
 computed_through:Mapped[int]=mapped_column(Integer,default=0)
 failure_position:Mapped[int|None]=mapped_column(Integer,nullable=True)
 failure_message:Mapped[str]=mapped_column(Text,default='')
 created_by:Mapped[str]=mapped_column(String(120))
 created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
 updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,onupdate=now)

class FilterObservation(Base):
 __tablename__='filter_observations'
 __table_args__=(UniqueConstraint('session_id','position'),)
 id:Mapped[int]=mapped_column(primary_key=True)
 session_id:Mapped[int]=mapped_column(ForeignKey('filter_sessions.id'))
 position:Mapped[int]=mapped_column(Integer)
 token:Mapped[str]=mapped_column(String(80))
 revision:Mapped[int]=mapped_column(Integer)
 updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,onupdate=now)

class FilterPrefixState(Base):
 """Saved forward state (log α) and filtering output for one prefix.

 A row is reusable as a resume point only while every observation in its
 prefix still carries the revision recorded here.  A correction bumps one
 observation revision, which makes that row and every later row stale.
 """
 __tablename__='filter_prefix_states'
 __table_args__=(UniqueConstraint('session_id','position'),)
 id:Mapped[int]=mapped_column(primary_key=True)
 session_id:Mapped[int]=mapped_column(ForeignKey('filter_sessions.id'))
 position:Mapped[int]=mapped_column(Integer)
 obs_revision:Mapped[int]=mapped_column(Integer)
 computed_revision:Mapped[int]=mapped_column(Integer)
 cumulative_log_likelihood:Mapped[float]=mapped_column(DOUBLE(asdecimal=False))
 posterior:Mapped[list]=mapped_column(JSON)
 marginal_state:Mapped[int]=mapped_column(Integer)
 log_alpha:Mapped[list]=mapped_column(JSON)
 updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now,onupdate=now)

class FilterSnapshot(Base):
 """One complete filtering result, keyed by the observation revision."""
 __tablename__='filter_snapshots'
 __table_args__=(UniqueConstraint('session_id','revision'),)
 id:Mapped[int]=mapped_column(primary_key=True)
 session_id:Mapped[int]=mapped_column(ForeignKey('filter_sessions.id'))
 revision:Mapped[int]=mapped_column(Integer)
 position_count:Mapped[int]=mapped_column(Integer)
 digest:Mapped[str]=mapped_column(String(64))
 payload:Mapped[dict]=mapped_column(JSON)
 created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=now)
