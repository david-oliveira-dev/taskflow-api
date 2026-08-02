# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Project foundation: `uv` with a pinned Python 3.12, `src/` layout with the `api` /
  `services` / `repositories` / `db` layers, Ruff, mypy in strict mode and pre-commit.
- Environment-driven configuration validated at startup, with the JWT key held in a
  `SecretStr` so it cannot leak through reprs or logs.
- Liveness probe `GET /health`, which touches no dependency by design.
- `docker-compose.yml` with Postgres 16 and Redis 7, and a multi-stage `Dockerfile`
  running as a non-root user.
- CI running lint, type checking and tests on every push and pull request, plus an
  image build whose smoke test starts the container with unreachable dependencies and
  still expects `/health` to answer.

- Domain model: the six entities, with UUID primary keys, enums stored as VARCHAR with a
  CHECK constraint, and a deterministic constraint naming convention so downgrades are
  reliable.
- Alembic migrations, wired to the same `Settings` the application uses.
- Async engine, session factory, and a `session_scope` that owns the transaction boundary.
- Repositories for users, projects, memberships and tasks.
- Keyset (cursor) pagination, with the page size clamped.

- Authentication: `POST /auth/register`, `POST /auth/login` (OAuth2 password flow) and
  `GET /auth/me`, with Argon2id password hashing and short-lived HS256 access tokens.
- Authorisation dependencies: `get_current_user`, `require_global_role` and
  `require_project_role`, the last resolving the caller's membership from the path.

- Projects: `GET|POST /projects` and `GET|PATCH|DELETE /projects/{id}`, with cursor
  pagination on the listing and archived projects hidden unless asked for.
- Membership: `GET|POST /projects/{id}/members` and
  `PATCH|DELETE /projects/{id}/members/{user_id}`, restricted to project owners.
- Audit trail written inside the same transaction as every mutation it describes, plus
  `GET /audit` for global administrators, filterable by entity and cursor-paginated.
- Domain rules as pure functions in `services/rules.py`, unit-tested without a database:
  archived projects reject writes, the only owner cannot be removed or demoted, a member
  must be an existing active account, and a patch that changes nothing records nothing.
- Removing a member clears their assignee on that project's tasks rather than deleting the
  work, and the audit entry records how many tasks were affected.
- Domain errors are mapped to statuses centrally in `api/errors.py` instead of in each
  handler; an error the edge does not recognise stays a 500 and its message is not echoed.

- Tasks: `GET|POST /projects/{id}/tasks` (cursor-paginated, filterable by status) and
  `GET|PATCH|DELETE /tasks/{task_id}`. Creating and editing needs `editor`, reading needs
  `viewer`, and a task id resolves to its project before authorisation runs — a caller with
  no access gets 404 rather than 403, as on projects.
- The assignee rule: a task can only be assigned to a member of its project (422
  otherwise), and sending `assignee_id` as null unassigns it. Tasks in an archived project
  are readable but not editable, and are deleted physically rather than flagged, with the
  audit entry keeping the title so the trail still says what went.

- Idempotency: `POST` accepts an optional `Idempotency-Key`. A repeat with the same body
  replays the original response (marked `Idempotent-Replay: true`); a key still in flight
  answers 409 without waiting or reprocessing; a key reused with a different body answers
  422. Serialised by the composite primary key on `(user_id, key)`, reserved in its own
  transaction so concurrent callers see the conflict at once. Records expire after 24 hours
  and `purge_expired()` sweeps them. See
  `docs/adr/0007-idempotency-serialised-by-the-database.md`.
- Rate limiting: a sliding window decided inside a single Lua script, so the count stays
  exact under concurrency. Keyed per account where the caller is authenticated, per address
  otherwise. Every response carries `X-RateLimit-Limit` / `-Remaining` / `-Reset`, and a 429
  adds `Retry-After`. Redis unavailable means the request is allowed, logged as a warning
  and marked `X-RateLimit-Degraded`. `/health` and `/ready` are exempt. See
  `docs/adr/0008-rate-limiting-atomic-and-fail-open.md`.
- A single error envelope on every failure —
  `{"error": {"code", "message", "detail"}, "correlation_id"}` — with a stable, machine
  readable `code` separate from the human `message`.
- A correlation id per request, returned as `X-Correlation-Id` and echoed in every error
  body. An inbound id is honoured so a trace survives across services, but only after being
  validated: it reaches logs and responses, and unvalidated input there is how log forging
  starts.
- `GET /ready` checks Postgres and Redis and names each separately. Postgres down is 503;
  Redis down is 200 and `degraded`, because the limiter fails open and the service still
  works.

- Structured logging with `structlog`: JSON in `ci` and `production`, human-readable
  locally. Every line carries the request's correlation id, injected by a processor so no
  call site has to remember it, and standard-library logs — SQLAlchemy, uvicorn — are routed
  through the same pipeline. The access line records method, path, status and duration, and
  deliberately nothing else: the query string carries tokens in the wild, the headers carry
  `Authorization`, and the body is where passwords are.
- `make seed` populates demo data — an administrator, the three project roles, a project and
  tasks in different states. It writes through the service layer rather than the
  repositories, so `GET /audit` has a real trail waiting; demo data with no trail would
  undercut the point. Safe to re-run.
- `docs/walkthrough.http` — the executable tour, ending on the three cross-cutting concerns.
- `docs/architecture.md` — request-path and data-model diagrams, the layer contract, and
  which orderings are load-bearing.
- README rewritten around the four properties the project exists to demonstrate.

### Changed
- `require_project_role` now returns a `ProjectAccess` carrying the loaded project, so
  handlers no longer fetch it a second time.
- Keyset pagination moved into `repositories/base.py` and is shared by every listing.
- Validation failures no longer echo the submitted value. Pydantic reports `input` by
  default, which would reflect a rejected password into the response body and any log that
  captures it.

### Fixed
- `Task` maps with `eager_defaults`, so the database-computed `updated_at` comes back with
  the UPDATE itself. Without it the attribute is expired after a flush and serialising the
  response triggers a lazy refresh, which under async SQLAlchemy raises `MissingGreenlet`
  rather than quietly issuing a query. PostgreSQL fetches it with RETURNING, so the fix
  costs no extra round trip.
- Enum columns now store the member **value** and are guarded by the CHECK constraint this
  project always claimed they had. SQLAlchemy persists the member *name* by default, so the
  database held `OWNER` and `TODO` while the API spoke `owner` and `todo` — invisible
  through the ORM, and silently wrong for every other reader: `WHERE status = 'done'` from
  psql matched nothing. `native_enum=False` also does not create a constraint on its own
  (`create_constraint` has defaulted to False since SQLAlchemy 1.4), so the columns were
  bare VARCHARs accepting any string. Both are now set explicitly in `_enum_column`, the
  initial migration was amended to match, and `upgrade head` / `downgrade base` were
  re-verified against a populated database.
- The request transaction now commits **before** the response is sent. It previously
  committed in the teardown of the session dependency, which FastAPI runs after the
  response has already reached the client: `POST /auth/register` answered 201 and the
  immediately following login failed with 401, ten times out of ten, and a freshly created
  project answered 404 on the next request. The whole test suite was structurally blind to
  this, because `httpx`'s ASGI transport awaits the teardown before returning. Fixed with
  `TransactionalRoute` and pinned by a regression test that runs a live server on a socket.
  See `docs/adr/0006-commit-before-the-response-is-sent.md`.

### Security
- The audit trail is append-only: `AuditRepository` exposes no update or delete, and
  `GET /audit` is restricted to global administrators because the trail spans every
  project.
- A caller with no access to a project receives 404 rather than 403, so probing ids cannot
  be used to discover which projects exist.
- `PATCH` bodies reject unknown fields instead of ignoring them, and only an allowlist of
  columns is writable.
- `Settings.safe_database_url` / `safe_redis_url` mask credentials for logging.
  `PostgresDsn` and `RedisDsn` keep the password in plain text in their reprs, so logging
  a settings object would have leaked the database password into the log stream.
- Login does not disclose whether an email is registered: unknown address, wrong password
  and deactivated account produce the same status, the same body, and the same timing —
  the password check runs against a dummy hash when no account exists.
- The JWT signing key must be at least 32 bytes (RFC 7518 section 3.2), enforced at
  startup rather than left as a runtime warning.
- Tokens are decoded with an explicit algorithm list, so an `alg: none` token is rejected.
- A request whose account was deactivated after the token was issued is refused, since
  tokens themselves cannot be revoked.
