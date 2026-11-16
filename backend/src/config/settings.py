import os
DATABASE_URL=os.getenv('DATABASE_URL','mysql+pymysql://hidden:hidden_demo@db:3306/hidden?charset=utf8mb4')
REDIS_URL=os.getenv('REDIS_URL','redis://redis:6379/0')
JWT_SECRET=os.environ['JWT_SECRET']
SEED_DEMO=os.getenv('SEED_DEMO','true').lower()=='true'
ARTIFACT_DIR=os.getenv('ARTIFACT_DIR','/artifacts')
RATE_LIMIT_WRITES=int(os.getenv('RATE_LIMIT_WRITES','60'))
FILTER_MAX_POSITIONS=int(os.getenv('FILTER_MAX_POSITIONS','2000'))
FILTER_CHECKPOINT_POSITIONS=int(os.getenv('FILTER_CHECKPOINT_POSITIONS','5'))
FILTER_LEASE_SECONDS=int(os.getenv('FILTER_LEASE_SECONDS','60'))
FILTER_INLINE_COMPUTE=os.getenv('FILTER_INLINE_COMPUTE','auto').lower()
