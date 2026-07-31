# 6. The request transaction commits before the response is sent

Date: 2026-07-30

## Status

Accepted

## Context

The session dependency was written the way most FastAPI examples write it: a generator
dependency that yields a session, rolls back if the handler raised, and commits otherwise.

```python
async with factory() as session:
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    else:
        await session.commit()
```

This is wrong in a way that no test in the suite could detect.

FastAPI solves dependencies against an `AsyncExitStack` owned by `AsyncExitStackMiddleware`,
which sits *outside* the router. The router sends the response — `await response(scope,
receive, send)` — and only then does the middleware unwind the stack and run the code after
`yield`. The commit therefore happens **after the client already has the answer**.

Found during manual verification of Phase 3a, against the service running under uvicorn:

```
POST /auth/register  -> 201 Created
POST /auth/login     -> 401 Unauthorized
```

Ten out of ten immediate logins failed. The same credentials worked a second later. The same
shape appeared one level up: a project created with 201 answered 404 on the very next
request, because its `owner` membership had not landed either.

The reason the test suite is blind to it is structural, not an oversight. `httpx`'s
`ASGITransport` awaits the entire application call, exit-stack teardown included, so in
process there is no moment at which the response exists and the commit has not run. Every
integration test passed throughout.

## Decision

Commit inside the request, not in the dependency teardown.

`api/routing.py` defines `TransactionalRoute`, an `APIRoute` whose handler wraps the
generated one:

```python
response = await handle(request)
session = getattr(request.state, "session", None)
if session is not None:
    await session.commit()
return response
```

At that point the endpoint has produced its `Response` object and Starlette has not sent it.
`get_session` publishes the session on `request.state` and keeps its own commit as a
backstop for any router not built on this class — a write arriving late is recoverable, a
write that never arrives is not.

Alternatives considered:

- **Commit in each service method.** Works, but it is one more thing to remember in every
  future service, and forgetting it fails silently in the same invisible way.
- **A middleware.** A middleware wrapping the app runs outside the router too, so it hits
  the same ordering problem for the response body.
- **Leave it and document the caveat.** Rejected outright: an API whose `201` does not mean
  "created" is broken, regardless of what the README says about it.

## Consequences

- The transaction boundary is unchanged: still one transaction per request, owned by the
  edge. Only the moment of commit moved earlier.
- Every router that touches the database must use `route_class=TransactionalRoute`. The
  backstop in `get_session` means forgetting it degrades to the old late-commit behaviour
  rather than losing data outright.
- `tests/integration/test_read_after_write.py` runs a **live uvicorn server on a real
  socket**, because that is the only configuration in which the bug is observable. It was
  confirmed to fail with the commit disabled and to pass with it in place.
- More generally: the in-process ASGI transport is a fast test double, not the deployment.
  Anything whose correctness depends on *when* work happens relative to the response needs a
  socket to be tested honestly. That is a standing exception to "tests run offline", and it
  is why this file exists rather than a comment in the session module.
