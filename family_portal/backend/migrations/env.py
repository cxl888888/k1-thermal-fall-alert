from pathlib import Path
import os
from alembic import context
from sqlalchemy import create_engine
from portal.db import Base

url=os.environ.get('FAMILY_DATABASE_URL') or 'sqlite:///'+str(Path(__file__).resolve().parents[2]/'data'/'family.db').replace('\\','/')
if not url.startswith('sqlite:'):
    raise RuntimeError('首版仅启用SQLite迁移')
if context.is_offline_mode():
    context.configure(url=url,target_metadata=Base.metadata,literal_binds=True)
    with context.begin_transaction(): context.run_migrations()
else:
    with create_engine(url).connect() as connection:
        context.configure(connection=connection,target_metadata=Base.metadata,render_as_batch=True)
        with context.begin_transaction(): context.run_migrations()
