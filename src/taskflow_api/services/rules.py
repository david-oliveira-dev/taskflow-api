"""Domain rules, as pure functions.

Every rule here decides using only its arguments — no session, no request, no clock. That
is deliberate and it is the same split the earlier portfolio projects used between reading
the world and judging it: the orchestration in `services/projects.py` needs a database to
test, these do not, and these are where the business actually lives.

Each function either returns a value or raises a domain error. None of them knows what an
HTTP status code is.
"""

import uuid
from collections.abc import Mapping

from taskflow_api.db.models import Membership, Project, User
from taskflow_api.enums import GlobalRole, ProjectRole
from taskflow_api.exceptions import BusinessRuleError, ConflictError

# Fields a PATCH is allowed to touch. An allowlist rather than "whatever the payload has",
# so that adding a column to the ORM model can never quietly become writable over HTTP.
UPDATABLE_PROJECT_FIELDS = frozenset({"name", "description"})
UPDATABLE_TASK_FIELDS = frozenset({"title", "description", "status", "priority", "assignee_id"})


def ensure_mutable(project: Project) -> None:
    """Refuse any write to an archived project.

    Archiving is this project's delete (there is no physical one), so an archived project
    behaves like a deleted one for writes while staying readable by id. Allowing edits would
    make "archived" a label rather than a state.

    Args:
        project: The project being written to.

    Raises:
        ConflictError: The project is archived.
    """
    if project.is_archived:
        raise ConflictError("Project is archived and cannot be modified.")


def ensure_not_last_owner(*, current_role: ProjectRole, owner_count: int) -> None:
    """Refuse to strip the `owner` role from the only owner.

    A project whose last owner is removed or demoted has nobody who can manage its
    membership — including nobody who can appoint a new owner. It is unreachable state, so
    the rule is enforced at the moment it would be created.

    Args:
        current_role: The role the affected member holds today.
        owner_count: How many members currently hold `owner`.

    Raises:
        ConflictError: This member is the only owner.
    """
    if current_role is ProjectRole.OWNER and owner_count <= 1:
        raise ConflictError(
            "This is the project's only owner; promote another member before removing "
            "or demoting this one."
        )


def ensure_can_be_member(user: User | None) -> User:
    """Check that a user may be added to a project.

    Args:
        user: The looked-up account, or None if there is no such user.

    Returns:
        The user, narrowed to non-None.

    Raises:
        BusinessRuleError: There is no such account, or it is deactivated. 422 rather than
            404: the resource addressed by the request is the project's member list, which
            does exist — it is the *body* that names something unusable.
    """
    if user is None:
        raise BusinessRuleError("No such user.")
    if not user.is_active:
        raise BusinessRuleError("Cannot add a deactivated account to a project.")
    return user


def ensure_assignee_is_member(
    *, assignee_id: uuid.UUID | None, membership: Membership | None
) -> None:
    """Refuse to assign work to someone outside the project.

    An assignee who cannot open the project cannot do the work, and would not see that it
    had been given to them. Worse, the assignment leaks the existence of a project to
    someone with no access to it.

    Unassigning is always allowed, which is what makes the `None` case a plain return
    rather than an error: it is how a member's work is released when they leave.

    Args:
        assignee_id: Who the task should be assigned to, or None to leave it unassigned.
        membership: That user's membership in the task's project, or None if they have none.

    Raises:
        BusinessRuleError: The nominated assignee is not a member. 422, matching the rule in
            the specification: the request is well-formed and can never be valid.
    """
    if assignee_id is None:
        return
    if membership is None:
        raise BusinessRuleError("The assignee must be a member of the project.")


def json_safe(diff: Mapping[str, Mapping[str, object]]) -> dict[str, dict[str, object]]:
    """Render a field diff so it can be stored as JSONB.

    A diff carries the real values, because the service assigns from it. Audit details go
    through `json.dumps`, which has no idea what a `UUID` is — so the copy that reaches the
    trail stringifies them. Enum members pass through untouched: they subclass `str`, and
    they serialise to the value the API uses.

    Args:
        diff: The output of `changed_fields`.

    Returns:
        The same structure with every value JSON-serialisable.
    """
    return {
        field: {
            key: str(value) if isinstance(value, uuid.UUID) else value
            for key, value in change.items()
        }
        for field, change in diff.items()
    }


def effective_role(*, user: User, membership_role: ProjectRole | None) -> ProjectRole | None:
    """Resolve the role a user acts with inside a project.

    A global `admin` is treated as an owner of every project even without a membership.
    The alternative — an administrator who cannot open a project to support it — pushes
    people towards querying the database by hand, which leaves no trace at all. Here every
    such action lands in the audit trail under the admin's own address, which is what makes
    the bypass acceptable rather than merely convenient.

    Args:
        user: The caller.
        membership_role: Their role in the project, or None if not a member.

    Returns:
        The role to authorise against, or None when the user has no access at all.
    """
    if membership_role is not None:
        return membership_role
    if user.global_role is GlobalRole.ADMIN:
        return ProjectRole.OWNER
    return None


def changed_fields(
    *, current: Mapping[str, object], requested: Mapping[str, object]
) -> dict[str, dict[str, object]]:
    """Diff a patch against the current state.

    Returns only the fields that actually change, which is what the audit entry should
    record: an entry saying "name was updated" when the name was rewritten to its existing
    value is noise, and noise is how trails stop being read.

    Args:
        current: The values as they are now.
        requested: The values the client asked for, already limited to the fields it sent.

    Returns:
        A mapping of field name to `{"from": ..., "to": ...}`, empty when nothing changes.
    """
    return {
        field: {"from": current.get(field), "to": value}
        for field, value in requested.items()
        if current.get(field) != value
    }
