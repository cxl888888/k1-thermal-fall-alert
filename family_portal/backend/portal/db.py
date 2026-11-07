from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

REVISION = '0002_contact_phone_changes'


def now():
    """SQLite dates are UTC without an implicit local-time conversion."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def stamp(value):
    return value.isoformat(timespec='milliseconds') + 'Z' if value else None


def uid():
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = 'clients'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    label: Mapped[str] = mapped_column(String(80))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Pairing(Base):
    __tablename__ = 'pairings'
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(80))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)

class ContactPhoneChange(Base):
    __tablename__ = 'contact_phone_changes'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    requester_id: Mapped[str] = mapped_column(ForeignKey('clients.id'))
    phone: Mapped[str] = mapped_column(String(21))
    approvals: Mapped[list] = mapped_column(JSON, default=list)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)


class Credential(Base):
    __tablename__ = 'credentials'
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str | None] = mapped_column(ForeignKey('clients.id'), nullable=True)
    role: Mapped[str] = mapped_column(String(16))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Device(Base):
    __tablename__ = 'devices'
    __table_args__ = (UniqueConstraint('device_id', 'origin'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    device_id: Mapped[str] = mapped_column(String(100))
    origin: Mapped[str] = mapped_column(String(32))
    boot_id: Mapped[str] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    sensor_status: Mapped[dict] = mapped_column(JSON, default=dict)
    interaction_state: Mapped[str] = mapped_column(String(80), default='unknown')
    source_gap: Mapped[bool] = mapped_column(Boolean, default=False)


class Cursor(Base):
    __tablename__ = 'source_cursors'
    __table_args__ = (UniqueConstraint('device_id', 'origin', 'boot_id'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    device_id: Mapped[str] = mapped_column(String(100))
    origin: Mapped[str] = mapped_column(String(32))
    boot_id: Mapped[str] = mapped_column(String(100))
    sequence: Mapped[int] = mapped_column(Integer)
    state_version: Mapped[int] = mapped_column(Integer)


class Incident(Base):
    __tablename__ = 'incidents'
    __table_args__ = (UniqueConstraint('device_id', 'origin', 'source_incident_id'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    device_id: Mapped[str] = mapped_column(String(100))
    source_incident_id: Mapped[str] = mapped_column(String(100))
    origin: Mapped[str] = mapped_column(String(32))
    boot_id: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(200))
    route: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(80))
    red_latched: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    occurred_at: Mapped[str | None] = mapped_column(String(48), nullable=True)
    time_quality: Mapped[str] = mapped_column(String(40))
    received_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SourceEvent(Base):
    __tablename__ = 'source_events'
    __table_args__ = (UniqueConstraint('device_id', 'origin', 'boot_id', 'sequence'),
                      UniqueConstraint('device_id', 'origin', 'boot_id', 'source_event_id'))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(100))
    origin: Mapped[str] = mapped_column(String(32))
    boot_id: Mapped[str] = mapped_column(String(100))
    sequence: Mapped[int] = mapped_column(Integer)
    source_event_id: Mapped[str] = mapped_column(String(100))
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    incident_id: Mapped[str | None] = mapped_column(ForeignKey('incidents.id'), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Operation(Base):
    __tablename__ = 'operations'
    __table_args__ = (UniqueConstraint('client_id', 'request_id'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(ForeignKey('clients.id'))
    request_id: Mapped[str] = mapped_column(String(100))
    incident_id: Mapped[str] = mapped_column(ForeignKey('incidents.id'))
    state_version: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    digest: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Change(Base):
    __tablename__ = 'changes'
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(40))
    incident_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Database:
    def __init__(self, url):
        if not url.startswith('sqlite:'):
            raise ValueError('首版仅启用SQLite；其他方言需完成迁移及并发验证后开放')
        options = dict(connect_args={'check_same_thread': False, 'timeout': 10})
        if url in ('sqlite://', 'sqlite:///:memory:'):
            options['poolclass'] = StaticPool
        self.engine = create_engine(url, **options)
        self.lock = threading.RLock()

        @event.listens_for(self.engine, 'connect')
        def configure(connection, _record):
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=10000')
            if url not in ('sqlite://', 'sqlite:///:memory:'):
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('PRAGMA synchronous=FULL')

        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def initialize_fresh(self):
        # SECURITY: explicit initialization of an empty database only; never "upgrade"
        # an existing database with create_all. Later upgrades belong to Alembic.
        if inspect(self.engine).get_table_names():
            raise ValueError('数据库不是空库，拒绝自动重建；升级请运行Alembic并先备份')
        with self.engine.begin() as connection:
            Base.metadata.create_all(connection)
            connection.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY NOT NULL)'))
            connection.execute(text('INSERT INTO alembic_version (version_num) VALUES (:v)'), {'v': REVISION})

    def check_revision(self):
        with self.engine.connect() as connection:
            if 'alembic_version' not in inspect(connection).get_table_names():
                raise ValueError('数据库尚未初始化，请先运行 python -m portal.cli init-db')
            if connection.execute(text('SELECT version_num FROM alembic_version')).scalar() != REVISION:
                raise ValueError('数据库版本与代码不一致，拒绝自动迁移')
