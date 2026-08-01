# 8. Rate limiting: atomic in Redis, and failing open

Date: 2026-08-01

## Status

Accepted

## Context

The specification (§2.1) fixes two properties and leaves the algorithm open: the count must
be atomic, and Redis being unavailable must not take the API down with it.

## Decision

### The decision happens inside one Lua script

`GET`, decide in Python, `SET` is a race. Under concurrency several callers read the same
count, all conclude they are under the limit, and all pass. **A limiter with a race is worse
than no limiter**, because it reports a guarantee it does not provide — capacity planning
and abuse response both end up resting on a number that is wrong exactly when load is high.

One Lua script, which Redis runs to completion without interleaving, makes the arithmetic
exact. `test_concurrent_requests_against_a_limit_of_n_minus_one` fires four requests
together against a limit of three and asserts that exactly one is refused; that test fails
against any check-then-set implementation.

### A sliding window, not a fixed one

The window is a sorted set of request timestamps, trimmed on every call. `INCR` plus
`EXPIRE` is cheaper and would have satisfied "atomic", but a fixed window lets a caller
spend the full quota at the end of one window and again at the start of the next — twice the
intended rate across the boundary, which is precisely the burst the limit exists to stop.

### Failure mode: open

If Redis is unreachable the request is allowed, a `WARNING` is logged, and the response
carries `X-RateLimit-Degraded: true`.

Rate limiting here protects against excess load. It is **not** an authorisation control, and
letting its outage take the whole API down trades a small problem for a total one. Failing
closed is defensible where a limiter guards something like fraud; this is not that. The
header exists so the choice is visible in the response rather than only in a log — a
request that was not actually counted says so.

`/ready` reports the same distinction: Redis down is `degraded` with a 200, because the
service still works. Only Postgres down produces a 503.

### The identity is read from an unverified token

The middleware runs before routing, which is the only placement that protects anything —
the quota has to be spent before any handler or database work happens. But authentication
is a dependency and has not run yet.

Rather than duplicate the auth path, the middleware reads the `sub` claim **without
verifying the signature**, falling back to the peer address. That is safe here precisely
because the claim only selects a counter: forging a token buys a different bucket, never
access. `get_current_user` still verifies the signature before anything is served.

Keying on the account rather than the address matters: one noisy client behind a shared
address would otherwise exhaust the quota of everyone behind it.

### The probes are exempt

`/health` and `/ready` are never throttled. A liveness probe rejected by the traffic it
exists to survive gets the container killed in the middle of an incident — the limiter would
turn a load spike into an outage.

## Consequences

- Every request costs one Redis round trip. Timeouts on the client are short (250 ms) and
  explicit: since the limiter fails open, a hanging Redis must become a fast error rather
  than a slow one, or the fail-open would itself stall every request.
- The three `X-RateLimit-*` headers are on every response, not just rejections. A client can
  only back off before hitting the wall if it is told how close it is.
- `Retry-After` is never zero on a rejection; it would invite an immediate retry certain to
  be rejected again.
- **The test suite has to opt out.** In-process requests carry no peer address, so every
  unauthenticated call in the suite counts against one `ip:unknown` bucket and the suite
  throttles itself. Integration fixtures raise the limit out of the way, and the tests that
  exercise the limiter authenticate — giving each test a bucket of its own by construction
  rather than depending on a `flushdb` between runs.
