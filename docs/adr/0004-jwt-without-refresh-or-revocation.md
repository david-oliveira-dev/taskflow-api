# 0004 — Short-lived JWTs, no refresh token, no revocation

Status: accepted · 2026-07-30

## Context

The service needs to authenticate requests without asking for a password every time. The
usual answers are a server-side session, or a signed token; if a token, then with or without
a refresh token, and with or without a way to revoke one before it expires.

Revocation is the interesting part. A signed token is valid because the maths says so, not
because a server says so — which is exactly what makes it cheap to verify and impossible to
withdraw.

## Decision

**A single access token, signed HS256, valid for 15 minutes.** No refresh token, and no
revocation list. The claims are `sub`, `exp`, `iat` and `jti`.

**The expiry window is the security boundary.** Fifteen minutes is the answer to "how long
can a stolen token be used", and there is no second mechanism.

**`jti` is issued anyway**, even though nothing reads it yet.

**`is_active` is checked on every request**, not only at login.

**The signing key must be at least 32 bytes**, enforced at startup.

## Alternatives considered

**Refresh tokens.** The standard pairing, and the right answer for a mobile client where
re-authenticating every fifteen minutes is user-hostile. Rejected as scope: a refresh token
is only useful with rotation and reuse detection, and a refresh token you cannot revoke is
just a long-lived access token wearing a disguise. Half of the feature would be worse than
none.

**A `jti` denylist in Redis.** Genuinely tempting, because Redis is already in the stack for
rate limiting, and the design is small: on logout, store the `jti` with a TTL equal to the
token's remaining life; have the auth dependency check it. This is the first thing to build
if the project grows, which is why the claim is already in the token — adding the denylist
later is an afternoon, not a token-format migration.

**Server-side sessions.** Revocable by construction, and the honest answer for many
services. Rejected because it puts a datastore read on the authentication path of every
request, and because the project exists partly to demonstrate stateless token handling.

## Consequences

- **A stolen token works until it expires.** This is the accepted cost, stated plainly in
  the README rather than hidden.
- **Logout is client-side only** — the client forgets the token; the server does not know.
- **Deactivating an account takes effect immediately anyway**, because the dependency checks
  `is_active` on every request. That check is the only thing standing between a token minted
  before deactivation and a live session, and there is a test that says so.
- Rotating the signing key invalidates every outstanding token at once. That is the blunt
  instrument available if one has to revoke everything.
- HS256 means whoever verifies can also mint. Fine while one service does both; splitting
  verification into another service means moving to RS256, which is a contained change
  because the algorithm is already a configuration value.
