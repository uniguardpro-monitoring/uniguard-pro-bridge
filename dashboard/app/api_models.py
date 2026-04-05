"""Pydantic models for the ARC REST API.

These models drive request validation, response serialization, and
auto-generated OpenAPI documentation at /api/docs.
"""
from typing import Any, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Standard response envelopes
# ---------------------------------------------------------------------------

class ErrorDetail(BaseModel):
    code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable error description")


class ErrorResponse(BaseModel):
    error: ErrorDetail


class PaginationMeta(BaseModel):
    total: int = Field(..., description="Total number of records")
    page: int = Field(..., description="Current page number")
    per_page: int = Field(..., description="Records per page")
    pages: int = Field(..., description="Total number of pages")


class DataResponse(BaseModel):
    """Single-item response wrapper."""
    data: Any


class PaginatedResponse(BaseModel):
    """Paginated list response wrapper."""
    data: List[Any]
    meta: PaginationMeta


# ---------------------------------------------------------------------------
# Dealer models
# ---------------------------------------------------------------------------

class DealerCreate(BaseModel):
    """Create a new dealer account."""
    name: str = Field(..., min_length=1, max_length=200, description="Dealer company name")
    phone: str = Field("", max_length=50, description="Contact phone number")
    email: str = Field("", max_length=200, description="Contact email address")
    notes: str = Field("", max_length=2000, description="Internal notes")

    model_config = {"json_schema_extra": {"examples": [{"name": "Acme Security", "phone": "555-0100", "email": "ops@acmesec.com"}]}}


class DealerUpdate(BaseModel):
    """Partial update for an existing dealer."""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    phone: Optional[str] = Field(None, max_length=50)
    email: Optional[str] = Field(None, max_length=200)
    notes: Optional[str] = Field(None, max_length=2000)
    enabled: Optional[bool] = Field(None, description="Enable or disable the dealer")


class DealerResponse(BaseModel):
    """Dealer record."""
    id: int
    prefix: str = Field(..., description="Legacy prefix (deprecated)")
    dnis: str = Field(..., description="8-hex linecard/DNIS for signal routing")
    name: str
    phone: str
    email: str
    notes: str
    enabled: bool
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Account models
# ---------------------------------------------------------------------------

class AccountCreate(BaseModel):
    """Create a new alarm system account."""
    account_id: Optional[str] = Field(None, pattern=r"^[0-9]{6}$",
                                       description="6-digit account number (auto-generated if omitted)")
    name: str = Field(..., min_length=1, max_length=200, description="Account / property name")
    address: str = Field("", max_length=500, description="Property address")
    phone: str = Field("", max_length=50, description="Contact phone")
    email: str = Field("", max_length=200, description="Contact email")
    notes: str = Field("", max_length=2000, description="Internal notes")
    dealer_id: Optional[int] = Field(None, description="Dealer ID (required for admin keys, ignored for dealer keys)")

    model_config = {"json_schema_extra": {"examples": [{"name": "Smith Residence", "address": "123 Main St", "phone": "555-0123"}]}}


class AccountUpdate(BaseModel):
    """Partial update for an existing account."""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    address: Optional[str] = Field(None, max_length=500)
    phone: Optional[str] = Field(None, max_length=50)
    email: Optional[str] = Field(None, max_length=200)
    notes: Optional[str] = Field(None, max_length=2000)


class AccountResponse(BaseModel):
    """Alarm system account record."""
    account_id: str
    name: str
    address: str
    phone: str
    email: str
    notes: str
    dealer_id: Optional[int]
    archived_at: Optional[str] = Field(None, description="ISO timestamp if archived, null if active")
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Event models
# ---------------------------------------------------------------------------

class EventResponse(BaseModel):
    """Alarm event record."""
    id: int
    received_at: str
    account_id: str
    event_code: str = Field(..., description="SIA event code (e.g. BA, FA, OP, CL, RP)")
    event_type: Optional[str] = Field(None, description="SIA code type (e.g. Alarm, Open/Close)")
    event_desc: Optional[str] = Field(None, description="SIA code description")
    zone: Optional[str]
    partition: Optional[str]
    message: Optional[str]
    dealer_id: Optional[int]


class EventStatsResponse(BaseModel):
    """Dashboard summary statistics."""
    total_events: int = Field(..., description="Total events (excluding heartbeats)")
    events_today: int = Field(..., description="Events received today")
    alarms_today: int = Field(..., description="Alarm events today (excluding open/close/heartbeat)")
    active_accounts: int = Field(..., description="Distinct accounts with events")


# ---------------------------------------------------------------------------
# Zone models
# ---------------------------------------------------------------------------

class ZoneUpsert(BaseModel):
    """Create or update a zone label."""
    zone_name: str = Field(..., min_length=0, max_length=200, description="Human-readable zone name")

    model_config = {"json_schema_extra": {"examples": [{"zone_name": "Front Door"}]}}


class ZoneResponse(BaseModel):
    """Zone record."""
    id: int
    account_id: str
    zone_number: str
    zone_name: str
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Webhook models
# ---------------------------------------------------------------------------

class WebhookCreate(BaseModel):
    """Create a new webhook endpoint."""
    url: str = Field(..., pattern=r"^https://[^\s]+$", max_length=2000,
                     description="HTTPS endpoint URL")
    description: str = Field("", max_length=200, description="Optional label")
    event_filter: str = Field("*", max_length=200,
                               description="'*' for all events, or comma-separated SIA codes (e.g. 'BA,FA,PA')")
    auth_type: str = Field("hmac", description="Authentication type: 'hmac' (HMAC-SHA256 with shared ARC_WEBHOOK_SECRET) or 'bearer' (Authorization: Bearer token)")
    account_filter: Optional[str] = Field(None, max_length=16,
                                           description="Scope to a specific account ID (e.g. '001'). Null = all accounts.")
    secret: Optional[str] = Field(None, max_length=500,
                                   description="For bearer: your token. For hmac: uses shared ARC_WEBHOOK_SECRET (omit this field).")
    dealer_id: Optional[int] = Field(None, description="Dealer ID (required for admin keys, ignored for dealer keys)")

    model_config = {"json_schema_extra": {"examples": [
        {"url": "https://us.uniguardpro.io/functions/v1/client-webhook", "description": "Smith Residence", "event_filter": "*", "account_filter": "001", "dealer_id": 2},
    ]}}


class WebhookUpdate(BaseModel):
    """Partial update for a webhook."""
    url: Optional[str] = Field(None, pattern=r"^https://[^\s]+$", max_length=2000)
    description: Optional[str] = Field(None, max_length=200)
    event_filter: Optional[str] = Field(None, max_length=200)
    auth_type: Optional[str] = Field(None, description="'hmac' or 'bearer'")
    account_filter: Optional[str] = Field(None, max_length=16)
    enabled: Optional[bool] = None


class WebhookResponse(BaseModel):
    """Webhook record (secret never included)."""
    id: int
    dealer_id: int
    url: str
    description: str
    event_filter: str
    auth_type: str
    account_filter: Optional[str]
    enabled: bool
    created_at: str
    updated_at: str


class WebhookCreatedResponse(BaseModel):
    """Returned only at creation time -- includes the secret once."""
    id: int
    dealer_id: int
    url: str
    description: str
    event_filter: str
    auth_type: str
    account_filter: Optional[str]
    enabled: bool
    secret: str = Field(..., description="Secret/token (shown once, copy immediately)")
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# API Key models
# ---------------------------------------------------------------------------

class ApiKeyCreate(BaseModel):
    """Create a new API key."""
    name: str = Field(..., min_length=1, max_length=200, description="Human-readable key label")
    dealer_id: Optional[int] = Field(None, description="Dealer ID to scope the key (null = admin key)")

    model_config = {"json_schema_extra": {"examples": [{"name": "Production integration", "dealer_id": 2}]}}


class ApiKeyResponse(BaseModel):
    """API key record (hash never included)."""
    id: int
    key_prefix: str = Field(..., description="First 8 characters for identification")
    dealer_id: Optional[int]
    name: str
    permissions: str
    enabled: bool
    created_at: str
    last_used_at: Optional[str]


class ApiKeyCreatedResponse(BaseModel):
    """Returned only at creation time -- includes the raw key once."""
    id: int
    key: str = Field(..., description="Full API key (shown once, copy immediately)")
    key_prefix: str
    dealer_id: Optional[int]
    name: str
    created_at: str
