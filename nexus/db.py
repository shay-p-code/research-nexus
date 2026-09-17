import time
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def uid():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


vector = Vector(1536).with_variant(JSON(), "sqlite")


class Inspection(Base):
    __tablename__ = "inspections"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    question: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    embedding: Mapped[list] = mapped_column(vector)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    inspection_id: Mapped[str] = mapped_column(ForeignKey("inspections.id"), unique=True)
    question: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    active_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    embedding: Mapped[list] = mapped_column(vector)
    focus: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(24), default="research")
    iteration: Mapped[int] = mapped_column(Integer, default=0)
    max_rounds: Mapped[int] = mapped_column(Integer)
    lease_hash: Mapped[str | None] = mapped_column(String(64))
    lease_expires: Mapped[float | None] = mapped_column(Float)
    executing: Mapped[bool] = mapped_column(default=False)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class ResearchRound(Base):
    __tablename__ = "research_rounds"
    __table_args__ = (UniqueConstraint("job_id", "iteration"),)
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    iteration: Mapped[int] = mapped_column(Integer)
    report: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSON)
    provider_response_id: Mapped[str] = mapped_column(Text)
    usage: Mapped[dict] = mapped_column(JSON)
    library_result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    first_seen: Mapped[float] = mapped_column(Float, default=time.time)


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    text: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(24))
    confidence: Mapped[str] = mapped_column(String(24))
    embedding: Mapped[list] = mapped_column(vector)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (UniqueConstraint("note_id", "source_id", "round_id"),)
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    round_id: Mapped[str] = mapped_column(ForeignKey("research_rounds.id"))


class Contradiction(Base):
    __tablename__ = "contradictions"
    __table_args__ = (UniqueConstraint("note_id", "other_id"),)
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id"), index=True)
    other_id: Mapped[str] = mapped_column(ForeignKey("notes.id"), index=True)


class AuthRecord(Base):
    __tablename__ = "auth_records"
    # Tokens/codes/tickets are stored only by SHA-256 hash.
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    data: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[float] = mapped_column(Float, index=True)


def database(url):
    engine = create_engine(url, pool_pre_ping=True, **(
        {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite") else {}
    ))
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def pragmas(conn, _):
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
    return engine, sessionmaker(engine, expire_on_commit=False)


def initialize(engine):
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("CREATE INDEX IF NOT EXISTS notes_text_idx ON notes USING gin(to_tsvector('english', text))"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS notes_vector_idx ON notes USING hnsw (embedding vector_cosine_ops)"))
