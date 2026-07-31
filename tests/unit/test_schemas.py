"""Wire-format rules that hold without a database."""

import pytest
from pydantic import ValidationError

from taskflow_api.api.schemas import (
    PageResponse,
    ProjectCreateRequest,
    ProjectResponse,
    ProjectUpdateRequest,
)


class TestProjectUpdateRequest:
    def test_an_empty_patch_is_rejected(self) -> None:
        """A PATCH with no fields is a client bug; answering 200 would hide it."""
        with pytest.raises(ValidationError, match="at least one field"):
            ProjectUpdateRequest()

    def test_omitting_a_field_and_nulling_it_are_different_requests(self) -> None:
        """The distinction `exclude_unset` preserves, and the reason the service takes a map.

        Both objects have `description is None`; only one of them asked for it.
        """
        omitted = ProjectUpdateRequest(name="Artemis")
        cleared = ProjectUpdateRequest(name="Artemis", description=None)

        assert omitted.model_dump(exclude_unset=True) == {"name": "Artemis"}
        assert cleared.model_dump(exclude_unset=True) == {
            "name": "Artemis",
            "description": None,
        }

    def test_an_unknown_field_is_rejected(self) -> None:
        """`nmae` must fail loudly rather than be dropped behind a 200."""
        with pytest.raises(ValidationError):
            ProjectUpdateRequest(nmae="Artemis")  # type: ignore[call-arg]


class TestProjectCreateRequest:
    def test_a_blank_name_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProjectCreateRequest(name="")

    def test_an_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProjectCreateRequest(name="Apollo", owner_id="me")  # type: ignore[call-arg]


class TestPageResponse:
    def test_the_envelope_is_the_same_shape_for_every_listing(self) -> None:
        page: PageResponse[ProjectResponse] = PageResponse(items=[], has_more=False)

        assert page.model_dump() == {"items": [], "next_cursor": None, "has_more": False}
