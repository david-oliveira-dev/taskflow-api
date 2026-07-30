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

### Security
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
