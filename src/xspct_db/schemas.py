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


class QueryJsonRequest(BaseModel):
    """Request body for the batch query endpoint."""

    users: list[str] = Field(default_factory=list, description="List of users to look up.")


class RspamdSettingsRequest(BaseModel):
    """Request body sent by Rspamd to the settings HTTP module."""

    uid: str = Field(default="", description="Rspamd session UID.")
    from_addr: str = Field(default="", alias="from", description="Envelope sender.")
    rcpts: list[str] = Field(default_factory=list, description="Envelope recipients.")
    mta_name: str | None = Field(default=None, alias="mta-name", description="MTA name reported by Rspamd.")
    mta_host: str | None = Field(default=None, alias="mta-host", description="MTA hostname reported by Rspamd.")
    ip: str | None = Field(default=None, description="Client IP address.")
    settings_name: str | None = Field(default=None, alias="settings-name", description="Rspamd settings name.")
    settings_id: str | None = Field(default=None, alias="settings-id", description="Rspamd settings ID.")

    model_config = {"populate_by_name": True}


class RspamdSettingsResponse(BaseModel):
    """Rspamd settings blob returned to the Rspamd settings HTTP module."""

    actions: dict[str, float] = Field(..., description="Score thresholds (reject, greylist, add header, …).")
    flags: list[str] = Field(default_factory=list, description="Rspamd processing flags.")
    groups_disabled: list[str] = Field(default_factory=list, description="Rspamd groups to disable.")
    groups_enabled: list[str] | None = Field(default=None, description="Rspamd groups to enable.")
    symbols_disabled: list[str] = Field(default_factory=list, description="Rspamd symbols to disable.")
    symbols_enabled: list[str] | None = Field(default=None, description="Rspamd symbols to enable.")
    symbols: list[str] = Field(default_factory=list, description="Rspamd symbols to force.")
    settings_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured user/alias data for addresses found in from/rcpts.",
    )
    settings_error: list[str] = Field(
        default_factory=list,
        description="Error messages produced during settings evaluation.",
    )


class ErrorResponse(BaseModel):
    """Generic error response."""

    error: str = Field(..., description="Human-readable error message.")
