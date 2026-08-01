# 7. Idempotency is serialised by the database, in its own transaction

Date: 2026-08-01

## Status

Accepted

## Context

`Idempotency-Key` is usually described as caching a response. That framing solves the easy
half. The case that actually breaks services is two requests with the same key arriving
**together**, the first still in flight — and no cache helps there, because at the moment
the second arrives there is nothing to serve.

The specification (§2.1) fixes the behaviour: the second request gets **409**, it does not
wait and it does not reprocess; a key reused with a different body gets **422**; records
expire after 24 hours.

## Decision

### The composite primary key is the mechanism, not a constraint

`(user_id, key)` is the primary key of `idempotency_keys`. A request claims a key by
inserting; the second request **loses that insert**, and losing it is what serialises the
pair. The obvious alternative — `SELECT` first, insert if absent — has a window in which
both callers see nothing and both proceed, which is the exact duplicate the header exists
to prevent.

Keys are scoped per account, so one client cannot collide with, or probe for, another's.

### The reservation commits in its own transaction

This is the part that is easy to get wrong, and it is not an optimisation.

If the reservation shared the request's transaction, the second caller's `INSERT` would
**block on the unique index** until the first request finished all of its work — so a replay
would hang for the duration of the original rather than being told 409 at once. Committing
the reservation first makes the conflict visible immediately. `IdempotencyStore` therefore
takes a session *factory* and opens its own unit of work for every operation.

The cost is that a reservation can outlive a failed request, so the route releases the key
when the handler raises or answers 4xx. Without that, a request that failed would hold its
key for the full retention window and every retry — including the one that would have
worked — would be answered 409 for a day.

### Claiming happens in a dependency; recording happens in the route

The two halves need different things. Claiming needs the authenticated caller, which only
exists after the auth dependency has run. Recording needs the response, which only exists
after the endpoint has.

A completed key **raises** `IdempotentReplay` from the dependency rather than setting a flag.
That is deliberate: raising is the only way a dependency can stop the endpoint from running,
and stopping it is the entire point.

This was found the hard way. The first implementation had the route class check a flag on
`request.state` before calling the handler — but the dependency that sets that flag runs
*inside* the handler, so the check always saw nothing. The endpoint ran, the resource was
created a second time, and the replay never happened. `test_repeating_a_request_returns_the
_first_response` caught it by asserting on the **effect** — how many projects exist — rather
than only on the status code, which was 201 either way.

## Consequences

- Every idempotent request costs two extra short transactions (claim, then record). That is
  the price of the reservation being visible to concurrent callers.
- A crash between claim and record leaves a key `in_progress` until it expires. Retries get
  409 rather than a duplicate, which is the safe direction; `purge_expired()` exists to
  sweep the table and is deliberately a method rather than a background loop, so scheduling
  stays an operational decision.
- The stored snapshot is the JSON body as sent. A response that is not a JSON object — a
  204 — is stored with a null payload and replayed as such.
- Tests assert on effects, not statuses. `codes.count(201) == 1` would pass against an
  implementation that created five projects and returned four errors; counting the rows is
  what makes the assertion mean anything.
