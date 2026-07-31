"""Domain rules, as pure functions.

Every rule here decides using only its arguments — no session, no request, no clock. That
is deliberate and it is the same split the earlier portfolio projects used between reading
the world and judging it: the orchestration in `services/projects.py` needs a database to
test, these do not, and these are where the business actually lives.

Each function either returns a value or raises a domain error. None of them knows what an
HTTP status code is.
"""

from collections.abc import Mapping

from taskflow_api.db.models import Project, User
from taskflow_api.enums import GlobalRole, ProjectRole
from taskflow_api.exceptions import BusinessRuleError, ConflictError

# Fields a PATCH is allowed to touch. An allowlist rather than "whatever the payload has",
# so that adding a column to the ORM model can never quietly become writable over HTTP.
UPDATABLE_PROJECT_FIELDS = frozenset({"name", "description"})


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
