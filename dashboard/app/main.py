"""ARC Dashboard - FastAPI Application."""
import asyncio
import html
import json as _json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from . import config
from .auth import (
    get_current_user,
    authenticate_user,
    create_session_token,
    ensure_admin_exists,
    is_rate_limited,
    record_failed_login,
    clear_failed_logins,
    change_password,
    get_users,
    create_user,
    delete_user,
    verify_session_token,
)
from .database import (
    migrate_db,
    get_events,
    get_event_stats,
    get_latest_event_id,
    get_events_since,
    get_accounts,
    get_account,
    create_account,
    update_account,
    delete_account,
    get_recent_critical_events,
    get_last_heartbeat,
    get_dealers,
    get_dealer,
    create_dealer,
    update_dealer,
    delete_dealer,
    get_dealer_user,
    next_dealer_prefix,
    next_account_id,
    get_zones,
    upsert_zone,
    delete_zone,
    get_account_name_map,
    get_zone_name_map,
    get_webhooks,
    get_webhook,
    create_webhook,
    update_webhook,
    update_webhook_secret,
    delete_webhook,
    get_delivery_log,
    get_delivery_stats,
    get_webhook_stats_all,
    enqueue_webhook_delivery,
    DEFAULT_DNIS,
)
from .webhook_worker import WebhookWorker
from .api import router as api_router, TAGS_METADATA

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("arc-dashboard")

# Route prefix for admin portal
P = "/alarm-admin"

# --- Event code severity ---
EVENT_SEVERITY = {
    "BA": "critical", "FA": "critical", "PA": "critical",
    "MA": "critical", "HA": "critical",
    "TA": "warning", "TR": "warning", "AT": "warning", "YT": "warning",
    "OP": "info", "CL": "info",
    "RP": "muted",
    "RX": "info",
}

ACCOUNT_ID_RE = re.compile(r"^[0-9A-Fa-f]{1,16}$")
PREFIX_RE = re.compile(r"^[0-9]{3}$")
DNIS_RE = re.compile(r"^[0-9A-Fa-f]{1,8}$")
WEBHOOK_URL_RE = re.compile(r"^https://[^\s]+$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def get_severity(code):
    return EVENT_SEVERITY.get(code, "info")


def normalize_account(account_id, prefix=None):
    """Strip the dealer prefix from an account ID for display.

    SIA DC-09 sends prefix+account (e.g. prefix '001' + account '234' = '001234').
    Some event types strip leading zeros (e.g. '1234' where '1' is stripped '001').
    """
    if not account_id:
        return ""
    if not prefix:
        return account_id
    if account_id.startswith(prefix):
        return account_id[len(prefix):]
    short_prefix = prefix.lstrip("0")
    if short_prefix and account_id.startswith(short_prefix):
        return account_id[len(short_prefix):]
    return account_id


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _safe_int(value, default=1, minimum=1, maximum=10000):
    if not value:
        return default
    try:
        return max(minimum, min(int(value), maximum))
    except (ValueError, TypeError):
        return default


def _generate_csrf_token() -> str:
    return secrets.token_hex(32)


def _get_csrf_token(request: Request) -> str:
    return request.cookies.get("csrf_token") or _generate_csrf_token()


def _verify_csrf(request_token, cookie_token) -> bool:
    if not request_token or not cookie_token:
        return False
    return secrets.compare_digest(str(request_token), str(cookie_token))


def _set_csrf_cookie(response, csrf, is_debug=False):
    response.set_cookie("csrf_token", csrf, httponly=True, samesite="strict", secure=not is_debug)
    return response


def _safe_ws_event(event, prefix=None, account_names=None, zone_names=None):
    """Build a safe dict for WebSocket JSON from a DB event row."""
    code = event.get("event_code", "")
    acct_id = normalize_account(str(event.get("account_id", "")), prefix)
    zone = str(event.get("zone", "") or "")
    acct_name = ""
    if account_names:
        acct_name = account_names.get(acct_id, "")
        if not acct_name:
            acct_name = account_names.get(str(event.get("account_id", "")), "")
    zone_name = ""
    if zone_names and zone:
        zone_name = zone_names.get((acct_id, zone), "")
    return {
        "id": event.get("id"),
        "received_at": str(event.get("received_at", "")),
        "account_id": html.escape(acct_id),
        "account_name": html.escape(acct_name),
        "event_code": html.escape(str(code)),
        "event_type": html.escape(str(event.get("event_type", "") or "")),
        "event_desc": html.escape(str(event.get("event_desc", "") or "")),
        "zone": html.escape(zone),
        "zone_name": html.escape(zone_name),
        "partition": html.escape(str(event.get("partition", "") or "")),
        "message": html.escape(str(event.get("message", "") or "")),
        "severity": get_severity(code),
        "type": "heartbeat" if code == "RP" else "event",
    }


def _form_str(form, key, max_length=500):
    """Extract a stripped string from a form, defaulting to empty. Truncates to max_length."""
    return form.get(key, "").strip()[:max_length]


# ---------------------------------------------------------------------------
# Lifecycle & App setup
# ---------------------------------------------------------------------------

webhook_worker = WebhookWorker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate_db()
    if ensure_admin_exists():
        logger.warning("Default admin created (admin/changeme) — CHANGE THIS IMMEDIATELY")
    await webhook_worker.start()
    logger.info("ARC Dashboard started — DB: %s, prefix: %s", config.DB_PATH, P)
    yield
    await webhook_worker.stop()


app = FastAPI(
    title="ARC Alarm Receiving Center API",
    description=(
        "REST API for the Alarm Receiving Center (ARC). Manage dealers, accounts, "
        "zones, events, webhooks, and API keys. Authenticate with an API key in the "
        "`X-API-Key` header.\n\n"
        "**Dealer-scoped keys** can only access data belonging to their dealer. "
        "**Admin keys** have full access across all dealers."
    ),
    version="1.0.0",
    docs_url=None,  # We serve custom docs below
    redoc_url=None,
    openapi_url="/api/openapi.json",
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
)
app.include_router(api_router)


# Custom OpenAPI schema — only include /api/ routes, exclude dashboard HTML routes
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.openapi.utils import get_openapi


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    # Filter to only /api/ paths
    filtered_paths = {path: ops for path, ops in schema.get("paths", {}).items() if path.startswith("/api/")}
    schema["paths"] = filtered_paths
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


@app.get("/api/docs", include_in_schema=False)
async def custom_swagger_ui():
    return get_swagger_ui_html(
        openapi_url="/api/openapi.json",
        title="ARC API - Swagger UI",
        swagger_js_url="/static/swagger/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger/swagger-ui.css",
        swagger_favicon_url="/static/logo-dark.png",
        swagger_ui_parameters={
            "docExpansion": "list",
            "defaultModelsExpandDepth": 1,
            "filter": True,
            "tryItOutEnabled": True,
        },
    )


@app.get("/api/redoc", include_in_schema=False)
async def custom_redoc():
    return get_redoc_html(
        openapi_url="/api/openapi.json",
        title="ARC API - ReDoc",
        redoc_js_url="https://unpkg.com/redoc@next/bundles/redoc.standalone.js",
        redoc_favicon_url="/static/logo-dark.png",
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://unpkg.com https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data: https://fastapi.tiangolo.com; "
            "connect-src 'self' wss: ws:; "
            "frame-ancestors 'none'"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)

BASE_DIR = Path(__file__).parent
app.mount(f"{P}/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="dealer_static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["get_severity"] = get_severity
templates.env.globals["normalize_account"] = normalize_account
templates.env.globals["P"] = P


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def require_admin(request: Request):
    """Return user dict if admin/operator, else None."""
    user = get_current_user(request)
    if not user or user.get("role") not in ("admin", "operator"):
        return None
    return user


def require_dealer(request: Request):
    """Return (user, dealer) if dealer user, else (None, None)."""
    user = get_current_user(request)
    if not user or user.get("role") != "dealer" or not user.get("dealer_id"):
        return None, None
    dealer = get_dealer(user["dealer_id"])
    if not dealer or not dealer.get("enabled"):
        return None, None
    return user, dealer


def _login_flow(request, form, username, password, ip, template, redirect_to, role_check=None):
    """Shared login logic for admin and dealer portals."""
    csrf_form = form.get("csrf_token", "")
    csrf_cookie = request.cookies.get("csrf_token", "")

    if not _verify_csrf(csrf_form, csrf_cookie):
        csrf = _generate_csrf_token()
        resp = templates.TemplateResponse(template, {
            "request": request, "error": "Invalid request. Please try again.", "csrf_token": csrf
        }, status_code=403)
        return _set_csrf_cookie(resp, csrf, config.DEBUG)

    if is_rate_limited(ip):
        csrf = _generate_csrf_token()
        resp = templates.TemplateResponse(template, {
            "request": request, "error": "Too many login attempts. Try again later.", "csrf_token": csrf
        }, status_code=429)
        return _set_csrf_cookie(resp, csrf, config.DEBUG)

    user = authenticate_user(username, password)
    if not user or (role_check and not role_check(user)):
        record_failed_login(ip)
        csrf = _generate_csrf_token()
        resp = templates.TemplateResponse(template, {
            "request": request, "error": "Invalid credentials", "csrf_token": csrf
        }, status_code=401)
        return _set_csrf_cookie(resp, csrf, config.DEBUG)

    clear_failed_logins(ip)
    logger.info("Login OK for '%s' from %s", username, ip)
    token = create_session_token(user["user_id"], user["username"], user["role"], user.get("dealer_id"))
    response = RedirectResponse(redirect_to, status_code=302)
    response.set_cookie("session", token, max_age=config.SESSION_MAX_AGE,
                         httponly=True, samesite="strict", secure=not config.DEBUG)
    response.delete_cookie("csrf_token")
    return response


def _change_password_flow(request, form, user, template, extra_ctx=None):
    """Shared password-change logic for admin and dealer settings pages."""
    ctx = {"request": request, "user": user, "success": None, "error": None}
    if extra_ctx:
        ctx.update(extra_ctx)

    csrf = _generate_csrf_token()
    ctx["csrf_token"] = csrf

    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        ctx["error"] = "Invalid request."
        resp = templates.TemplateResponse(template, ctx, status_code=403)
        return _set_csrf_cookie(resp, csrf, config.DEBUG)

    current = form.get("current_password", "")
    new = form.get("new_password", "")
    confirm = form.get("confirm_password", "")

    if new != confirm:
        ctx["error"] = "New passwords do not match."
    elif len(new) < 8:
        ctx["error"] = "Password must be at least 8 characters."
    elif not change_password(user["user_id"], current, new):
        ctx["error"] = "Current password is incorrect."
    else:
        ctx["success"] = "Password changed successfully."
        logger.info("Password changed for user '%s'", user["username"])

    resp = templates.TemplateResponse(template, ctx)
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


# ============================================================================
# ADMIN PORTAL — /alarm-admin/...
# ============================================================================

# --- Admin Auth ---
@app.get(f"{P}/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    user = get_current_user(request)
    if user and user.get("role") in ("admin", "operator"):
        return RedirectResponse(f"{P}/", status_code=302)
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("login.html", {"request": request, "error": None, "csrf_token": csrf})
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.post(f"{P}/login")
async def admin_login_submit(request: Request):
    form = await request.form()
    return _login_flow(
        request, form,
        username=_form_str(form, "username"),
        password=form.get("password", ""),
        ip=_client_ip(request),
        template="login.html",
        redirect_to=f"{P}/",
        role_check=lambda u: u.get("role") in ("admin", "operator"),
    )


@app.get(f"{P}/logout")
async def admin_logout():
    response = RedirectResponse(f"{P}/login", status_code=302)
    response.delete_cookie("session")
    return response


# --- Admin Dashboard ---
@app.get(f"{P}/", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    user = require_admin(request)
    if not user:
        return RedirectResponse(f"{P}/login", status_code=302)
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "user": user,
        "stats": get_event_stats(),
        "events": get_events(limit=50, exclude_codes=["RP"])[0],
        "total": get_events(limit=1)[1],
        "latest_id": get_latest_event_id(),
        "critical_events": get_recent_critical_events(hours=1),
        "last_heartbeat": get_last_heartbeat(),
        "account_names": get_account_name_map(),
        "zone_names": get_zone_name_map(),
    })


# --- Admin Events ---
@app.get(f"{P}/events", response_class=HTMLResponse)
async def admin_events_page(request: Request):
    user = require_admin(request)
    if not user:
        return RedirectResponse(f"{P}/login", status_code=302)
    params = request.query_params
    account = params.get("account", "").strip() or None
    code = params.get("code", "").strip().upper() or None
    zone = params.get("zone", "").strip() or None
    show_hb = params.get("show_heartbeats", "").strip() == "1"
    page = _safe_int(params.get("page"), default=1)
    limit = 50
    offset = (page - 1) * limit
    exclude = None if show_hb else ["RP"]
    events, total = get_events(limit=limit, offset=offset, account=account, code=code, zone=zone,
                               exclude_codes=exclude)
    pages = max(1, (total + limit - 1) // limit)
    return templates.TemplateResponse("events.html", {
        "request": request, "user": user, "events": events,
        "total": total, "page": page, "pages": pages,
        "show_heartbeats": show_hb,
        "filters": {"account": account or "", "code": code or "", "zone": zone or ""},
        "account_names": get_account_name_map(),
        "zone_names": get_zone_name_map(),
    })


@app.websocket(f"{P}/events/ws")
async def admin_events_websocket(ws: WebSocket):
    token = ws.cookies.get("session")
    if not token:
        await ws.close(code=4001, reason="Authentication required")
        return
    user = verify_session_token(token)
    if not user or user.get("role") not in ("admin", "operator"):
        await ws.close(code=4001, reason="Invalid session")
        return
    await ws.accept()
    last_id = get_latest_event_id()
    session_start = asyncio.get_event_loop().time()
    acct_names = get_account_name_map()
    z_names = get_zone_name_map()
    cache_refresh = 0
    try:
        while True:
            await asyncio.sleep(1.5)
            # Re-validate session every 5 minutes; hard timeout at SESSION_MAX_AGE
            elapsed = asyncio.get_event_loop().time() - session_start
            if elapsed > config.SESSION_MAX_AGE:
                await ws.close(code=4001, reason="Session expired")
                return
            if int(elapsed) % 300 < 2:
                check = verify_session_token(token)
                if not check:
                    await ws.close(code=4001, reason="Session expired")
                    return
            # Refresh name caches every 30 seconds
            if int(elapsed) - cache_refresh >= 30:
                cache_refresh = int(elapsed)
                acct_names = get_account_name_map()
                z_names = get_zone_name_map()
            new_events = get_events_since(last_id)
            if new_events:
                last_id = new_events[-1]["id"]
                for event in new_events:
                    await ws.send_json(_safe_ws_event(event, account_names=acct_names, zone_names=z_names))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("Admin WebSocket error: %s", e)


# --- Admin Accounts ---
@app.get(f"{P}/accounts", response_class=HTMLResponse)
async def admin_accounts_page(request: Request):
    user = require_admin(request)
    if not user:
        return RedirectResponse(f"{P}/login", status_code=302)
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("accounts.html", {
        "request": request, "user": user, "accounts": get_accounts(),
        "dealers": get_dealers(), "csrf_token": csrf,
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.post(f"{P}/accounts")
async def admin_create_account(request: Request):
    user = require_admin(request)
    if not user:
        return RedirectResponse(f"{P}/login", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse(f"{P}/accounts", status_code=302)
    account_id = _form_str(form, "account_id").upper()
    name = _form_str(form, "name")
    if not ACCOUNT_ID_RE.match(account_id) or not name:
        return RedirectResponse(f"{P}/accounts", status_code=302)
    dealer_id_str = _form_str(form, "dealer_id")
    dealer_id = int(dealer_id_str) if dealer_id_str else None
    try:
        create_account(
            account_id=account_id, name=name,
            address=_form_str(form, "address"),
            phone=_form_str(form, "phone"),
            email=_form_str(form, "email"),
            notes=_form_str(form, "notes"),
            dealer_id=dealer_id,
        )
    except Exception as e:
        logger.error("Error creating account: %s", e)
    return RedirectResponse(f"{P}/accounts", status_code=302)


@app.get(f"{P}/accounts/next-id")
async def admin_next_account_id(request: Request, dealer_id: int | None = None):
    user = require_admin(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if dealer_id is None:
        return JSONResponse({"error": "dealer_id required"}, status_code=400)
    try:
        aid = next_account_id(dealer_id)
    except Exception as e:
        logger.error("Error generating next account ID: %s", e)
        return JSONResponse({"error": "failed"}, status_code=500)
    return JSONResponse({"account_id": aid})


@app.post(f"{P}/accounts/{{account_id}}/edit")
async def admin_edit_account(request: Request, account_id: str):
    user = require_admin(request)
    if not user:
        return RedirectResponse(f"{P}/login", status_code=302)
    if not ACCOUNT_ID_RE.match(account_id):
        return RedirectResponse(f"{P}/accounts", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse(f"{P}/accounts", status_code=302)
    update_account(
        account_id=account_id, name=_form_str(form, "name"),
        address=_form_str(form, "address"), phone=_form_str(form, "phone"),
        email=_form_str(form, "email"), notes=_form_str(form, "notes"),
    )
    if request.headers.get("hx-request"):
        acct, dealer = _resolve_admin_account(account_id)
        csrf = _get_csrf_token(request)
        resp = templates.TemplateResponse("partials/account_details.html", {
            "request": request, "account": acct, "dealer": dealer,
            "base_url": P, "csrf_token": csrf, "success": True,
        })
        return _set_csrf_cookie(resp, csrf, config.DEBUG)
    return RedirectResponse(f"{P}/accounts", status_code=302)


@app.post(f"{P}/accounts/{{account_id}}/delete")
async def admin_delete_account(request: Request, account_id: str):
    user = require_admin(request)
    if not user:
        return RedirectResponse(f"{P}/login", status_code=302)
    if not ACCOUNT_ID_RE.match(account_id):
        return RedirectResponse(f"{P}/accounts", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse(f"{P}/accounts", status_code=302)
    delete_account(account_id)
    return RedirectResponse(f"{P}/accounts", status_code=302)


# --- Admin Account Modal (HTMX partials) ---
def _resolve_admin_account(account_id_raw):
    """Resolve an account from a raw ID that may include dealer prefix."""
    acct = get_account(account_id_raw)
    if acct:
        return acct, None
    # Try stripping known dealer prefixes
    for d in get_dealers():
        prefix = d["prefix"]
        if account_id_raw.startswith(prefix):
            short_id = account_id_raw[len(prefix):]
            acct = get_account(short_id)
            if acct:
                return acct, d
        short_prefix = prefix.lstrip("0")
        if short_prefix and account_id_raw.startswith(short_prefix):
            short_id = account_id_raw[len(short_prefix):]
            acct = get_account(short_id)
            if acct:
                return acct, d
    return None, None


@app.get(f"{P}/accounts/{{account_id}}/modal/details", response_class=HTMLResponse)
async def admin_account_modal_details(request: Request, account_id: str):
    user = require_admin(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    acct, dealer = _resolve_admin_account(account_id)
    if not acct:
        return HTMLResponse("<div class='text-red-400'>Account not found.</div>")
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("partials/account_details.html", {
        "request": request, "account": acct, "dealer": dealer,
        "base_url": P, "csrf_token": csrf, "success": False,
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.get(f"{P}/accounts/{{account_id}}/modal/events", response_class=HTMLResponse)
async def admin_account_modal_events(request: Request, account_id: str):
    user = require_admin(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    offset = _safe_int(request.query_params.get("offset"), default=0, minimum=0)
    limit = 25
    # Events store the raw transmitted account_id (with prefix), so search by what was passed
    events, total = get_events(limit=limit, offset=offset, account=account_id)
    # Also resolve the short account_id for zone lookups
    acct, _ = _resolve_admin_account(account_id)
    short_id = acct["account_id"] if acct else account_id
    zone_names = get_zone_name_map()
    return templates.TemplateResponse("partials/account_events.html", {
        "request": request, "events": events, "total": total,
        "offset": offset, "limit": limit, "account_id": account_id,
        "zone_names": zone_names, "base_url": P,
    })


@app.get(f"{P}/accounts/{{account_id}}/modal/zones", response_class=HTMLResponse)
async def admin_account_modal_zones(request: Request, account_id: str):
    user = require_admin(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    acct, _ = _resolve_admin_account(account_id)
    short_id = acct["account_id"] if acct else account_id
    zones = get_zones(short_id)
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("partials/account_zones.html", {
        "request": request, "zones": zones, "account_id": account_id,
        "base_url": P, "csrf_token": csrf, "success": None,
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.post(f"{P}/accounts/{{account_id}}/zones")
async def admin_upsert_zone(request: Request, account_id: str):
    user = require_admin(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return HTMLResponse("CSRF error", status_code=403)
    acct, _ = _resolve_admin_account(account_id)
    short_id = acct["account_id"] if acct else account_id
    zone_number = _form_str(form, "zone_number")
    zone_name = _form_str(form, "zone_name")
    if zone_number and zone_name:
        upsert_zone(short_id, zone_number, zone_name)
    zones = get_zones(short_id)
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("partials/account_zones.html", {
        "request": request, "zones": zones, "account_id": account_id,
        "base_url": P, "csrf_token": csrf, "success": "Zone saved.",
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.post(f"{P}/accounts/{{account_id}}/zones/{{zone_number}}/delete")
async def admin_delete_zone(request: Request, account_id: str, zone_number: str):
    user = require_admin(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return HTMLResponse("CSRF error", status_code=403)
    acct, _ = _resolve_admin_account(account_id)
    short_id = acct["account_id"] if acct else account_id
    delete_zone(short_id, zone_number)
    zones = get_zones(short_id)
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("partials/account_zones.html", {
        "request": request, "zones": zones, "account_id": account_id,
        "base_url": P, "csrf_token": csrf, "success": "Zone deleted.",
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


# --- Admin Settings ---
@app.get(f"{P}/settings", response_class=HTMLResponse)
async def admin_settings_page(request: Request):
    user = require_admin(request)
    if not user:
        return RedirectResponse(f"{P}/login", status_code=302)
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("settings.html", {
        "request": request, "user": user, "csrf_token": csrf,
        "success": None, "error": None,
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.post(f"{P}/settings")
async def admin_settings_submit(request: Request):
    user = require_admin(request)
    if not user:
        return RedirectResponse(f"{P}/login", status_code=302)
    form = await request.form()
    return _change_password_flow(request, form, user, "settings.html")


# --- Admin User Management ---
@app.get(f"{P}/users", response_class=HTMLResponse)
async def admin_users_page(request: Request):
    user = require_admin(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse(f"{P}/", status_code=302)
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("users.html", {
        "request": request, "user": user, "users": get_users(),
        "csrf_token": csrf, "error": None, "success": None,
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.post(f"{P}/users")
async def admin_create_user(request: Request):
    user = require_admin(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse(f"{P}/", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse(f"{P}/users", status_code=302)
    username = _form_str(form, "username")
    password = form.get("password", "")
    role = form.get("role", "operator")
    if not username or len(password) < 8 or role not in ("admin", "operator"):
        return RedirectResponse(f"{P}/users", status_code=302)
    try:
        create_user(username, password, role)
        logger.info("User '%s' created by '%s'", username, user["username"])
    except Exception as e:
        logger.error("Error creating user: %s", e)
    return RedirectResponse(f"{P}/users", status_code=302)


@app.post(f"{P}/users/{{user_id}}/delete")
async def admin_delete_user(request: Request, user_id: int):
    user = require_admin(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse(f"{P}/", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse(f"{P}/users", status_code=302)
    if user_id == user["user_id"]:
        return RedirectResponse(f"{P}/users", status_code=302)
    delete_user(user_id)
    logger.info("User ID %d deleted by '%s'", user_id, user["username"])
    return RedirectResponse(f"{P}/users", status_code=302)


# --- Admin Dealer Management ---
@app.get(f"{P}/dealers", response_class=HTMLResponse)
async def admin_dealers_page(request: Request):
    user = require_admin(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse(f"{P}/", status_code=302)
    csrf = _get_csrf_token(request)
    dealers = get_dealers()
    for d in dealers:
        du = get_dealer_user(d["id"])
        d["login_username"] = du["username"] if du else None
    resp = templates.TemplateResponse("dealers.html", {
        "request": request, "user": user, "dealers": dealers,
        "csrf_token": csrf, "next_prefix": next_dealer_prefix(), "dnis": DEFAULT_DNIS,
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.post(f"{P}/dealers")
async def admin_create_dealer(request: Request):
    user = require_admin(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse(f"{P}/", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse(f"{P}/dealers", status_code=302)

    prefix = _form_str(form, "prefix")
    dnis = _form_str(form, "dnis").upper()
    name = _form_str(form, "name")
    phone = _form_str(form, "phone")
    email = _form_str(form, "email")
    notes = _form_str(form, "notes")
    password = form.get("password", "")

    if not PREFIX_RE.match(prefix) or not DNIS_RE.match(dnis) or not name:
        return RedirectResponse(f"{P}/dealers", status_code=302)
    if not email or len(password) < 8:
        return RedirectResponse(f"{P}/dealers", status_code=302)

    try:
        dealer_id = create_dealer(prefix=prefix, dnis=dnis, name=name,
                                   phone=phone, email=email, notes=notes)
        create_user(email, password, role="dealer", dealer_id=dealer_id)
        logger.info("Dealer '%s' (prefix=%s) created with login '%s' by '%s'",
                     name, prefix, email, user["username"])
    except Exception as e:
        logger.error("Error creating dealer: %s", e)
    return RedirectResponse(f"{P}/dealers", status_code=302)


@app.post(f"{P}/dealers/{{dealer_id}}/edit")
async def admin_edit_dealer(request: Request, dealer_id: int):
    user = require_admin(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse(f"{P}/", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse(f"{P}/dealers", status_code=302)
    update_dealer(dealer_id,
                  name=_form_str(form, "name") or None,
                  phone=_form_str(form, "phone"),
                  email=_form_str(form, "email"),
                  notes=_form_str(form, "notes"),
                  enabled=form.get("enabled") == "1")
    return RedirectResponse(f"{P}/dealers", status_code=302)


@app.post(f"{P}/dealers/{{dealer_id}}/delete")
async def admin_delete_dealer(request: Request, dealer_id: int):
    user = require_admin(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse(f"{P}/", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse(f"{P}/dealers", status_code=302)
    delete_dealer(dealer_id)
    logger.info("Dealer ID %d deleted by '%s'", dealer_id, user["username"])
    return RedirectResponse(f"{P}/dealers", status_code=302)


# ============================================================================
# DEALER PORTAL — / (root)
# ============================================================================

@app.get("/login", response_class=HTMLResponse)
async def dealer_login_page(request: Request):
    user = get_current_user(request)
    if user and user.get("role") == "dealer":
        return RedirectResponse("/", status_code=302)
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("dealer/login.html", {"request": request, "error": None, "csrf_token": csrf})
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.post("/login")
async def dealer_login_submit(request: Request):
    form = await request.form()
    return _login_flow(
        request, form,
        username=_form_str(form, "username"),
        password=form.get("password", ""),
        ip=_client_ip(request),
        template="dealer/login.html",
        redirect_to="/",
        role_check=lambda u: u.get("role") == "dealer" and u.get("dealer_id") is not None,
    )


@app.get("/logout")
async def dealer_logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("session")
    return response


@app.get("/", response_class=HTMLResponse)
async def dealer_dashboard(request: Request):
    user, dealer = require_dealer(request)
    if not user:
        # If admin user hits /, redirect to admin portal
        admin_user = get_current_user(request)
        if admin_user and admin_user.get("role") in ("admin", "operator"):
            return RedirectResponse(f"{P}/", status_code=302)
        return RedirectResponse("/login", status_code=302)
    did = dealer["id"]
    prefix = dealer["prefix"]
    events, total = get_events(limit=50, exclude_codes=["RP"], dealer_id=did)
    return templates.TemplateResponse("dealer/dashboard.html", {
        "request": request, "user": user, "dealer": dealer,
        "stats": get_event_stats(dealer_id=did),
        "events": events, "total": total,
        "latest_id": get_latest_event_id(),
        "critical_events": get_recent_critical_events(hours=1, dealer_id=did),
        "last_heartbeat": get_last_heartbeat(),  # global receiver heartbeat
        "prefix": prefix,
        "account_names": get_account_name_map(dealer_id=did),
        "zone_names": get_zone_name_map(dealer_id=did),
    })


@app.get("/events", response_class=HTMLResponse)
async def dealer_events_page(request: Request):
    user, dealer = require_dealer(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    did = dealer["id"]
    params = request.query_params
    account = params.get("account", "").strip() or None
    code = params.get("code", "").strip().upper() or None
    zone = params.get("zone", "").strip() or None
    show_hb = params.get("show_heartbeats", "").strip() == "1"
    page = _safe_int(params.get("page"), default=1)
    limit = 50
    offset = (page - 1) * limit
    exclude = None if show_hb else ["RP"]
    events, total = get_events(limit=limit, offset=offset, account=account, code=code, zone=zone,
                               dealer_id=did, exclude_codes=exclude)
    pages = max(1, (total + limit - 1) // limit)
    return templates.TemplateResponse("dealer/events.html", {
        "request": request, "user": user, "dealer": dealer,
        "events": events, "total": total, "page": page, "pages": pages,
        "prefix": dealer["prefix"],
        "show_heartbeats": show_hb,
        "filters": {"account": account or "", "code": code or "", "zone": zone or ""},
        "account_names": get_account_name_map(dealer_id=did),
        "zone_names": get_zone_name_map(dealer_id=did),
    })


@app.websocket("/events/ws")
async def dealer_events_websocket(ws: WebSocket):
    token = ws.cookies.get("session")
    if not token:
        await ws.close(code=4001, reason="Authentication required")
        return
    user = verify_session_token(token)
    if not user or user.get("role") != "dealer" or not user.get("dealer_id"):
        await ws.close(code=4001, reason="Invalid session")
        return
    dealer = get_dealer(user["dealer_id"])
    if not dealer:
        await ws.close(code=4001, reason="Dealer not found")
        return
    did = dealer["id"]
    prefix = dealer["prefix"]
    await ws.accept()
    last_id = get_latest_event_id()
    last_hb = get_last_heartbeat()
    last_hb_time = last_hb["received_at"] if last_hb else None
    session_start = asyncio.get_event_loop().time()
    acct_names = get_account_name_map(dealer_id=did)
    z_names = get_zone_name_map(dealer_id=did)
    cache_refresh = 0
    try:
        while True:
            await asyncio.sleep(1.5)
            # Re-validate session every 5 minutes; hard timeout at SESSION_MAX_AGE
            elapsed = asyncio.get_event_loop().time() - session_start
            if elapsed > config.SESSION_MAX_AGE:
                await ws.close(code=4001, reason="Session expired")
                return
            if int(elapsed) % 300 < 2:
                check = verify_session_token(token)
                if not check:
                    await ws.close(code=4001, reason="Session expired")
                    return
                # Re-check dealer is still enabled
                d = get_dealer(did)
                if not d or not d.get("enabled"):
                    await ws.close(code=4001, reason="Dealer disabled")
                    return
            # Refresh name caches every 30 seconds
            if int(elapsed) - cache_refresh >= 30:
                cache_refresh = int(elapsed)
                acct_names = get_account_name_map(dealer_id=did)
                z_names = get_zone_name_map(dealer_id=did)
            # Dealer-scoped events
            new_events = get_events_since(last_id, dealer_id=did)
            if new_events:
                last_id = new_events[-1]["id"]
                for event in new_events:
                    await ws.send_json(_safe_ws_event(event, prefix, account_names=acct_names, zone_names=z_names))
            # Global heartbeat (not scoped to dealer)
            hb = get_last_heartbeat()
            if hb and hb["received_at"] != last_hb_time:
                last_hb_time = hb["received_at"]
                await ws.send_json({
                    "type": "heartbeat",
                    "received_at": str(hb["received_at"]),
                    "event_code": hb["event_code"],
                    "severity": "muted",
                })
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("Dealer WebSocket error: %s", e)


@app.get("/accounts", response_class=HTMLResponse)
async def dealer_accounts_page(request: Request):
    user, dealer = require_dealer(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    csrf = _get_csrf_token(request)
    did = dealer["id"]
    resp = templates.TemplateResponse("dealer/accounts.html", {
        "request": request, "user": user, "dealer": dealer,
        "accounts": get_accounts(dealer_id=did),
        "next_account_id": next_account_id(did),
        "csrf_token": csrf,
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.post("/accounts")
async def dealer_create_account(request: Request):
    user, dealer = require_dealer(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse("/accounts", status_code=302)
    account_id = _form_str(form, "account_id").upper()
    name = _form_str(form, "name")
    if not ACCOUNT_ID_RE.match(account_id) or not name:
        return RedirectResponse("/accounts", status_code=302)
    try:
        create_account(
            account_id=account_id, name=name,
            address=_form_str(form, "address"),
            phone=_form_str(form, "phone"),
            email=_form_str(form, "email"),
            notes=_form_str(form, "notes"),
            dealer_id=dealer["id"],
        )
    except Exception as e:
        logger.error("Error creating dealer account: %s", e)
    return RedirectResponse("/accounts", status_code=302)


@app.post("/accounts/{account_id}/edit")
async def dealer_edit_account(request: Request, account_id: str):
    user, dealer = require_dealer(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not ACCOUNT_ID_RE.match(account_id):
        return RedirectResponse("/accounts", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse("/accounts", status_code=302)
    update_account(
        account_id=account_id, name=_form_str(form, "name"),
        address=_form_str(form, "address"), phone=_form_str(form, "phone"),
        email=_form_str(form, "email"), notes=_form_str(form, "notes"),
        dealer_id=dealer["id"],
    )
    if request.headers.get("hx-request"):
        acct = get_account(account_id, dealer_id=dealer["id"])
        csrf = _get_csrf_token(request)
        resp = templates.TemplateResponse("partials/account_details.html", {
            "request": request, "account": acct, "dealer": dealer,
            "base_url": "", "csrf_token": csrf, "success": True,
        })
        return _set_csrf_cookie(resp, csrf, config.DEBUG)
    return RedirectResponse("/accounts", status_code=302)


@app.post("/accounts/{account_id}/delete")
async def dealer_delete_account(request: Request, account_id: str):
    user, dealer = require_dealer(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not ACCOUNT_ID_RE.match(account_id):
        return RedirectResponse("/accounts", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse("/accounts", status_code=302)
    delete_account(account_id, dealer_id=dealer["id"])
    return RedirectResponse("/accounts", status_code=302)


# --- Dealer Account Modal (HTMX partials) ---
@app.get("/accounts/{account_id}/modal/details", response_class=HTMLResponse)
async def dealer_account_modal_details(request: Request, account_id: str):
    user, dealer = require_dealer(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    acct = get_account(account_id, dealer_id=dealer["id"])
    if not acct:
        return HTMLResponse("<div class='text-red-400'>Account not found.</div>")
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("partials/account_details.html", {
        "request": request, "account": acct, "dealer": dealer,
        "base_url": "", "csrf_token": csrf, "success": False,
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.get("/accounts/{account_id}/modal/events", response_class=HTMLResponse)
async def dealer_account_modal_events(request: Request, account_id: str):
    user, dealer = require_dealer(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    did = dealer["id"]
    offset = _safe_int(request.query_params.get("offset"), default=0, minimum=0)
    limit = 25
    # Events store full prefix+account_id, so search for both forms
    prefix = dealer["prefix"]
    full_account = prefix + account_id
    events, total = get_events(limit=limit, offset=offset, account=full_account, dealer_id=did)
    if total == 0:
        # Try with short prefix
        short = prefix.lstrip("0")
        if short:
            events, total = get_events(limit=limit, offset=offset, account=short + account_id, dealer_id=did)
    if total == 0:
        # Try raw account_id
        events, total = get_events(limit=limit, offset=offset, account=account_id, dealer_id=did)
    zone_names = get_zone_name_map(dealer_id=did)
    return templates.TemplateResponse("partials/account_events.html", {
        "request": request, "events": events, "total": total,
        "offset": offset, "limit": limit, "account_id": account_id,
        "zone_names": zone_names, "base_url": "",
    })


@app.get("/accounts/{account_id}/modal/zones", response_class=HTMLResponse)
async def dealer_account_modal_zones(request: Request, account_id: str):
    user, dealer = require_dealer(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    zones = get_zones(account_id, dealer_id=dealer["id"])
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("partials/account_zones.html", {
        "request": request, "zones": zones, "account_id": account_id,
        "base_url": "", "csrf_token": csrf, "success": None,
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.post("/accounts/{account_id}/zones")
async def dealer_upsert_zone(request: Request, account_id: str):
    user, dealer = require_dealer(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return HTMLResponse("CSRF error", status_code=403)
    zone_number = _form_str(form, "zone_number")
    zone_name = _form_str(form, "zone_name")
    if zone_number and zone_name:
        upsert_zone(account_id, zone_number, zone_name, dealer_id=dealer["id"])
    zones = get_zones(account_id, dealer_id=dealer["id"])
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("partials/account_zones.html", {
        "request": request, "zones": zones, "account_id": account_id,
        "base_url": "", "csrf_token": csrf, "success": "Zone saved.",
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.post("/accounts/{account_id}/zones/{zone_number}/delete")
async def dealer_delete_zone(request: Request, account_id: str, zone_number: str):
    user, dealer = require_dealer(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return HTMLResponse("CSRF error", status_code=403)
    delete_zone(account_id, zone_number, dealer_id=dealer["id"])
    zones = get_zones(account_id, dealer_id=dealer["id"])
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("partials/account_zones.html", {
        "request": request, "zones": zones, "account_id": account_id,
        "base_url": "", "csrf_token": csrf, "success": "Zone deleted.",
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.get("/settings", response_class=HTMLResponse)
async def dealer_settings_page(request: Request):
    user, dealer = require_dealer(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("dealer/settings.html", {
        "request": request, "user": user, "dealer": dealer,
        "csrf_token": csrf, "success": None, "error": None,
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.post("/settings")
async def dealer_settings_submit(request: Request):
    user, dealer = require_dealer(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    return _change_password_flow(request, form, user, "dealer/settings.html", {"dealer": dealer})


# --- Dealer Webhooks ---

def _mask_secret(secret: str) -> str:
    """Mask a webhook secret, showing only the last 4 chars."""
    if not secret or len(secret) <= 4:
        return "****"
    return "..." + secret[-4:]


@app.get("/webhooks", response_class=HTMLResponse)
async def dealer_webhooks_page(request: Request):
    user, dealer = require_dealer(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    csrf = _get_csrf_token(request)
    did = dealer["id"]
    webhooks = get_webhooks(dealer_id=did)
    # Add masked secrets and delivery stats
    for wh in webhooks:
        wh["masked_secret"] = _mask_secret(wh.get("secret", ""))
        stats = get_delivery_stats(wh["id"])
        wh["stats"] = stats
    resp = templates.TemplateResponse("dealer/webhooks.html", {
        "request": request, "user": user, "dealer": dealer,
        "webhooks": webhooks, "csrf_token": csrf,
        "new_secret": None,
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.post("/webhooks")
async def dealer_create_webhook(request: Request):
    user, dealer = require_dealer(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse("/webhooks", status_code=302)

    url = _form_str(form, "url", max_length=2000)
    description = _form_str(form, "description", max_length=200)
    event_filter = _form_str(form, "event_filter", max_length=200) or "*"

    if not WEBHOOK_URL_RE.match(url):
        # Re-render with error
        csrf = _generate_csrf_token()
        did = dealer["id"]
        webhooks = get_webhooks(dealer_id=did)
        for wh in webhooks:
            wh["masked_secret"] = _mask_secret(wh.get("secret", ""))
            wh["stats"] = get_delivery_stats(wh["id"])
        resp = templates.TemplateResponse("dealer/webhooks.html", {
            "request": request, "user": user, "dealer": dealer,
            "webhooks": webhooks, "csrf_token": csrf,
            "new_secret": None, "error": "URL must start with https://",
        })
        return _set_csrf_cookie(resp, csrf, config.DEBUG)

    secret = secrets.token_hex(32)
    try:
        wh_id = create_webhook(
            dealer_id=dealer["id"], url=url, secret=secret,
            description=description, event_filter=event_filter,
        )
        logger.info("Webhook created: id=%d dealer=%d url=%s", wh_id, dealer["id"], url)
    except Exception as e:
        logger.error("Error creating webhook: %s", e)
        return RedirectResponse("/webhooks", status_code=302)

    # Re-render showing the secret once
    csrf = _generate_csrf_token()
    did = dealer["id"]
    webhooks = get_webhooks(dealer_id=did)
    for wh in webhooks:
        wh["masked_secret"] = _mask_secret(wh.get("secret", ""))
        wh["stats"] = get_delivery_stats(wh["id"])
    resp = templates.TemplateResponse("dealer/webhooks.html", {
        "request": request, "user": user, "dealer": dealer,
        "webhooks": webhooks, "csrf_token": csrf,
        "new_secret": secret, "new_webhook_id": wh_id,
        "success": "Webhook created. Copy the secret below — it won't be shown again.",
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.post("/webhooks/{webhook_id:int}/edit")
async def dealer_edit_webhook(request: Request, webhook_id: int):
    user, dealer = require_dealer(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse("/webhooks", status_code=302)

    wh = get_webhook(webhook_id, dealer_id=dealer["id"])
    if not wh:
        return RedirectResponse("/webhooks", status_code=302)

    url = _form_str(form, "url", max_length=2000)
    if url and not WEBHOOK_URL_RE.match(url):
        return RedirectResponse("/webhooks", status_code=302)

    update_webhook(
        webhook_id, dealer_id=dealer["id"],
        url=url or None,
        description=_form_str(form, "description", max_length=200) or None,
        event_filter=_form_str(form, "event_filter", max_length=200) or None,
        enabled=form.get("enabled"),
    )
    return RedirectResponse("/webhooks", status_code=302)


@app.post("/webhooks/{webhook_id:int}/toggle")
async def dealer_toggle_webhook(request: Request, webhook_id: int):
    user, dealer = require_dealer(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse("/webhooks", status_code=302)

    wh = get_webhook(webhook_id, dealer_id=dealer["id"])
    if not wh:
        return RedirectResponse("/webhooks", status_code=302)

    new_enabled = 0 if wh["enabled"] else 1
    update_webhook(webhook_id, dealer_id=dealer["id"], enabled=new_enabled)
    return RedirectResponse("/webhooks", status_code=302)


@app.post("/webhooks/{webhook_id:int}/delete")
async def dealer_delete_webhook(request: Request, webhook_id: int):
    user, dealer = require_dealer(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse("/webhooks", status_code=302)
    delete_webhook(webhook_id, dealer_id=dealer["id"])
    return RedirectResponse("/webhooks", status_code=302)


@app.post("/webhooks/{webhook_id:int}/regenerate-secret")
async def dealer_regenerate_secret(request: Request, webhook_id: int):
    user, dealer = require_dealer(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse("/webhooks", status_code=302)

    wh = get_webhook(webhook_id, dealer_id=dealer["id"])
    if not wh:
        return RedirectResponse("/webhooks", status_code=302)

    new_secret = secrets.token_hex(32)
    update_webhook_secret(webhook_id, new_secret, dealer_id=dealer["id"])

    # Re-render showing the new secret
    csrf = _generate_csrf_token()
    did = dealer["id"]
    webhooks = get_webhooks(dealer_id=did)
    for w in webhooks:
        w["masked_secret"] = _mask_secret(w.get("secret", ""))
        w["stats"] = get_delivery_stats(w["id"])
    resp = templates.TemplateResponse("dealer/webhooks.html", {
        "request": request, "user": user, "dealer": dealer,
        "webhooks": webhooks, "csrf_token": csrf,
        "new_secret": new_secret, "new_webhook_id": webhook_id,
        "success": "Secret regenerated. Copy it below — it won't be shown again.",
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.post("/webhooks/{webhook_id:int}/test")
async def dealer_test_webhook(request: Request, webhook_id: int):
    """Send a test webhook payload to verify endpoint connectivity."""
    user, dealer = require_dealer(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    if not _verify_csrf(form.get("csrf_token"), request.cookies.get("csrf_token")):
        return RedirectResponse("/webhooks", status_code=302)

    wh = get_webhook(webhook_id, dealer_id=dealer["id"])
    if not wh:
        return RedirectResponse("/webhooks", status_code=302)

    test_payload = _json.dumps({
        "event_id": "evt_test_0",
        "account_id": dealer["prefix"] + "TEST",
        "event_code": "TEST",
        "event_type": "Test",
        "title": "Webhook connectivity test",
        "name": "Test",
        "zone": "",
        "zone_name": "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "description": "Webhook connectivity test",
        "dealer_id": str(dealer["id"]),
        "dealer_name": dealer["name"],
        "account_name": "Test Account",
        "account_address": "",
        "account_phone": "",
        "account_email": "",
        "partition": "",
        "message": "This is a test webhook from ARC",
        "sia_type": "Test",
        "sia_description": "Webhook connectivity test",
    })

    # Enqueue the test delivery
    enqueue_webhook_delivery(wh["id"], 0, test_payload)

    csrf = _generate_csrf_token()
    did = dealer["id"]
    webhooks = get_webhooks(dealer_id=did)
    for w in webhooks:
        w["masked_secret"] = _mask_secret(w.get("secret", ""))
        w["stats"] = get_delivery_stats(w["id"])
    resp = templates.TemplateResponse("dealer/webhooks.html", {
        "request": request, "user": user, "dealer": dealer,
        "webhooks": webhooks, "csrf_token": csrf,
        "new_secret": None,
        "success": f"Test webhook queued for delivery to {wh['url']}",
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)


@app.get("/webhooks/{webhook_id:int}/deliveries", response_class=HTMLResponse)
async def dealer_webhook_deliveries(request: Request, webhook_id: int):
    """HTMX partial: delivery log for a webhook."""
    user, dealer = require_dealer(request)
    if not user:
        return HTMLResponse("Unauthorized", status_code=401)
    wh = get_webhook(webhook_id, dealer_id=dealer["id"])
    if not wh:
        return HTMLResponse("Not found", status_code=404)
    deliveries = get_delivery_log(webhook_id, limit=25)
    return templates.TemplateResponse("dealer/webhook_deliveries.html", {
        "request": request, "deliveries": deliveries, "webhook": wh,
    })


# --- Admin Webhooks Overview ---

@app.get(f"{P}/webhooks", response_class=HTMLResponse)
async def admin_webhooks_page(request: Request):
    user = require_admin(request)
    if not user:
        return RedirectResponse(f"{P}/login", status_code=302)
    webhook_stats = get_webhook_stats_all()
    csrf = _get_csrf_token(request)
    resp = templates.TemplateResponse("webhooks_admin.html", {
        "request": request, "user": user, "webhook_stats": webhook_stats,
        "csrf_token": csrf,
    })
    return _set_csrf_cookie(resp, csrf, config.DEBUG)
