import json
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

Base = declarative_base()

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def get_engine() -> Engine:
    """Lazily build the SQLAlchemy engine so importing the module never fails."""
    global _engine, _SessionLocal
    if _engine is None:
        if not settings.DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. Copy .env.example to .env and fill it in."
            )
        _engine = create_engine(
            settings.DATABASE_URL,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            # Disable psycopg3 server-side prepared statement caching.
            # Pooled connections reused by the worker can raise
            # "prepared statement "_pg3_N" already exists" otherwise.
            connect_args={"prepare_threshold": None},
        )
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    return _engine


def get_session_factory() -> sessionmaker:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_query(query: str, params: dict | None = None, *, fetch: str = "all"):
    """
    Run raw SQL on a short-lived connection.

    fetch: "all" -> list[Row], "one" -> Row | None, "scalar" -> first column of first row,
           "none" -> for INSERT/UPDATE/DELETE with no RETURNING (no result read).
    """
    # engine.begin() commits on success / rolls back on error. engine.connect()
    # would NOT commit in SQLAlchemy 2.0, silently rolling back every write.
    with get_engine().begin() as conn:
        result = conn.execute(text(query), params or {})
        if fetch == "all":
            return result.fetchall()
        if fetch == "one":
            return result.first()
        if fetch == "scalar":
            return result.scalar()
        if fetch == "none":
            return None
        raise ValueError(f"Unknown fetch mode: {fetch}")


def call_function(fn_name: str, params: dict | None = None):
    """
    Call a Postgres function that RETURNS jsonb (e.g. public.check_availability(...)).

    Returns the parsed jsonb as a Python object, or None for a NULL result.
    """
    args = params or {}
    placeholders = ", ".join(f":{k}" for k in args)
    scalar = run_query(
        f"SELECT public.{fn_name}({placeholders})::text", args, fetch="scalar"
    )
    return json.loads(scalar) if scalar else None


def get_db():
    """FastAPI dependency (kept for backwards compatibility)."""
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()

