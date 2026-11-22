"""Pydantic request and response schemas for the control-plane HTTP API.

One module per domain (`auth.py`, `keys.py`, `settings.py`). Routers map repository and service
results into these schemas explicitly; SQLAlchemy ORM models never reach a response (ADR 0005).
"""
