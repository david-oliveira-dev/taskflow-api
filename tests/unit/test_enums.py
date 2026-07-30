"""Role ordering — the basis of every authorisation check that comes later."""

import pytest

from taskflow_api.enums import ProjectRole


@pytest.mark.parametrize(
    ("held", "required", "allowed"),
    [
        (ProjectRole.OWNER, ProjectRole.VIEWER, True),
        (ProjectRole.OWNER, ProjectRole.EDITOR, True),
        (ProjectRole.OWNER, ProjectRole.OWNER, True),
        (ProjectRole.EDITOR, ProjectRole.VIEWER, True),
        (ProjectRole.EDITOR, ProjectRole.EDITOR, True),
        (ProjectRole.EDITOR, ProjectRole.OWNER, False),
        (ProjectRole.VIEWER, ProjectRole.VIEWER, True),
        (ProjectRole.VIEWER, ProjectRole.EDITOR, False),
        (ProjectRole.VIEWER, ProjectRole.OWNER, False),
    ],
)
def test_role_privilege_is_ordered(held: ProjectRole, required: ProjectRole, allowed: bool) -> None:
    assert held.can_act_as(required) is allowed


def test_roles_serialise_as_their_value() -> None:
    """StrEnum keeps the wire format stable: the API sends "owner", not "ProjectRole.OWNER"."""
    assert f"{ProjectRole.OWNER}" == "owner"
