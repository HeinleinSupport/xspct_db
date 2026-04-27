# SPDX-License-Identifier: EUPL-1.2
# SPDX-FileCopyrightText: 2026 Carsten Rosenberg <c.rosenberg@heinlein-support.de>

"""Pydantic models for OpenAPI request / response documentation."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QueryResponse(BaseModel):
    """User lookup result."""

    users: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Map of primary-key → user attribute dict.",
    )


class UserInput(BaseModel):
    """A single lookup target."""

    username: str = Field(..., description="Email address or username to look up.")


class QueryJsonRequest(BaseModel):
    """Request body for the batch query endpoint."""

    users: list[UserInput] = Field(..., description="List of users to look up.")


class RspamdSettingsResponse(BaseModel):
    """Rspamd settings blob returned to the Rspamd settings HTTP module."""

    actions: dict[str, float] = Field(
        ..., description="Score thresholds (reject, greylist, add header, …)."
    )
    flags: list[str] = Field(default_factory=list, description="Rspamd processing flags.")
    groups_disabled: list[str] = Field(
        default_factory=list, description="Rspamd groups to disable."
    )
    symbols: list[str] = Field(
        default_factory=list, description="Rspamd symbols to force."
    )


class ErrorResponse(BaseModel):
    """Generic error response."""

    error: str = Field(..., description="Human-readable error message.")
