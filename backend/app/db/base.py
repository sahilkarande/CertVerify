"""Declarative base shared by all ORM models (models are added from Phase 2)."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
