"""ARC REST API v1.

Provides programmatic access to the Alarm Receiving Center for managing
dealers, accounts, zones, events, webhooks, and API keys. Authenticated
via API key in the X-API-Key header.

Interactive documentation: /api/docs (Swagger UI) or /api/redoc (ReDoc)
"""
import asyncio
import hashlib
import json
import math
import os
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader

from .api_models import (
    AccountCreate, AccountResponse, AccountUpdate,
    ApiKeyCreate, ApiKeyCreatedResponse, ApiKeyResponse,
    DataResponse, DealerCreate, DealerResponse, DealerUpdate,
    ErrorResponse, EventResponse, EventStatsResponse,
    PaginatedResponse, PaginationMeta,
    WebhookCreate, WebhookCreatedResponse, WebhookResponse, WebhookUpdate,
    ZoneResponse, ZoneUpsert,
)
from .database import (
    archive_account, restore_account,
    create_account, create_api_key, create_dealer, create_webhook,
    delete_account, delete_api_key, delete_dealer, delete_webhook, delete_zone,
    enqueue_webhook_delivery,
    get_account, get_accounts, get_api_key_by_hash, get_api_keys,
    get_dealer, get_dealers, get_event, get_event_stats, get_events,
    get_events_since, get_latest_event_id, get_last_heartbeat,
    get_recent_critical_events, get_webhook, get_webhooks,
    get_zones,
    next_account_id, next_dealer_prefix, next_linecard,
    update_account, update_api_key_last_used, update_dealer, update_webhook,
    upsert_zone,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_RATE_LIMIT = int(os.environ.get("ARC_API_RATE_LIMIT", "120"))  # per minute
API_RATE_WINDOW = 60  # seconds

# OpenAPI tag metadata (drives Swagger UI grouping)
TAGS_METADATA = [
    {"name": "API Keys", "description": "Create and manage API keys for authentication."},
    {"name": "Dealers", "description": "Manage dealer accounts. **Admin keys only.**"},
    {"name": "Accounts", "description": "Manage alarm system accounts (customer properties). Dealer-scoped."},
    {"name": "Events", "description": "Access alarm event history and live stream. Dealer-scoped."},
    {"name": "Zones", "description": "Label alarm zones within accounts. Dealer-scoped."},
    {"name": "Webhooks", "description": "Configure webhook endpoints for event forwarding. Dealer-scoped."},
    {"name": "SIA Codes", "description": (
        "Reference for SIA DC-09 alarm event codes. These 2-character codes appear in the "
        "`event_code` field of alarm events and webhook payloads.\n\n"
        "**Severity categories used in this system:**\n"
        "- **Critical** (immediate response): `BA` `FA` `PA` `MA` `HA`\n"
        "- **Warning** (medium priority): `TA` `TR` `AT` `YT`\n"
        "- **Info** (normal activity): `OP` `CL` `RX`\n"
        "- **Muted** (supervision): `RP`"
    )},
]

# ---------------------------------------------------------------------------
# SIA DC-09 Code Reference
# ---------------------------------------------------------------------------

# Complete SIA event code table — loaded from pysiaalarm at import time,
# with a hardcoded fallback for the most common codes.
SIA_CODE_TABLE = {}
try:
    from pysiaalarm.utils import SIA_CODES as _raw_codes
    for _code, _obj in _raw_codes.items():
        SIA_CODE_TABLE[_code] = {
            "code": _code,
            "type": getattr(_obj, "type", ""),
            "description": getattr(_obj, "description", ""),
        }
except Exception:
    pass

# Ensure we always have at least the common codes
_COMMON_CODES = {
    "BA": ("Burglary Alarm", "Burglary zone has been violated while armed"),
    "BC": ("Burglary Cancel", "Alarm has been cancelled by authorized user"),
    "BR": ("Burglary Restoral", "Alarm/trouble condition has been eliminated"),
    "BT": ("Burglary Trouble", "Burglary zone disabled by fault"),
    "BX": ("Burglary Test", "Burglary zone activated during testing"),
    "CA": ("Automatic Closing", "System armed automatically"),
    "CL": ("Closing Report", "System armed, normal"),
    "FA": ("Fire Alarm", "Fire condition detected"),
    "FC": ("Fire Cancel", "A Fire Alarm has been cancelled by an authorized person"),
    "FR": ("Fire Restoral", "Alarm/trouble condition has been eliminated"),
    "FT": ("Fire Trouble", "Zone disabled by fault"),
    "FX": ("Fire Test", "Fire zone activated during test"),
    "GA": ("Gas Alarm", "Gas alarm condition detected"),
    "HA": ("Holdup Alarm", "Silent alarm, user under duress"),
    "KA": ("Heat Alarm", "High temperature detected on premise"),
    "MA": ("Medical Alarm", "Emergency assistance request"),
    "OP": ("Opening Report", "Account was disarmed"),
    "PA": ("Panic Alarm", "Emergency assistance request, manually activated"),
    "QA": ("Emergency Alarm", "Emergency assistance request"),
    "RP": ("Automatic Test", "Automatic communication test report"),
    "RR": ("Power Up", "System lost power, is now restored"),
    "RX": ("Manual Test", "Manual communication test report"),
    "SA": ("Sprinkler Alarm", "Sprinkler flow condition exists"),
    "TA": ("Tamper Alarm", "Alarm equipment enclosure opened"),
    "TR": ("Tamper Restoral", "Alarm equipment enclosure has been closed"),
    "AT": ("AC Trouble", "AC power has been failed"),
    "AR": ("AC Restoral", "AC power has been restored"),
    "WA": ("Water Alarm", "Water detected at protected premises"),
    "YT": ("System Battery Trouble", "Low battery in control/communicator"),
    "ZA": ("Freeze Alarm", "Low temperature detected at premises"),
}
for _c, (_t, _d) in _COMMON_CODES.items():
    if _c not in SIA_CODE_TABLE:
        SIA_CODE_TABLE[_c] = {"code": _c, "type": _t, "description": _d}

router = APIRouter(prefix="/api/v1")

# ---------------------------------------------------------------------------
# API key authentication
# ---------------------------------------------------------------------------

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# In-memory rate limiter: {key_prefix: [timestamp, ...]}
_rate_buckets: dict[int, list[float]] = defaultdict(list)


def _is_rate_limited(key_id: int) -> bool:
    """Check if an API key has exceeded the rate limit."""
    now = time.monotonic()
    bucket = _rate_buckets[key_id]
    # Purge old entries
    cutoff = now - API_RATE_WINDOW
    _rate_buckets[prefix] = bucket = [t for t in bucket if t > cutoff]
    if len(bucket) >= API_RATE_LIMIT:
        return True
    bucket.append(now)
    return False


async def get_api_user(
    request: Request,
    api_key: Optional[str] = Depends(api_key_header),
) -> dict:
    """Validate API key and return auth context.

    Returns dict with: key_id, dealer_id (None for admin), is_admin, permissions.
    """
    # Also check query param (needed for SSE EventSource which can't set headers)
    if not api_key:
        api_key = request.query_params.get("api_key")

    if not api_key:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "MISSING_KEY", "message": "X-API-Key header or api_key query parameter required"}},
        )
    if not api_key.startswith("arc_") or len(api_key) != 52:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "INVALID_KEY", "message": "Invalid API key format"}},
        )

    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    record = get_api_key_by_hash(key_hash)

    if not record or not record["enabled"]:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "INVALID_KEY", "message": "Invalid or disabled API key"}},
        )

    if _is_rate_limited(record["id"]):
        raise HTTPException(
            status_code=429,
            detail={"error": {"code": "RATE_LIMITED", "message": f"Rate limit exceeded ({API_RATE_LIMIT}/min)"}},
        )

    # Update last_used timestamp (best-effort, don't fail the request)
    try:
        update_api_key_last_used(record["id"])
    except Exception:
        pass

    return {
        "key_id": record["id"],
        "dealer_id": record["dealer_id"],
        "is_admin": record["dealer_id"] is None,
        "permissions": record["permissions"],
    }


async def require_api_admin(auth: dict = Depends(get_api_user)) -> dict:
    """Dependency that requires an admin-level API key."""
    if not auth["is_admin"]:
        raise HTTPException(
            status_code=403,
            detail={"error": {"code": "FORBIDDEN", "message": "Admin API key required"}},
        )
    return auth


def _scope_dealer_id(auth: dict, body_dealer_id: Optional[int] = None) -> int:
    """Resolve the effective dealer_id for scoped operations.

    Dealer keys: always use their own dealer_id.
    Admin keys: must provide dealer_id in request body.
    """
    if not auth["is_admin"]:
        return auth["dealer_id"]
    if body_dealer_id is not None:
        return body_dealer_id
    return None  # admin with no filter = all dealers


def _require_dealer_id(auth: dict, body_dealer_id: Optional[int] = None) -> int:
    """Like _scope_dealer_id but raises if no dealer_id can be determined."""
    did = _scope_dealer_id(auth, body_dealer_id)
    if did is None:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "VALIDATION_ERROR", "message": "dealer_id is required for admin API keys"}},
        )
    return did


def _paginate(data: list, total: int, page: int, per_page: int) -> dict:
    """Build paginated response dict."""
    return {
        "data": data,
        "meta": {
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, math.ceil(total / per_page)),
        },
    }


# ============================================================================
# API KEY ENDPOINTS
# ============================================================================

@router.get("/api-keys", tags=["API Keys"], summary="List API keys",
            response_model=DataResponse, responses={401: {"model": ErrorResponse}})
async def list_api_keys(auth: dict = Depends(get_api_user)):
    """List API keys visible to the caller.

    - **Admin keys**: see all API keys.
    - **Dealer keys**: see only their own dealer's keys.
    """
    dealer_id = None if auth["is_admin"] else auth["dealer_id"]
    keys = get_api_keys(dealer_id=dealer_id)
    return {"data": keys}


@router.post("/api-keys", tags=["API Keys"], summary="Create API key",
             response_model=DataResponse, status_code=201,
             responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}})
async def create_api_key_endpoint(body: ApiKeyCreate, auth: dict = Depends(require_api_admin)):
    """Create a new API key. **Admin only.**

    The raw key is returned **once** in the response. Store it securely --
    it cannot be retrieved again.

    Set `dealer_id` to scope the key to a specific dealer, or omit for an admin-level key.
    """
    raw_key = "arc_" + secrets.token_hex(24)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:8]

    key_id = create_api_key(
        key_hash=key_hash,
        key_prefix=key_prefix,
        dealer_id=body.dealer_id,
        name=body.name,
    )
    return {"data": {
        "id": key_id,
        "key": raw_key,
        "key_prefix": key_prefix,
        "dealer_id": body.dealer_id,
        "name": body.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }}


@router.delete("/api-keys/{key_id}", tags=["API Keys"], summary="Revoke API key",
               responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def revoke_api_key(key_id: int, auth: dict = Depends(get_api_user)):
    """Revoke (delete) an API key.

    - **Admin keys**: can revoke any key.
    - **Dealer keys**: can only revoke their own dealer's keys.
    """
    dealer_id = None if auth["is_admin"] else auth["dealer_id"]
    delete_api_key(key_id, dealer_id=dealer_id)
    return {"data": {"message": "API key revoked"}}


# ============================================================================
# DEALER ENDPOINTS
# ============================================================================

@router.get("/dealers", tags=["Dealers"], summary="List all dealers",
            response_model=DataResponse, responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}})
async def list_dealers(auth: dict = Depends(require_api_admin)):
    """List all dealer accounts. **Admin only.**"""
    dealers = get_dealers()
    return {"data": dealers}


@router.post("/dealers", tags=["Dealers"], summary="Create dealer",
             response_model=DataResponse, status_code=201,
             responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}})
async def create_dealer_endpoint(body: DealerCreate, auth: dict = Depends(require_api_admin)):
    """Create a new dealer account. **Admin only.**

    A unique 8-hex linecard/DNIS is automatically assigned for signal routing.
    """
    linecard = next_linecard()
    dealer_id = create_dealer(
        prefix="000", dnis=linecard, name=body.name,
        phone=body.phone, email=body.email, notes=body.notes,
    )
    dealer = get_dealer(dealer_id)
    return {"data": dealer}


@router.get("/dealers/{dealer_id}", tags=["Dealers"], summary="Get dealer",
            response_model=DataResponse,
            responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def get_dealer_endpoint(dealer_id: int, auth: dict = Depends(require_api_admin)):
    """Get a single dealer by ID. **Admin only.**"""
    dealer = get_dealer(dealer_id)
    if not dealer:
        raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": "Dealer not found"}})
    return {"data": dealer}


@router.patch("/dealers/{dealer_id}", tags=["Dealers"], summary="Update dealer",
              response_model=DataResponse,
              responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def update_dealer_endpoint(dealer_id: int, body: DealerUpdate, auth: dict = Depends(require_api_admin)):
    """Partially update a dealer. **Admin only.** Only provided fields are changed."""
    dealer = get_dealer(dealer_id)
    if not dealer:
        raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": "Dealer not found"}})
    update_dealer(
        dealer_id,
        name=body.name, phone=body.phone, email=body.email,
        notes=body.notes, enabled=body.enabled,
    )
    return {"data": get_dealer(dealer_id)}


@router.delete("/dealers/{dealer_id}", tags=["Dealers"], summary="Delete dealer",
               responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def delete_dealer_endpoint(dealer_id: int, auth: dict = Depends(require_api_admin)):
    """Delete a dealer and cascade-remove associated users. **Admin only.**

    Accounts and events are unlinked (dealer_id set to NULL), not deleted.
    """
    dealer = get_dealer(dealer_id)
    if not dealer:
        raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": "Dealer not found"}})
    delete_dealer(dealer_id)
    return {"data": {"message": "Dealer deleted"}}


# ============================================================================
# ACCOUNT ENDPOINTS
# ============================================================================

@router.get("/accounts", tags=["Accounts"], summary="List accounts",
            response_model=PaginatedResponse, responses={401: {"model": ErrorResponse}})
async def list_accounts(
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(50, ge=1, le=200, description="Items per page"),
    include_archived: bool = Query(False, description="Include archived (soft-deleted) accounts"),
    auth: dict = Depends(get_api_user),
):
    """List alarm system accounts.

    - **Dealer keys**: only see accounts belonging to their dealer.
    - **Admin keys**: see all accounts across all dealers.

    Set `include_archived=true` to include soft-deleted accounts.
    """
    dealer_id = _scope_dealer_id(auth)
    accounts = get_accounts(dealer_id=dealer_id, include_archived=include_archived)
    total = len(accounts)
    start = (page - 1) * per_page
    page_data = accounts[start:start + per_page]
    return _paginate(page_data, total, page, per_page)


@router.post("/accounts", tags=["Accounts"], summary="Create account",
             response_model=DataResponse, status_code=201,
             responses={401: {"model": ErrorResponse}, 409: {"model": ErrorResponse}})
async def create_account_endpoint(body: AccountCreate, auth: dict = Depends(get_api_user)):
    """Create a new alarm system account.

    If `account_id` is omitted, the next available hex ID is auto-generated.
    Dealer keys automatically assign the account to their dealer.
    Admin keys must provide `dealer_id`.
    """
    did = _require_dealer_id(auth, body.dealer_id)
    account_id = body.account_id
    if not account_id:
        account_id = next_account_id(did)
    else:
        account_id = account_id.upper()

    # Check for duplicate
    existing = get_account(account_id, dealer_id=did)
    if existing:
        raise HTTPException(status_code=409, detail={"error": {"code": "CONFLICT", "message": f"Account {account_id} already exists for this dealer"}})

    try:
        create_account(
            account_id=account_id, name=body.name,
            address=body.address, phone=body.phone,
            email=body.email, notes=body.notes,
            dealer_id=did,
        )
    except Exception as e:
        raise HTTPException(status_code=409, detail={"error": {"code": "CONFLICT", "message": str(e)}})

    acct = get_account(account_id, dealer_id=did)
    return {"data": acct}


@router.get("/accounts/{account_id}", tags=["Accounts"], summary="Get account",
            response_model=DataResponse, responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def get_account_endpoint(account_id: str, auth: dict = Depends(get_api_user)):
    """Get a single account by ID."""
    dealer_id = _scope_dealer_id(auth)
    acct = get_account(account_id.upper(), dealer_id=dealer_id)
    if not acct:
        raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": f"Account {account_id} not found"}})
    return {"data": acct}


@router.patch("/accounts/{account_id}", tags=["Accounts"], summary="Update account",
              response_model=DataResponse, responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def update_account_endpoint(account_id: str, body: AccountUpdate, auth: dict = Depends(get_api_user)):
    """Partially update an account. Only provided fields are changed."""
    dealer_id = _scope_dealer_id(auth)
    acct = get_account(account_id.upper(), dealer_id=dealer_id)
    if not acct:
        raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": f"Account {account_id} not found"}})

    # Merge: use existing values for fields not provided
    update_account(
        account_id=account_id.upper(),
        name=body.name if body.name is not None else acct["name"],
        address=body.address if body.address is not None else acct.get("address", ""),
        phone=body.phone if body.phone is not None else acct.get("phone", ""),
        email=body.email if body.email is not None else acct.get("email", ""),
        notes=body.notes if body.notes is not None else acct.get("notes", ""),
        dealer_id=dealer_id,
    )
    return {"data": get_account(account_id.upper(), dealer_id=dealer_id)}


@router.post("/accounts/{account_id}/archive", tags=["Accounts"], summary="Archive account",
             response_model=DataResponse, responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def archive_account_endpoint(account_id: str, auth: dict = Depends(get_api_user)):
    """Soft-delete an account. Sets `archived_at` timestamp.

    Archived accounts are hidden from default list queries but can be
    restored. Historical events are preserved.
    """
    dealer_id = _scope_dealer_id(auth)
    acct = get_account(account_id.upper(), dealer_id=dealer_id)
    if not acct:
        raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": f"Account {account_id} not found"}})
    archive_account(account_id.upper(), dealer_id=dealer_id)
    return {"data": get_account(account_id.upper(), dealer_id=dealer_id)}


@router.post("/accounts/{account_id}/restore", tags=["Accounts"], summary="Restore account",
             response_model=DataResponse, responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def restore_account_endpoint(account_id: str, auth: dict = Depends(get_api_user)):
    """Restore a previously archived account. Clears `archived_at`."""
    dealer_id = _scope_dealer_id(auth)
    acct = get_account(account_id.upper(), dealer_id=dealer_id)
    if not acct:
        raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": f"Account {account_id} not found"}})
    restore_account(account_id.upper(), dealer_id=dealer_id)
    return {"data": get_account(account_id.upper(), dealer_id=dealer_id)}


@router.delete("/accounts/{account_id}", tags=["Accounts"], summary="Hard-delete account",
               responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def hard_delete_account(account_id: str, auth: dict = Depends(require_api_admin)):
    """Permanently delete an account. **Admin only.**

    Prefer `/accounts/{id}/archive` for soft-delete. This action is irreversible.
    """
    acct = get_account(account_id.upper())
    if not acct:
        raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": f"Account {account_id} not found"}})
    delete_account(account_id.upper())
    return {"data": {"message": "Account permanently deleted"}}


# ============================================================================
# EVENT ENDPOINTS
# ============================================================================

@router.get("/events/stats", tags=["Events"], summary="Get event statistics",
            response_model=DataResponse, responses={401: {"model": ErrorResponse}})
async def event_stats(auth: dict = Depends(get_api_user)):
    """Get dashboard summary statistics (total events, events today, alarms today, active accounts)."""
    dealer_id = _scope_dealer_id(auth)
    stats = get_event_stats(dealer_id=dealer_id)
    heartbeat = get_last_heartbeat(dealer_id=dealer_id)
    stats["last_heartbeat"] = heartbeat["received_at"] if heartbeat else None
    return {"data": stats}


@router.get("/events/stream", tags=["Events"], summary="Live event stream (SSE)",
            responses={401: {"model": ErrorResponse}})
async def event_stream(
    auth: dict = Depends(get_api_user),
    exclude_heartbeats: bool = Query(True, description="Exclude RP heartbeat events"),
):
    """Server-Sent Events (SSE) stream of live alarm events.

    Connect with an `EventSource` or any SSE client. Each event is a JSON object.

    **Authentication**: Pass `api_key` as a query parameter since EventSource
    doesn't support custom headers:
    ```
    const es = new EventSource('/api/v1/events/stream?api_key=arc_...');
    es.onmessage = (e) => console.log(JSON.parse(e.data));
    ```
    """
    dealer_id = _scope_dealer_id(auth)

    async def generate():
        last_id = get_latest_event_id()
        yield "event: connected\ndata: {\"status\": \"connected\"}\n\n"
        while True:
            await asyncio.sleep(1.5)
            try:
                new_events = get_events_since(last_id, dealer_id=dealer_id)
                if new_events:
                    last_id = new_events[-1]["id"]
                    for evt in new_events:
                        if exclude_heartbeats and evt.get("event_code") == "RP":
                            continue
                        yield f"event: alarm\ndata: {json.dumps(evt, default=str)}\n\n"
            except Exception:
                yield "event: error\ndata: {\"error\": \"stream interrupted\"}\n\n"
                break

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/events/{event_id}", tags=["Events"], summary="Get single event",
            response_model=DataResponse, responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def get_event_endpoint(event_id: int, auth: dict = Depends(get_api_user)):
    """Get a single alarm event by ID."""
    dealer_id = _scope_dealer_id(auth)
    evt = get_event(event_id, dealer_id=dealer_id)
    if not evt:
        raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": "Event not found"}})
    return {"data": evt}


@router.get("/events", tags=["Events"], summary="List events",
            response_model=PaginatedResponse, responses={401: {"model": ErrorResponse}})
async def list_events(
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(50, ge=1, le=200, description="Items per page"),
    account: Optional[str] = Query(None, description="Filter by account ID"),
    code: Optional[str] = Query(None, description="Filter by SIA event code (e.g. BA, FA, OP)"),
    zone: Optional[str] = Query(None, description="Filter by zone number"),
    since: Optional[str] = Query(None, description="Only events after this ISO timestamp"),
    exclude_heartbeats: bool = Query(True, description="Exclude RP heartbeat events"),
    auth: dict = Depends(get_api_user),
):
    """List alarm events with pagination and optional filters.

    Events are returned newest-first. Use `since` for incremental polling.
    """
    dealer_id = _scope_dealer_id(auth)
    exclude_codes = ["RP"] if exclude_heartbeats else None
    offset = (page - 1) * per_page
    events, total = get_events(
        limit=per_page, offset=offset,
        account=account, code=code, zone=zone, since=since,
        exclude_codes=exclude_codes, dealer_id=dealer_id,
    )
    return _paginate(events, total, page, per_page)


# ============================================================================
# ZONE ENDPOINTS
# ============================================================================

@router.get("/accounts/{account_id}/zones", tags=["Zones"], summary="List zones",
            response_model=DataResponse, responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def list_zones(account_id: str, auth: dict = Depends(get_api_user)):
    """List all named zones for an account."""
    dealer_id = _scope_dealer_id(auth)
    acct = get_account(account_id.upper(), dealer_id=dealer_id)
    if not acct:
        raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": f"Account {account_id} not found"}})
    zones = get_zones(account_id.upper(), dealer_id=dealer_id)
    return {"data": zones}


@router.put("/accounts/{account_id}/zones/{zone_number}", tags=["Zones"],
            summary="Create or update zone",
            response_model=DataResponse, responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def upsert_zone_endpoint(account_id: str, zone_number: str, body: ZoneUpsert,
                                auth: dict = Depends(get_api_user)):
    """Create or update a zone label for an account.

    If the zone already exists, its name is updated. Otherwise a new zone is created.
    """
    dealer_id = _scope_dealer_id(auth)
    acct = get_account(account_id.upper(), dealer_id=dealer_id)
    if not acct:
        raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": f"Account {account_id} not found"}})
    upsert_zone(account_id.upper(), zone_number, body.zone_name, dealer_id=dealer_id)
    zones = get_zones(account_id.upper(), dealer_id=dealer_id)
    zone = next((z for z in zones if z["zone_number"] == zone_number), None)
    return {"data": zone}


@router.delete("/accounts/{account_id}/zones/{zone_number}", tags=["Zones"],
               summary="Delete zone",
               responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def delete_zone_endpoint(account_id: str, zone_number: str,
                                auth: dict = Depends(get_api_user)):
    """Delete a zone label from an account."""
    dealer_id = _scope_dealer_id(auth)
    acct = get_account(account_id.upper(), dealer_id=dealer_id)
    if not acct:
        raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": f"Account {account_id} not found"}})
    delete_zone(account_id.upper(), zone_number, dealer_id=dealer_id)
    return {"data": {"message": f"Zone {zone_number} deleted"}}


# ============================================================================
# WEBHOOK ENDPOINTS
# ============================================================================

@router.get("/webhooks", tags=["Webhooks"], summary="List webhooks",
            response_model=DataResponse, responses={401: {"model": ErrorResponse}})
async def list_webhooks(auth: dict = Depends(get_api_user)):
    """List configured webhooks.

    - **Dealer keys**: see only their dealer's webhooks.
    - **Admin keys**: see all webhooks.
    """
    dealer_id = _scope_dealer_id(auth)
    webhooks = get_webhooks(dealer_id=dealer_id)
    # Strip secrets from response
    for wh in webhooks:
        wh.pop("secret", None)
    return {"data": webhooks}


@router.post("/webhooks", tags=["Webhooks"], summary="Create webhook",
             response_model=DataResponse, status_code=201,
             responses={401: {"model": ErrorResponse}})
async def create_webhook_endpoint(body: WebhookCreate, auth: dict = Depends(get_api_user)):
    """Create a new webhook endpoint.

    The HMAC signing `secret` is returned **once** in this response.
    Store it securely -- it cannot be retrieved again.

    Admin keys must provide `dealer_id`. Dealer keys auto-assign.
    """
    did = _require_dealer_id(auth, body.dealer_id)

    # Use shared ARC_WEBHOOK_SECRET for hmac (default), or provided secret for bearer
    from . import config as _cfg
    if body.auth_type == "bearer":
        if not body.secret:
            raise HTTPException(status_code=422, detail={
                "error": {"code": "VALIDATION_ERROR", "message": "secret (Bearer token) is required for auth_type='bearer'"}
            })
        secret = body.secret
    else:
        secret = body.secret or _cfg.WEBHOOK_SECRET or secrets.token_hex(32)

    wh_id = create_webhook(
        dealer_id=did, url=body.url, secret=secret,
        description=body.description, event_filter=body.event_filter,
        auth_type=body.auth_type, account_filter=body.account_filter,
    )
    wh = get_webhook(wh_id, dealer_id=did)
    # Include secret in creation response only
    wh["secret"] = secret
    return {"data": wh}


@router.patch("/webhooks/{webhook_id}", tags=["Webhooks"], summary="Update webhook",
              response_model=DataResponse,
              responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def update_webhook_endpoint(webhook_id: int, body: WebhookUpdate,
                                   auth: dict = Depends(get_api_user)):
    """Partially update a webhook. Only provided fields are changed."""
    dealer_id = _scope_dealer_id(auth)
    wh = get_webhook(webhook_id, dealer_id=dealer_id)
    if not wh:
        raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": "Webhook not found"}})
    update_webhook(
        webhook_id, dealer_id=dealer_id,
        url=body.url, description=body.description,
        event_filter=body.event_filter, enabled=body.enabled,
        auth_type=body.auth_type, account_filter=body.account_filter,
    )
    result = get_webhook(webhook_id, dealer_id=dealer_id)
    result.pop("secret", None)
    return {"data": result}


@router.delete("/webhooks/{webhook_id}", tags=["Webhooks"], summary="Delete webhook",
               responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def delete_webhook_endpoint(webhook_id: int, auth: dict = Depends(get_api_user)):
    """Delete a webhook and all its delivery history."""
    dealer_id = _scope_dealer_id(auth)
    wh = get_webhook(webhook_id, dealer_id=dealer_id)
    if not wh:
        raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": "Webhook not found"}})
    delete_webhook(webhook_id, dealer_id=dealer_id)
    return {"data": {"message": "Webhook deleted"}}


@router.post("/webhooks/{webhook_id}/test", tags=["Webhooks"], summary="Send test webhook",
             response_model=DataResponse,
             responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
async def test_webhook_endpoint(webhook_id: int, auth: dict = Depends(get_api_user)):
    """Send a test payload to verify webhook endpoint connectivity.

    The test event uses `code: "TEST"` and `event_id: 0` so your receiver
    can distinguish it from real alarms.
    """
    dealer_id = _scope_dealer_id(auth)
    wh = get_webhook(webhook_id, dealer_id=dealer_id)
    if not wh:
        raise HTTPException(status_code=404, detail={"error": {"code": "NOT_FOUND", "message": "Webhook not found"}})

    dealer = get_dealer(wh["dealer_id"])
    # Use the account_filter as the account_id (the short account portion)
    acct_id = wh.get("account_filter") or "TEST"
    acct = get_account(acct_id, dealer_id=wh["dealer_id"]) if acct_id != "TEST" else None
    import calendar
    unix_ts = int(calendar.timegm(datetime.now(timezone.utc).utctimetuple()))
    test_payload = json.dumps({
        "event_id": "evt_test_0",
        "account_id": acct_id,
        "event_code": "TEST",
        "zone": "",
        "zone_name": "",
        "timestamp": unix_ts,
        "description": "Webhook connectivity test",
        "dealer_id": str(wh["dealer_id"]),
        "account_name": acct["name"] if acct else "Test Account",
    })
    enqueue_webhook_delivery(wh["id"], 0, test_payload)
    return {"data": {"message": f"Test webhook queued for delivery to {wh['url']}"}}


# ============================================================================
# SIA CODE REFERENCE
# ============================================================================

@router.get("/sia-codes", tags=["SIA Codes"], summary="List all SIA event codes",
            response_model=DataResponse)
async def list_sia_codes(
    category: Optional[str] = Query(
        None,
        description="Filter by category: `alarm`, `trouble`, `restoral`, `bypass`, "
                    "`open_close`, `test`, `supervisory`, `fire`, `burglary`, `medical`, "
                    "`panic`, `tamper`, `access`",
    ),
):
    """Complete reference of SIA DC-09 alarm event codes.

    These 2-character codes appear in the `event_code` field of events and webhook payloads.
    No authentication required.

    **Common codes you'll encounter:**

    | Code | Type | Meaning |
    |------|------|---------|
    | `BA` | Burglary Alarm | Intrusion detected while armed |
    | `FA` | Fire Alarm | Fire condition detected |
    | `PA` | Panic Alarm | Manual emergency request |
    | `MA` | Medical Alarm | Medical emergency request |
    | `HA` | Holdup Alarm | Silent duress alarm |
    | `OP` | Opening Report | System disarmed |
    | `CL` | Closing Report | System armed |
    | `RP` | Automatic Test | Heartbeat / supervision signal |
    | `TA` | Tamper Alarm | Equipment enclosure opened |
    | `WA` | Water Alarm | Water/leak detected |
    | `GA` | Gas Alarm | Gas detected |
    | `ZA` | Freeze Alarm | Low temperature detected |

    **Code naming patterns:**
    - `xA` = Alarm (e.g. `BA`, `FA`, `PA`)
    - `xR` = Restoral (e.g. `BR`, `FR`, `PR`)
    - `xT` = Trouble (e.g. `BT`, `FT`, `PT`)
    - `xB` = Bypass (e.g. `BB`, `FB`, `PB`)
    - `xH` = Alarm Restore (e.g. `BH`, `FH`, `PH`)
    - `xJ` = Trouble Restore (e.g. `BJ`, `FJ`, `PJ`)
    - `xS` = Supervisory (e.g. `BS`, `FS`, `PS`)
    - `xU` = Unbypass (e.g. `BU`, `FU`, `PU`)
    - `xX` = Test (e.g. `BX`, `FX`, `TX`)
    """
    codes = list(SIA_CODE_TABLE.values())

    if category:
        cat = category.lower()
        _CATEGORY_FILTERS = {
            "alarm": lambda c: c["code"].endswith("A") and c["code"] != "CA",
            "trouble": lambda c: c["code"].endswith("T") and c["code"] not in ("AT", "CT"),
            "restoral": lambda c: c["code"].endswith("R") and c["code"] not in ("CR",),
            "bypass": lambda c: c["code"].endswith("B"),
            "open_close": lambda c: c["code"] in ("OP", "CL", "CA", "CP", "CQ", "OA", "OQ", "OR", "OS", "OK", "OJ", "CF", "CI", "CJ", "CK", "CT", "OT"),
            "test": lambda c: c["code"].endswith("X") or c["code"] in ("RP", "RX", "TS", "TE", "TC"),
            "supervisory": lambda c: c["code"].endswith("S") and len(c["code"]) == 2,
            "fire": lambda c: c["code"].startswith("F"),
            "burglary": lambda c: c["code"].startswith("B"),
            "medical": lambda c: c["code"].startswith("M"),
            "panic": lambda c: c["code"].startswith("P"),
            "tamper": lambda c: c["code"].startswith("T"),
            "access": lambda c: c["code"].startswith("D"),
        }
        filt = _CATEGORY_FILTERS.get(cat)
        if filt:
            codes = [c for c in codes if filt(c)]

    codes.sort(key=lambda c: c["code"])
    return {"data": codes, "meta": {"total": len(codes)}}


@router.get("/sia-codes/{code}", tags=["SIA Codes"], summary="Look up a single SIA code",
            response_model=DataResponse,
            responses={404: {"model": ErrorResponse}})
async def get_sia_code(code: str):
    """Look up a specific SIA event code by its 2-character identifier.

    No authentication required.
    """
    entry = SIA_CODE_TABLE.get(code.upper())
    if not entry:
        raise HTTPException(status_code=404, detail={
            "error": {"code": "NOT_FOUND", "message": f"SIA code '{code.upper()}' not found"}
        })
    return {"data": entry}
