"""Pydantic schemas — the wire format.

These are the **API** shapes. ORM models live in `db/models.py`, and the two are kept
separate on purpose: a schema that is just the ORM row serialised leaks columns the client
should never see, `hashed_password` first among them.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from taskflow_api.enums import GlobalRole


class RegisterRequest(BaseModel):
    """Payload for creating an account."""

    email: EmailStr
    # Long minimum, no composition rules. Length is what actually resists guessing; forcing
    # a symbol and a digit mostly produces "Password1!" and a note on a monitor.
    password: str = Field(min_length=12, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)


class UserResponse(BaseModel):
    """A user as the API exposes it.

    Note what is absent: `hashed_password` is not here, and cannot be added by accident,
    because this is not built from the ORM row by reflection.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    global_role: GlobalRole
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    """An issued access token, in the shape OAuth2 clients expect."""

    access_token: str
    # S105 fires on the name, not the value: this is the OAuth2 token *type*, a literal
    # the RFC mandates, not a secret.
    token_type: str = "bearer"  # noqa: S105
    expires_in: int = Field(description="Seconds until the token expires.")
