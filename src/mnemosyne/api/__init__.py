"""
API layer: FastAPI application, routers, Pydantic schemas, and
exception-to-HTTP mapping. This is the only layer that knows JSON is
involved — everything it calls (services, engine, repositories) works
with plain domain dataclasses.
"""
