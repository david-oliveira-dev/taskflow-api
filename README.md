# taskflow-api

**Most task APIs are correct only when requests arrive one at a time.** Retry a `POST` after
a timeout and you get a duplicate. Page through a list while someone is inserting and you
silently skip rows. Delete a user and the audit trail forgets who acted. Knock over the rate
limiter's Redis and the whole service goes down with it.

This is a projects-and-tasks API where those four are the actual feature set and the CRUD is
the pretext. Every claim below is exercised by a test, and the ones that matter assert on the
**effect** — how many rows exist afterwards — rather than on a status code, because three of
the four real bugs found while building this returned a perfectly correct `201`.

```bash
make up && make migrate && make seed && make run
```

Then open [`docs/walkthrough.http`](docs/walkthrough.http) and run it top to bottom. It ends
by sending the same request twice under one `Idempotency-Key`, exhausting the rate limit, and
reading the audit trail those requests left behind.

---

## The four things

### 1. Idempotency that survives a concurrent retry

`POST` accepts an optional `Idempotency-Key`. A repeat with the same body replays the
original response; a key still in flight gets **409** without waiting or reprocessing; a key
reused with a different body gets **422**, because answering with the first response would
tell the caller their payload had been applied when it never was.

```
POST /projects  Idempotency-Key: abc   ->  201  {"id": "8f0e…"}
POST /projects  Idempotency-Key: abc   ->  201  {"id": "8f0e…"}   Idempotent-Replay: true
POST /projects  Idempotency-Key: abc   ->  422  idempotency_key_reused   (different body)
```

The serialisation is a composite primary key on `(user_id, key)` — the second request **loses
its insert**, and that is what orders the pair. `SELECT`-then-`INSERT` leaves a window where
both callers see nothing and both proceed.

The reservation commits in its **own transaction**. Sharing the request's would make the
second caller block on the unique index until the first finished all of its work, so a replay
would hang rather than be told 409 at once.

[ADR 0007](docs/adr/0007-idempotency-serialised-by-the-database.md)

### 2. A rate limiter that is actually atomic, and fails open

The decision happens inside a single Lua script. `GET`, decide in Python, `SET` is a race:
concurrent callers read the same count, all conclude they are under the limit, and all pass.
**A limiter with a race is worse than no limiter**, because it reports a guarantee it does
not provide. A test fires four requests together against a limit of three and asserts that
exactly one is refused.

The window slides rather than resetting, so a caller cannot spend the full quota at the end
of one window and again at the start of the next.

If Redis is unreachable the request is **allowed**, logged as a warning, and marked
`X-RateLimit-Degraded`. Rate limiting protects against load; it is not an authorisation
control, and letting its outage take the API down trades a small problem for a total one.
`/health` and `/ready` are exempt — a probe throttled by the traffic it exists to survive
gets the container killed in the middle of an incident.

[ADR 0008](docs/adr/0008-rate-limiting-atomic-and-fail-open.md)

### 3. An audit trail that outlives its subject

Every mutation writes an entry **in the same transaction as the change**, so a rejected
request leaves nothing behind — there is a test for exactly that. `AuditRepository` has no
update and no delete: append-only is enforced by giving the application no vocabulary for
anything else.

`actor_id` is `ON DELETE SET NULL` **and** `actor_email` is denormalised at write time, so an
entry still names its author after the account is gone. A patch that changes nothing writes
no entry at all.

`GET /audit` is administrator-only: the trail spans every project, so exposing it would leak
the existence and membership of projects the caller cannot see.

### 4. Pagination that stays correct under concurrent writes

Keyset, not offset. `LIMIT/OFFSET` makes PostgreSQL walk and discard rows, and — worse — the
window shifts when rows are inserted mid-pagination, so a client silently skips or repeats
entries. The test inserts a row **between two page requests** and asserts nothing is dropped
or served twice.

---

## Also true, and less headline-worthy

- **Login does not disclose whether an address has an account.** Unknown address, wrong
  password and deactivated account produce the same status, the same body and the same
  timing — the hash is verified against a dummy even when no user exists. There is a timing
  test, not only a status test.
- **A stranger gets 404, not 403.** A 403 confirms the resource exists, which turns id
  probing into a directory of other people's projects.
- **One error envelope everywhere**, with a stable machine-readable `code` kept separate from
  the human `message`, plus a correlation id that is also a response header. Validation
  failures do not echo the submitted value; Pydantic reports it by default, which was
  reflecting rejected passwords into the response body.
- **The request transaction commits before the response is sent.** FastAPI unwinds dependency
  teardown *after* the client has the answer, so the usual session dependency acknowledges
  writes before they are durable
  ([ADR 0006](docs/adr/0006-commit-before-the-response-is-sent.md)).
- **Enum columns store values and carry CHECK constraints.** SQLAlchemy persists the member
  *name* by default and `native_enum=False` creates no constraint at all — so the database
  held `OWNER` while the API said `owner`, and the columns accepted any string.

## Endpoints

| Route | Notes |
|---|---|
| `POST /auth/register` · `POST /auth/login` · `GET /auth/me` | Accounts and tokens |
| `GET\|POST /projects` · `GET\|PATCH\|DELETE /projects/{id}` | `DELETE` archives; nothing is physically removed |
| `GET\|POST /projects/{id}/members` · `PATCH\|DELETE …/{user_id}` | Membership, owner only |
| `GET\|POST /projects/{id}/tasks` · `GET\|PATCH\|DELETE /tasks/{id}` | Tasks, editor and above |
| `GET /audit` | The trail, administrators only |
| `GET /health` · `GET /ready` | Liveness and readiness |

Listings are cursor-paginated and share one envelope:
`{"items": [...], "next_cursor": "…", "has_more": true}`.

Roles: `viewer` reads, `editor` creates and edits tasks, `owner` also manages membership. A
global `admin` is treated as an owner of any project — and every such action lands in the
audit trail under their own address, which is what makes the bypass acceptable rather than
merely convenient.

Interactive docs at `/docs`, disabled when `ENVIRONMENT=production`.

## Running it

Requires Docker and [`uv`](https://docs.astral.sh/uv/). Nothing else — Postgres and Redis run
in containers.

```bash
cp .env.example .env     # the defaults match docker-compose.yml
make install             # uv sync
make up                  # Postgres 16 + Redis 7, bound to 127.0.0.1 only
make migrate             # alembic upgrade head
make seed                # demo accounts, a project with all three roles, some tasks
make run                 # uvicorn with reload, on :8000
```

`make seed` prints the credentials and is safe to re-run. It writes through the service layer
rather than the repositories, so `GET /audit` has a real trail waiting the moment you look.

### Tests

```bash
make test-unit   # offline, no Docker
make test        # everything, including containers
make check       # what CI runs: lint, types, tests
```

Unit tests are offline and deterministic. **The integration tests are a documented exception**
to that rule: they start real Postgres and Redis containers through testcontainers, because a
fake Redis would only prove the fake agrees with itself, and the entire point of the limiter
is that *Redis* evaluates its script atomically. The schema under test is built by running the
real Alembic migration, so every run also proves `upgrade head`.

One test goes further and runs a **live uvicorn on a socket**. It exists because `httpx`'s
in-process transport awaits the whole application call, teardown included — which made the
suite structurally blind to work happening after the response was sent.

## Configuration

Every setting comes from the environment and is validated at startup, so a missing value stops
the process rather than surfacing as a confusing 500 later. `.env.example` lists them all with
commentary.

The JWT signing key must be at least 32 bytes (RFC 7518 §3.2), enforced at startup rather than
left as a runtime warning. `Settings` never renders credentials: `PostgresDsn` and `RedisDsn`
keep the password in plain text in their reprs, so anything reporting configuration uses
`safe_database_url` / `safe_redis_url`.

Logs are JSON in `ci` and `production` and human-readable locally. Every line carries the
request's correlation id, including lines from SQLAlchemy and uvicorn, which are routed through
the same pipeline. The access line records method, path, status and duration — never the query
string, the headers or the body.

## Limitations, stated plainly

- **Not multi-tenant.** Isolation is by membership, not by schema or database.
- **No refresh token and no revocation.** Access tokens live 15 minutes and that window *is*
  the security boundary. A `jti` is already minted on every token, so a Redis denylist is an
  afternoon's work rather than a token-format migration — the design is written down in
  [ADR 0004](docs/adr/0004-jwt-without-refresh-or-revocation.md).
- **The limiter is not a gateway.** It counts in shared Redis, so it works across replicas,
  but it does no burst shaping, per-endpoint quotas or client classes.
- **Archived projects accumulate.** No purge, by design; retention would be its own decision.
- **The idempotency sweep is a method, not a scheduler.** `purge_expired()` exists and is
  tested; wiring it to cron or a worker is left to whoever deploys this.
- **No notifications, no email, no realtime.**

## Layout

```
src/taskflow_api/
  api/          routers, dependencies, schemas, middleware, error envelope
  services/     orchestration and the audit trail; rules.py holds the pure decisions
  repositories/ SQLAlchemy access — no business rules, no transactions
  db/           engine, session, ORM models, Redis client
docs/
  architecture.md    diagrams and the layer contract
  walkthrough.http   the executable tour
  adr/               eight decision records
```

[`docs/architecture.md`](docs/architecture.md) has the request-path and data-model diagrams
and explains which orderings are load-bearing.

## Contributing

Keep the domain core free of I/O, keep unit tests offline, and run `make check` before opening
a pull request.

## Licence

MIT — see [LICENSE](LICENSE).

## Contact

David Oliveira — davidoliveira.devbr@gmail.com
