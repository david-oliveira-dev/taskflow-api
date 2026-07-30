# 0001 — FastAPI, SQLAlchemy 2.0 async, and PyJWT

Status: accepted · 2026-07-30

## Context

The service needs an HTTP layer, a way to talk to PostgreSQL, and a way to sign access
tokens. Every choice here is load-bearing for the rest of the project: the async model, in
particular, has to be consistent from the router down to the driver, because one blocking
call in an async path stalls the whole event loop.

## Decision

**FastAPI + uvicorn.** Native async, type hints drive both validation and the OpenAPI
document, and Pydantic v2 is already required by the portfolio standard. The generated
OpenAPI is not a nice-to-have here — it is how a reviewer explores the API without reading
the code.

**SQLAlchemy 2.0 in async style, with asyncpg, plus Alembic.** The 2.0 typed API works with
mypy in strict mode, which a looser data-access layer would not. Alembic covers the part
that separates a real service from a demo: versioned migrations that apply *and* revert.

**PyJWT**, not python-jose. python-jose has been effectively unmaintained for years and
pulls in a second cryptography stack we would never use. PyJWT does exactly one thing.

**Python 3.12, single version.** This is a service, not a library: nothing consumes it as a
dependency, so a version matrix would cost CI time and buy nothing.

## Alternatives considered

**Django + DRF.** More batteries, a real admin, and a mature auth system. Rejected because
the async story is still partial and because the batteries would hide precisely the work
this project exists to demonstrate — writing the audit trail, the idempotency layer and the
rate limiter by hand.

**Flask.** Smaller, but synchronous by default, with no typed request validation and no
OpenAPI without extensions.

**Litestar.** Genuinely good and closer to FastAPI than most alternatives. Rejected on
ecosystem size alone: for a portfolio piece, the reviewer's familiarity is a feature.

**Raw asyncpg without an ORM.** Faster and more explicit. Rejected because migrations,
relationships and the audit trail would all become hand-rolled, and none of that is the
point of the project.

## Consequences

- Everything from router to driver is async; a synchronous database call anywhere is a bug,
  and `ASYNC` lint rules are enabled to catch the common cases.
- Strict mypy is viable across the data layer, which would be painful with SQLAlchemy 1.x.
- Signing is symmetric (HS256). If the API were ever split across services that must verify
  tokens without being able to mint them, this becomes RS256 — a contained change, since
  the algorithm is already a configuration value.
