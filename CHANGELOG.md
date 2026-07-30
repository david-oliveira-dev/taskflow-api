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

### Security
- `Settings.safe_database_url` / `safe_redis_url` mask credentials for logging.
  `PostgresDsn` and `RedisDsn` keep the password in plain text in their reprs, so logging
  a settings object would have leaked the database password into the log stream.
