"""
Declarative base for all ORM models.

Keeping this in its own module (rather than defining it alongside the
first model) avoids circular imports: repository modules import `Base`
from here, and Alembic's migration environment imports `Base.metadata`
without needing to import the FastAPI app or service layer.
"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models in Mnemosyne."""
