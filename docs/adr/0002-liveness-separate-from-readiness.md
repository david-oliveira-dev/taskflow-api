# 0002 — `/health` and `/ready` are different endpoints

Status: accepted · 2026-07-30

## Context

Orchestrators ask a service two questions that look alike and mean opposite things:

- *Is this process alive, or should I kill and restart it?* (liveness)
- *Can this process serve a request right now, or should I route around it?* (readiness)

The tempting shortcut is one `/health` endpoint that checks Postgres and Redis and answers
both. It is a shortcut with a specific failure mode.

## Decision

Two endpoints, with a rule about what each may touch.

`GET /health` — liveness. Returns the process status and version. **Touches no dependency.**

`GET /ready` — readiness. Checks Postgres and Redis, and fails when they are unreachable.
Wired in Phase 4, once those connections exist.

## Alternatives considered

**A single endpoint that checks dependencies.** This is the common shortcut, and it converts
a dependency outage into a total outage: Postgres goes down, the liveness probe starts
failing, the orchestrator concludes every container is broken and kills them all. The
containers were healthy — they just had nothing to talk to. Restarting them does not bring
Postgres back, and now there is no capacity left for when it returns.

**A single endpoint that checks nothing.** Cheap and safe for liveness, but then the load
balancer keeps sending traffic to an instance that cannot serve it.

## Consequences

- The Docker `HEALTHCHECK` and the CI smoke test both hit `/health`, which is why the image
  can be smoke-tested with no Postgres and no Redis anywhere near it. That test is a real
  guard: if someone later adds a dependency check to `/health`, CI fails.
- `/ready` needs its own timeout budget, so a slow dependency degrades it into a clear
  failure instead of a hanging request. That is a Phase 4 concern.
