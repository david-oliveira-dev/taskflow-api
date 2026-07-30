# 0003 — Integration tests use testcontainers and the real migration

Status: accepted · 2026-07-30

## Context

The repositories exist to talk to PostgreSQL, and most of what they guarantee — unique
constraints, `ON DELETE SET NULL`, check constraints, row-comparison keyset pagination — is
enforced by the database, not by Python. Testing them against SQLite or a mock proves
nothing about the thing being claimed.

Two decisions follow: where the database comes from, and how the schema gets into it.

## Decision

**Containers via `testcontainers`, started by the test suite**, one per session.

**The schema is created by running the actual Alembic migration**, not by
`Base.metadata.create_all`.

## Alternatives considered

**`services:` in the GitHub Actions workflow.** Faster on the runner, and the usual choice.
Rejected because it only exists in CI: locally you would start containers by hand and keep
two definitions in sync. Divergence between the local setup and CI is precisely what broke
the pipelines of projects 01-03 in this portfolio, and the fix there was to make both run
the same thing. Repeating the mistake knowingly would be worse than making it once.

**`create_all` for the schema.** One line, much faster, and wrong in a specific way: it
builds the schema *from the models*, so the migration could be broken, missing, or drifted
from the models and every test would still pass. Running the migration means each
integration run also proves `upgrade head` works — the acceptance criterion of this phase is
tested continuously rather than once by hand.

**A shared long-lived database.** Fastest of all, and the source of tests that pass only in
the order they were written.

## Consequences

- Integration tests need a Docker daemon and a cached image. This is the documented
  exception to the "tests run offline" rule; unit tests keep it, and `-m "not integration"`
  runs them alone.
- On a mechanical disk the container start dominates: the suite takes roughly 20 seconds,
  almost all of it before the first assertion. Session scope, not function scope, is what
  keeps that from being 20 seconds per test.
- Each test runs in a transaction that is rolled back, so tests share a container without
  sharing state, and the schema is created once.
- The fixture hands the DSN to Alembic through its own config rather than through
  `DATABASE_URL`. An earlier version set the environment variable, and because the fixture
  is session-scoped it leaked into the unit tests — where a test asserting that a missing
  setting fails started passing for the wrong reason. Environment mutation in a
  session-scoped fixture is a trap worth naming.
