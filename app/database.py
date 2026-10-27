from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.config import settings

# ----------------------------------------------------------------
# DATABASE ENGINE
# ----------------------------------------------------------------
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,  # Verify connections before using
    pool_size=5,
    max_overflow=10
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def libpq_url(url: str) -> str:
    """``url`` without a SQLAlchemy driver suffix, e.g. ``postgresql+psycopg2://`` -> ``postgresql://``.

    For the psycopg 3 connections outside SQLAlchemy (LISTEN, the worker role).
    """
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    return f"{scheme.split('+', 1)[0]}://{rest}"

# ----------------------------------------------------------------
# DEPENDENCY
# ----------------------------------------------------------------
def get_db():
    """Database session dependency"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
