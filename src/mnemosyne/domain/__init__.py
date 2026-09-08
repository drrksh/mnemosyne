"""
Domain layer: entities, value objects, enums, DTOs, and exceptions.

Nothing in this package imports SQLAlchemy, FastAPI, or Pydantic. This
is deliberate — the domain layer describes what a "memory" IS to
Mnemosyne, independent of how it is stored or transported. Every other
layer depends on this one; this one depends on nothing else in the
project.
"""
