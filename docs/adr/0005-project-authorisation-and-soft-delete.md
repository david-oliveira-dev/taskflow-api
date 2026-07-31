# 5. Project authorisation, soft delete, and where the audit entry is written

Date: 2026-07-30

## Status

Accepted

## Context

Phase 3a introduces the first resources with more than one class of caller. Three questions
had to be settled before writing the endpoints, because each one leaks into every later
phase if it is answered inconsistently:

1. What decides whether a caller may act on a project — their membership, their global
   role, or both?
2. What does `DELETE /projects/{id}` actually do, given the domain model carries both an
   `is_archived` flag and a delete verb?
3. Where does the audit entry get written, relative to the mutation it describes?

## Decision

### Authorisation is membership-based, with a global-admin bypass

The per-project role in `memberships` is the authority. `Project.owner_id` records who
created the project and is never consulted for a permission decision — two sources of truth
for the same question is how "the owner cannot open their own project" bugs happen.

A global `admin` is resolved to `owner` on any project, membership or not
(`services.rules.effective_role`). The alternative was an administrator who has to be added
to a project before they can support it, which in practice means people reach for `psql` —
and a database console leaves no trace at all. The bypass is acceptable specifically because
every mutation lands in the audit trail under the admin's own address.

The bypass is deliberately *narrow*: `GET /projects` stays membership-scoped even for an
admin. Administering a project someone asked about is a different capability from
enumerating every project in the installation, and merging them turns a support action into
a data dump.

A caller with no access gets **404, not 403**. A 403 confirms the project exists, which
turns id probing into a directory of other people's projects. The 403 only appears once
membership is established, when the caller already knows the project is there.

### `DELETE` archives; nothing is physically deleted

`DELETE /projects/{id}` sets `is_archived`. The project drops out of listings, stays
readable by id, and refuses every subsequent mutation with 409 — **including a second
archive**. Making re-archiving idempotent was tempting, but it costs a second rule
("archived projects reject all writes, except this one"), and rules with exceptions are the
ones that get applied wrongly six months later.

Tasks are still deleted physically when their own endpoints arrive: a task is cheap and the
trail records its removal. Users are soft-deleted through `is_active`.

### The audit entry is written inside the mutation's transaction

`ProjectService` appends the entry through the same session, so the request-scoped
transaction commits both or neither. An entry that survives a rolled-back change is a lie,
and one lost when the change succeeds is worse. `tests/integration/test_projects_api.py`
proves the negative case: a rejected `POST /members` leaves no `member.added` behind.

Two supporting choices follow from wanting the trail to stay readable:

- `actor_email` is denormalised at write time, so an entry still names its author after the
  account is gone (`actor_id` is `ON DELETE SET NULL`).
- A patch that changes nothing writes no entry at all. `services.rules.changed_fields`
  diffs the request against current state, and only genuine changes are recorded. A trail
  full of "updated" entries describing no update stops being read.

`AuditRepository` has no update and no delete method. Append-only is enforced by giving the
application no vocabulary for anything else, rather than by a convention someone has to
remember in review.

## Consequences

- Granting a global `admin` role is a significant act: it confers owner-level access to
  every project. There is deliberately no endpoint that grants it — it is a database
  operation, and the integration tests reach around the API to do it.
- Archived projects accumulate. There is no purge, by design; if storage ever becomes the
  constraint, that is a separate decision with its own retention policy.
- Because authorisation ignores `owner_id`, a project can outlive its creator's membership
  once another owner exists. That is intended: the `owner` **role** is what is protected
  (`ensure_not_last_owner`), not the original creator.
- The rules live in `services/rules.py` as pure functions and are unit-tested offline;
  `services/projects.py` only sequences them. The split is the same one the earlier
  portfolio projects used between reading the world and judging it.
