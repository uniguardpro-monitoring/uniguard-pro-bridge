"""Database access layer."""
import logging
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

logger = logging.getLogger("arc-dashboard")

DEFAULT_DNIS = os.environ.get("SIA_LINECARD_DINS", "01")


@contextmanager
def get_db():
    """Get a read-only SQLite connection."""
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_db_rw():
    """Get a read-write SQLite connection with auto-commit on success."""
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dealer_scope(dealer_id):
    """Build a dealer_id WHERE fragment and params tuple."""
    if dealer_id is not None:
        return "AND dealer_id = ?", (dealer_id,)
    return "", ()


_VALID_TABLES = frozenset({
    "dealers", "users", "accounts", "events", "zones",
    "webhooks", "webhook_queue", "webhook_deliveries",
    "api_keys",
})


def _column_exists(conn, table, column):
    if table not in _VALID_TABLES:
        raise ValueError(f"Invalid table name: {table}")
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c["name"] == column for c in cols)


def _table_exists(conn, table):
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row[0] > 0


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def migrate_db():
    """Run schema migrations. Safe to call repeatedly."""
    with get_db_rw() as conn:
        # Create dealers table
        if not _table_exists(conn, "dealers"):
            conn.execute("""
                CREATE TABLE dealers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prefix TEXT NOT NULL UNIQUE,
                    dnis TEXT NOT NULL,
                    name TEXT NOT NULL,
                    phone TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            logger.info("Created dealers table")

        # Add dealer_id to users
        if not _column_exists(conn, "users", "dealer_id"):
            conn.execute("ALTER TABLE users ADD COLUMN dealer_id INTEGER REFERENCES dealers(id)")
            logger.info("Added dealer_id column to users table")

        # Add dealer_id to accounts
        if not _column_exists(conn, "accounts", "dealer_id"):
            conn.execute("ALTER TABLE accounts ADD COLUMN dealer_id INTEGER REFERENCES dealers(id)")
            logger.info("Added dealer_id column to accounts table")

        # Add dealer_id to events
        if not _column_exists(conn, "events", "dealer_id"):
            conn.execute("ALTER TABLE events ADD COLUMN dealer_id INTEGER REFERENCES dealers(id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_dealer_id ON events(dealer_id)")
            logger.info("Added dealer_id column to events table")

        # Migrate dealers: drop UNIQUE on dnis, split contact -> phone+email
        needs_rebuild = False
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='dealers' "
            "AND name='sqlite_autoindex_dealers_2'"
        ).fetchone()
        if idx:
            needs_rebuild = True
        if _column_exists(conn, "dealers", "contact") and not _column_exists(conn, "dealers", "phone"):
            needs_rebuild = True

        if needs_rebuild:
            conn.execute("ALTER TABLE dealers RENAME TO dealers_old")
            conn.execute("""
                CREATE TABLE dealers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prefix TEXT NOT NULL UNIQUE,
                    dnis TEXT NOT NULL,
                    name TEXT NOT NULL,
                    phone TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                INSERT INTO dealers (id, prefix, dnis, name, phone, email, notes, enabled, created_at, updated_at)
                SELECT id, prefix, dnis, name, COALESCE(contact, ''), '', COALESCE(notes, ''), enabled, created_at, updated_at
                FROM dealers_old
            """)
            conn.execute("DROP TABLE dealers_old")
            logger.info("Rebuilt dealers table (phone/email columns, no UNIQUE on dnis)")

        # Migrate DNIS from 00000001 to 01
        conn.execute("UPDATE dealers SET dnis = '01' WHERE dnis = '00000001'")

        # Migrate accounts: split contact -> phone+email
        if _column_exists(conn, "accounts", "contact") and not _column_exists(conn, "accounts", "phone"):
            conn.execute("ALTER TABLE accounts ADD COLUMN phone TEXT DEFAULT ''")
            conn.execute("ALTER TABLE accounts ADD COLUMN email TEXT DEFAULT ''")
            conn.execute("UPDATE accounts SET phone = COALESCE(contact, '')")
            logger.info("Added phone/email columns to accounts table (migrated contact -> phone)")

        # Create zones table
        if not _table_exists(conn, "zones"):
            conn.execute("""
                CREATE TABLE zones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    dealer_id INTEGER,
                    zone_number TEXT NOT NULL,
                    zone_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_id, dealer_id, zone_number)
                )
            """)
            logger.info("Created zones table")

        # Create webhooks table
        if not _table_exists(conn, "webhooks"):
            conn.execute("""
                CREATE TABLE webhooks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dealer_id INTEGER NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
                    url TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    event_filter TEXT DEFAULT '*',
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_webhooks_dealer_id ON webhooks(dealer_id)")
            logger.info("Created webhooks table")

        # Add auth_type to webhooks (hmac or bearer)
        if _table_exists(conn, "webhooks") and not _column_exists(conn, "webhooks", "auth_type"):
            conn.execute("ALTER TABLE webhooks ADD COLUMN auth_type TEXT NOT NULL DEFAULT 'hmac'")
            logger.info("Added auth_type column to webhooks table")

        # Add account_filter to webhooks (scope webhook to specific account)
        if _table_exists(conn, "webhooks") and not _column_exists(conn, "webhooks", "account_filter"):
            conn.execute("ALTER TABLE webhooks ADD COLUMN account_filter TEXT DEFAULT NULL")
            logger.info("Added account_filter column to webhooks table")

        # Create webhook_queue table
        if not _table_exists(conn, "webhook_queue"):
            conn.execute("""
                CREATE TABLE webhook_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    webhook_id INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
                    event_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wq_status_next ON webhook_queue(status, next_attempt_at)")
            logger.info("Created webhook_queue table")

        # Create webhook_deliveries table
        if not _table_exists(conn, "webhook_deliveries"):
            conn.execute("""
                CREATE TABLE webhook_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    webhook_id INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
                    event_id INTEGER NOT NULL,
                    attempt INTEGER NOT NULL,
                    status_code INTEGER,
                    response_body TEXT DEFAULT '',
                    error TEXT DEFAULT '',
                    duration_ms INTEGER,
                    delivered_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wd_webhook_id ON webhook_deliveries(webhook_id)")
            logger.info("Created webhook_deliveries table")

        # Create api_keys table
        if not _table_exists(conn, "api_keys"):
            conn.execute("""
                CREATE TABLE api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_hash TEXT NOT NULL UNIQUE,
                    key_prefix TEXT NOT NULL,
                    dealer_id INTEGER REFERENCES dealers(id) ON DELETE CASCADE,
                    name TEXT NOT NULL DEFAULT '',
                    permissions TEXT NOT NULL DEFAULT '*',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)")
            logger.info("Created api_keys table")

        # Add archived_at to accounts for soft-delete support
        if not _column_exists(conn, "accounts", "archived_at"):
            conn.execute("ALTER TABLE accounts ADD COLUMN archived_at TEXT DEFAULT NULL")
            logger.info("Added archived_at column to accounts table")

    # Seed default dealer from env vars if no dealers exist
    _seed_default_dealer()

    # Migrate to linecard-based dealer identification
    _migrate_to_linecard_system()


def _seed_default_dealer():
    """Create a default dealer from env vars if the dealers table is empty."""
    prefix = os.environ.get("SIA_DEALER_PREFIX", "001")
    dnis = os.environ.get("SIA_LINECARD_DINS", "01")
    with get_db_rw() as conn:
        count = conn.execute("SELECT COUNT(*) FROM dealers").fetchone()[0]
        if count > 0:
            return
        now = _now()
        conn.execute(
            "INSERT INTO dealers (prefix, dnis, name, phone, email, notes, created_at, updated_at) "
            "VALUES (?, ?, ?, '', '', '', ?, ?)",
            (prefix, dnis, "Default Dealer", now, now),
        )
        dealer_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE events SET dealer_id = ? WHERE dealer_id IS NULL", (dealer_id,))
        conn.execute("UPDATE accounts SET dealer_id = ? WHERE dealer_id IS NULL", (dealer_id,))
        logger.info("Seeded default dealer (prefix=%s, dnis=%s, id=%d) and backfilled data", prefix, dnis, dealer_id)


def _migrate_to_linecard_system():
    """Migrate from prefix-based to linecard/DNIS-based dealer identification.

    - Assigns random 8-hex linecards to dealers (skips dealer 1 / Alarm.com Heart Beat)
    - Reassigns account_ids to 6-digit globally sequential numbers
    - Updates events, webhooks, and zones tables to match new account_ids
    - Idempotent: detects if already migrated by checking account_id format
    """
    with get_db_rw() as conn:
        # Check if migration is needed: are there any old-format account_ids (< 6 chars)?
        accounts = conn.execute(
            "SELECT account_id, dealer_id, rowid FROM accounts ORDER BY rowid"
        ).fetchall()
        if not accounts:
            return
        # If all account_ids are already 6-digit numeric, skip
        needs_migration = any(
            len(a["account_id"]) < 6 or not a["account_id"].isdigit()
            for a in accounts if a["dealer_id"] != 1  # skip dealer 1
        )
        if not needs_migration:
            return

        logger.info("Starting linecard system migration...")

        # Step 1: Assign unique 8-hex linecards to dealers (skip dealer 1)
        dealers = conn.execute("SELECT id, prefix, dnis FROM dealers ORDER BY id").fetchall()
        for d in dealers:
            if d["id"] == 1:
                continue  # Alarm.com Heart Beat stays unchanged
            # Generate random 8-hex linecard
            while True:
                new_dnis = secrets.token_hex(4).upper()  # 8 hex chars
                existing = conn.execute(
                    "SELECT COUNT(*) FROM dealers WHERE dnis = ?", (new_dnis,)
                ).fetchone()[0]
                if existing == 0:
                    break
            conn.execute("UPDATE dealers SET dnis = ? WHERE id = ?", (new_dnis, d["id"]))
            logger.info("Dealer %d: assigned linecard %s (was prefix=%s, dnis=%s)",
                        d["id"], new_dnis, d["prefix"], d["dnis"])

        # Step 2: Reassign account_ids to 6-digit sequential (skip dealer 1)
        next_id = 1
        account_map = {}  # (old_dealer_id, old_account_id) -> new_account_id
        for a in accounts:
            if a["dealer_id"] == 1:
                continue
            old_id = a["account_id"]
            new_id = str(next_id).zfill(6)
            account_map[(a["dealer_id"], old_id)] = new_id
            conn.execute(
                "UPDATE accounts SET account_id = ? WHERE rowid = ?",
                (new_id, a["rowid"]),
            )
            logger.info("Account: %s (dealer %d) -> %s", old_id, a["dealer_id"], new_id)
            next_id += 1

        # Step 3: Update events table — map old prefix+account to new 6-digit ID
        # Build prefix-to-dealer map for parsing old event account_ids
        prefix_map = {}
        for d in dealers:
            if d["id"] == 1:
                continue
            prefix_map[d["prefix"]] = d["id"]

        # Get all events for non-dealer-1 accounts
        events = conn.execute(
            "SELECT id, account_id, dealer_id FROM events WHERE dealer_id IS NOT NULL AND dealer_id != 1"
        ).fetchall()
        updated_events = 0
        for evt in events:
            old_evt_acct = evt["account_id"]
            did = evt["dealer_id"]
            # Try to find the old account portion by stripping known prefixes
            old_acct_portion = None
            for prefix, prefix_did in prefix_map.items():
                if prefix_did == did and old_evt_acct.startswith(prefix):
                    old_acct_portion = old_evt_acct[len(prefix):]
                    break
                short = prefix.lstrip("0")
                if short and prefix_did == did and old_evt_acct.startswith(short):
                    old_acct_portion = old_evt_acct[len(short):]
                    break
            if old_acct_portion and (did, old_acct_portion) in account_map:
                new_acct = account_map[(did, old_acct_portion)]
                conn.execute(
                    "UPDATE events SET account_id = ? WHERE id = ?",
                    (new_acct, evt["id"]),
                )
                updated_events += 1

        logger.info("Updated %d events with new account_ids", updated_events)

        # Step 4: Update webhooks account_filter
        for (old_did, old_acct), new_acct in account_map.items():
            conn.execute(
                "UPDATE webhooks SET account_filter = ? WHERE account_filter = ? AND dealer_id = ?",
                (new_acct, old_acct, old_did),
            )

        # Step 5: Update zones account_id
        for (old_did, old_acct), new_acct in account_map.items():
            conn.execute(
                "UPDATE zones SET account_id = ? WHERE account_id = ? AND dealer_id = ?",
                (new_acct, old_acct, old_did),
            )

        logger.info("Linecard system migration complete")


# ---------------------------------------------------------------------------
# Event queries
# ---------------------------------------------------------------------------

def _build_event_filters(account=None, code=None, zone=None, since=None,
                         exclude_codes=None, dealer_id=None):
    """Build WHERE clause and params for event queries."""
    clauses = []
    params = []
    if account:
        clauses.append("account_id = ?")
        params.append(account)
    if code:
        clauses.append("event_code = ?")
        params.append(code)
    if zone:
        clauses.append("zone = ?")
        params.append(zone)
    if since:
        clauses.append("received_at > ?")
        params.append(since)
    if exclude_codes:
        placeholders = ",".join("?" * len(exclude_codes))
        clauses.append(f"event_code NOT IN ({placeholders})")
        params.extend(exclude_codes)
    if dealer_id is not None:
        clauses.append("dealer_id = ?")
        params.append(dealer_id)
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    return where, params


def get_events(limit=50, offset=0, account=None, code=None, zone=None,
               since=None, exclude_codes=None, dealer_id=None):
    """Fetch events with optional filters. Returns (list[dict], total_count)."""
    where, params = _build_event_filters(account, code, zone, since, exclude_codes, dealer_id)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM events WHERE 1=1{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM events WHERE 1=1{where}", params,
        ).fetchone()[0]
    return [dict(r) for r in rows], total


def get_latest_event_id():
    with get_db() as conn:
        row = conn.execute("SELECT MAX(id) FROM events").fetchone()
        return row[0] or 0


def get_events_since(event_id, dealer_id=None):
    """Get events newer than the given ID (capped at 100)."""
    scope, scope_params = _dealer_scope(dealer_id)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM events WHERE id > ? {scope} ORDER BY id ASC LIMIT 100",
            (event_id,) + scope_params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_event_stats(dealer_id=None):
    """Get dashboard summary stats in a single query."""
    scope, params = _dealer_scope(dealer_id)
    with get_db() as conn:
        row = conn.execute(f"""
            SELECT
                COALESCE(SUM(CASE WHEN event_code != 'RP' THEN 1 ELSE 0 END), 0) AS total_events,
                COALESCE(SUM(CASE WHEN received_at >= date('now') AND event_code != 'RP' THEN 1 ELSE 0 END), 0) AS events_today,
                COALESCE(SUM(CASE WHEN received_at >= date('now')
                     AND event_code NOT IN ('RP', 'CL', 'OP', 'RX')
                     THEN 1 ELSE 0 END), 0) AS alarms_today,
                COUNT(DISTINCT CASE WHEN event_code != 'RP' THEN account_id END) AS active_accounts
            FROM events WHERE 1=1 {scope}
        """, params).fetchone()
        return dict(row)


def get_recent_critical_events(hours=1, dealer_id=None):
    """Get critical alarm events from the last N hours."""
    scope, scope_params = _dealer_scope(dealer_id)
    params = (f"-{hours}",) + scope_params
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT * FROM events
            WHERE event_code IN ('BA', 'FA', 'PA', 'MA', 'HA')
              AND received_at >= datetime('now', ? || ' hours')
              {scope}
            ORDER BY id DESC LIMIT 20
        """, params).fetchall()
        return [dict(r) for r in rows]


def get_last_heartbeat(dealer_id=None):
    """Get the most recent supervision heartbeat event."""
    scope, params = _dealer_scope(dealer_id)
    with get_db() as conn:
        row = conn.execute(
            f"SELECT received_at, event_code FROM events "
            f"WHERE event_code = 'RP' {scope} ORDER BY id DESC LIMIT 1",
            params,
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Dealer CRUD
# ---------------------------------------------------------------------------

def get_dealers():
    """Get all dealers ordered by name."""
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM dealers ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def get_dealer(dealer_id):
    """Get a single dealer by ID."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM dealers WHERE id = ?", (dealer_id,)).fetchone()
        return dict(row) if row else None


def get_dealer_by_prefix(prefix):
    """Get a dealer by prefix."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM dealers WHERE prefix = ?", (prefix,)).fetchone()
        return dict(row) if row else None


def next_dealer_prefix():
    """DEPRECATED: Use next_linecard() instead. Kept for backward compatibility."""
    return next_linecard()


def next_linecard():
    """Generate a unique random 8-hex-character linecard/DNIS."""
    with get_db() as conn:
        for _ in range(100):  # safety limit
            candidate = secrets.token_hex(4).upper()
            exists = conn.execute(
                "SELECT COUNT(*) FROM dealers WHERE dnis = ?", (candidate,)
            ).fetchone()[0]
            if exists == 0:
                return candidate
    raise RuntimeError("Failed to generate unique linecard after 100 attempts")


def create_dealer(prefix, dnis, name, phone="", email="", notes=""):
    """Create a new dealer. Returns the new dealer ID."""
    now = _now()
    with get_db_rw() as conn:
        conn.execute(
            "INSERT INTO dealers (prefix, dnis, name, phone, email, notes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (prefix, dnis, name, phone, email, notes, now, now),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_dealer(dealer_id, name=None, phone=None, email=None, notes=None, enabled=None):
    """Update dealer fields (only non-None values are updated)."""
    now = _now()
    fields = ["updated_at=?"]
    params = [now]
    for col, val in [("name", name), ("phone", phone), ("email", email), ("notes", notes)]:
        if val is not None:
            fields.append(f"{col}=?")
            params.append(val)
    if enabled is not None:
        fields.append("enabled=?")
        params.append(int(enabled))
    params.append(dealer_id)
    with get_db_rw() as conn:
        conn.execute(f"UPDATE dealers SET {','.join(fields)} WHERE id=?", params)


def delete_dealer(dealer_id):
    """Delete a dealer and cascade: remove dealer users, unlink accounts/events."""
    with get_db_rw() as conn:
        conn.execute("DELETE FROM users WHERE dealer_id = ?", (dealer_id,))
        conn.execute("UPDATE accounts SET dealer_id = NULL WHERE dealer_id = ?", (dealer_id,))
        conn.execute("UPDATE events SET dealer_id = NULL WHERE dealer_id = ?", (dealer_id,))
        conn.execute("DELETE FROM dealers WHERE id = ?", (dealer_id,))


def get_dealer_user(dealer_id):
    """Get the login user associated with a dealer."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, username, role, dealer_id, created_at, last_login "
            "FROM users WHERE dealer_id = ? LIMIT 1",
            (dealer_id,),
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Account CRUD (dealer-scoped)
# ---------------------------------------------------------------------------

def next_account_id(dealer_id=None):
    """Generate the next available 6-digit sequential account ID (global)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(CAST(account_id AS INTEGER)) FROM accounts "
            "WHERE account_id GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'"
        ).fetchone()
        max_val = row[0] if row[0] is not None else 0
    return str(max_val + 1).zfill(6)


def get_accounts(dealer_id=None, include_archived=False):
    """Get accounts, optionally scoped to a dealer."""
    scope, params = _dealer_scope(dealer_id)
    archive_filter = "" if include_archived else "AND (archived_at IS NULL)"
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM accounts WHERE 1=1 {scope} {archive_filter} ORDER BY name", params
        ).fetchall()
        return [dict(r) for r in rows]


def get_account(account_id, dealer_id=None):
    """Get a single account by ID, optionally scoped to a dealer."""
    scope, scope_params = _dealer_scope(dealer_id)
    with get_db() as conn:
        row = conn.execute(
            f"SELECT * FROM accounts WHERE account_id = ? {scope}",
            (account_id,) + scope_params,
        ).fetchone()
        return dict(row) if row else None


def create_account(account_id, name, address="", phone="", email="", notes="", dealer_id=None):
    now = _now()
    with get_db_rw() as conn:
        conn.execute(
            "INSERT INTO accounts (account_id, name, address, phone, email, notes, dealer_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, name, address, phone, email, notes, dealer_id, now, now),
        )


def update_account(account_id, name, address="", phone="", email="", notes="", dealer_id=None):
    now = _now()
    scope, scope_params = _dealer_scope(dealer_id)
    with get_db_rw() as conn:
        conn.execute(
            f"UPDATE accounts SET name=?, address=?, phone=?, email=?, notes=?, updated_at=? "
            f"WHERE account_id=? {scope}",
            (name, address, phone, email, notes, now, account_id) + scope_params,
        )


def delete_account(account_id, dealer_id=None):
    scope, scope_params = _dealer_scope(dealer_id)
    with get_db_rw() as conn:
        conn.execute(
            f"DELETE FROM accounts WHERE account_id = ? {scope}",
            (account_id,) + scope_params,
        )


# ---------------------------------------------------------------------------
# Zone CRUD (account-scoped)
# ---------------------------------------------------------------------------

def get_zones(account_id, dealer_id=None):
    """Get all zones for an account, ordered by zone_number."""
    scope, scope_params = _dealer_scope(dealer_id)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM zones WHERE account_id = ? {scope} ORDER BY zone_number",
            (account_id,) + scope_params,
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_zone(account_id, zone_number, zone_name, dealer_id=None):
    """Insert or update a zone for an account."""
    now = _now()
    with get_db_rw() as conn:
        existing = conn.execute(
            "SELECT id FROM zones WHERE account_id = ? AND zone_number = ? AND dealer_id = ?",
            (account_id, zone_number, dealer_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE zones SET zone_name = ?, updated_at = ? WHERE id = ?",
                (zone_name, now, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO zones (account_id, dealer_id, zone_number, zone_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (account_id, dealer_id, zone_number, zone_name, now, now),
            )


def delete_zone(account_id, zone_number, dealer_id=None):
    """Delete a zone by account and zone number."""
    scope, scope_params = _dealer_scope(dealer_id)
    with get_db_rw() as conn:
        conn.execute(
            f"DELETE FROM zones WHERE account_id = ? AND zone_number = ? {scope}",
            (account_id, zone_number) + scope_params,
        )


# ---------------------------------------------------------------------------
# Lookup maps (for efficient name resolution in templates/WebSocket)
# ---------------------------------------------------------------------------

def get_account_name_map(dealer_id=None):
    """Return {account_id: name} for accounts under a dealer.

    For admin (dealer_id=None), returns {prefix+account_id: name} keyed on
    the composite ID as stored in the events table.
    """
    with get_db() as conn:
        if dealer_id is not None:
            rows = conn.execute(
                "SELECT account_id, name FROM accounts WHERE dealer_id = ?",
                (dealer_id,),
            ).fetchall()
            return {r["account_id"]: r["name"] for r in rows}
        else:
            rows = conn.execute(
                "SELECT a.account_id, a.name, d.prefix "
                "FROM accounts a JOIN dealers d ON a.dealer_id = d.id"
            ).fetchall()
            result = {}
            for r in rows:
                result[r["prefix"] + r["account_id"]] = r["name"]
                result[r["account_id"]] = r["name"]
            return result


def get_zone_name_map(dealer_id=None):
    """Return {(account_id, zone_number): zone_name} for zones under a dealer."""
    scope, scope_params = _dealer_scope(dealer_id)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT account_id, zone_number, zone_name FROM zones WHERE 1=1 {scope}",
            scope_params,
        ).fetchall()
        return {(r["account_id"], r["zone_number"]): r["zone_name"] for r in rows}


# ---------------------------------------------------------------------------
# Webhook CRUD
# ---------------------------------------------------------------------------

def get_webhooks(dealer_id=None):
    """Get all webhooks, optionally scoped to a dealer."""
    scope, params = _dealer_scope(dealer_id)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM webhooks WHERE 1=1 {scope} ORDER BY created_at DESC", params
        ).fetchall()
        return [dict(r) for r in rows]


def get_webhook(webhook_id, dealer_id=None):
    """Get a single webhook by ID, optionally scoped to a dealer."""
    scope, scope_params = _dealer_scope(dealer_id)
    with get_db() as conn:
        row = conn.execute(
            f"SELECT * FROM webhooks WHERE id = ? {scope}",
            (webhook_id,) + scope_params,
        ).fetchone()
        return dict(row) if row else None


def get_webhooks_for_account(account_id, dealer_id=None):
    """Get webhooks scoped to a specific account via account_filter."""
    scope, scope_params = _dealer_scope(dealer_id)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM webhooks WHERE account_filter = ? {scope} ORDER BY created_at DESC",
            (account_id,) + scope_params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_enabled_webhooks_for_dealer(dealer_id):
    """Get enabled webhooks for a dealer (used by receiver enqueue)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, url, secret, event_filter, auth_type, account_filter FROM webhooks "
            "WHERE dealer_id = ? AND enabled = 1",
            (dealer_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def create_webhook(dealer_id, url, secret, description="", event_filter="*",
                   auth_type="hmac", account_filter=None):
    """Create a new webhook. Returns the new webhook ID.

    Raises ValueError if a webhook with the same URL and account_filter
    already exists for the dealer (prevents duplicates).
    """
    now = _now()
    with get_db_rw() as conn:
        # Check for duplicate: same dealer + URL + account_filter
        existing = conn.execute(
            "SELECT id FROM webhooks WHERE dealer_id = ? AND url = ? AND "
            "(account_filter = ? OR (account_filter IS NULL AND ? IS NULL))",
            (dealer_id, url, account_filter, account_filter),
        ).fetchone()
        if existing:
            raise ValueError(
                f"A webhook for this URL and account already exists (webhook ID {existing[0]})"
            )
        conn.execute(
            "INSERT INTO webhooks (dealer_id, url, secret, description, event_filter, "
            "auth_type, account_filter, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (dealer_id, url, secret, description, event_filter, auth_type, account_filter, now, now),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_webhook(webhook_id, dealer_id=None, **fields):
    """Update webhook fields. Only non-None values in fields are updated."""
    allowed = {"url", "description", "event_filter", "enabled", "auth_type", "account_filter"}
    now = _now()
    set_parts = ["updated_at=?"]
    params = [now]
    for col, val in fields.items():
        if col in allowed and val is not None:
            set_parts.append(f"{col}=?")
            params.append(int(val) if col == "enabled" else val)
    scope, scope_params = _dealer_scope(dealer_id)
    params.append(webhook_id)
    with get_db_rw() as conn:
        conn.execute(
            f"UPDATE webhooks SET {','.join(set_parts)} WHERE id=? {scope}",
            params + list(scope_params),
        )


def update_webhook_secret(webhook_id, new_secret, dealer_id=None):
    """Regenerate a webhook's secret."""
    now = _now()
    scope, scope_params = _dealer_scope(dealer_id)
    with get_db_rw() as conn:
        conn.execute(
            f"UPDATE webhooks SET secret=?, updated_at=? WHERE id=? {scope}",
            (new_secret, now, webhook_id) + scope_params,
        )


def delete_webhook(webhook_id, dealer_id=None):
    """Delete a webhook (cascade deletes queue and delivery rows)."""
    scope, scope_params = _dealer_scope(dealer_id)
    with get_db_rw() as conn:
        # Verify webhook exists (and belongs to dealer if scoped)
        wh = conn.execute(
            f"SELECT id FROM webhooks WHERE id = ? {scope}",
            (webhook_id,) + scope_params,
        ).fetchone()
        if not wh:
            return
        conn.execute("DELETE FROM webhook_queue WHERE webhook_id = ?", (webhook_id,))
        conn.execute("DELETE FROM webhook_deliveries WHERE webhook_id = ?", (webhook_id,))
        conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))


# ---------------------------------------------------------------------------
# Webhook queue operations (used by receiver + dispatch worker)
# ---------------------------------------------------------------------------

def enqueue_webhook_delivery(webhook_id, event_id, payload_json, now_iso=None):
    """Insert a pending delivery into the webhook queue."""
    now = now_iso or _now()
    with get_db_rw() as conn:
        conn.execute(
            "INSERT INTO webhook_queue (webhook_id, event_id, payload, attempts, "
            "next_attempt_at, status, created_at) VALUES (?, ?, ?, 0, ?, 'pending', ?)",
            (webhook_id, event_id, payload_json, now, now),
        )


def enqueue_webhooks_for_event(event_id, dealer_id, event_code, payload_json):
    """Look up enabled webhooks for a dealer and enqueue matching deliveries."""
    webhooks = get_enabled_webhooks_for_dealer(dealer_id)
    now = _now()
    for wh in webhooks:
        filt = wh.get("event_filter", "*").strip()
        if filt != "*":
            allowed_codes = {c.strip().upper() for c in filt.split(",") if c.strip()}
            if event_code.upper() not in allowed_codes:
                continue
        enqueue_webhook_delivery(wh["id"], event_id, payload_json, now_iso=now)


def get_pending_deliveries(limit=20):
    """Get pending deliveries ready for dispatch."""
    now = _now()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT q.*, w.url, w.secret, w.auth_type, w.enabled AS webhook_enabled "
            "FROM webhook_queue q JOIN webhooks w ON q.webhook_id = w.id "
            "WHERE q.status = 'pending' AND q.next_attempt_at <= ? "
            "ORDER BY q.created_at ASC LIMIT ?",
            (now, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_delivery_success(queue_id):
    """Mark a queued delivery as successfully delivered."""
    with get_db_rw() as conn:
        conn.execute(
            "UPDATE webhook_queue SET status='delivered', attempts=attempts+1 WHERE id=?",
            (queue_id,),
        )


def mark_delivery_retry(queue_id, next_attempt_at):
    """Increment attempts and schedule a retry."""
    with get_db_rw() as conn:
        conn.execute(
            "UPDATE webhook_queue SET attempts=attempts+1, next_attempt_at=? WHERE id=?",
            (next_attempt_at, queue_id),
        )


def mark_delivery_failed(queue_id):
    """Mark a queued delivery as permanently failed."""
    with get_db_rw() as conn:
        conn.execute(
            "UPDATE webhook_queue SET status='failed', attempts=attempts+1 WHERE id=?",
            (queue_id,),
        )


def log_delivery_attempt(webhook_id, event_id, attempt, status_code,
                         response_body, error, duration_ms):
    """Log a single webhook delivery attempt."""
    now = _now()
    with get_db_rw() as conn:
        conn.execute(
            "INSERT INTO webhook_deliveries (webhook_id, event_id, attempt, status_code, "
            "response_body, error, duration_ms, delivered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (webhook_id, event_id, attempt, status_code,
             (response_body or "")[:200], error or "", duration_ms, now),
        )


def get_delivery_log(webhook_id, limit=50):
    """Get recent delivery attempts for a webhook."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM webhook_deliveries WHERE webhook_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (webhook_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_delivery_stats(webhook_id):
    """Get delivery counts for a webhook."""
    with get_db() as conn:
        row = conn.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END), 0) AS delivered,
                COALESCE(SUM(CASE WHEN status_code IS NULL OR status_code NOT BETWEEN 200 AND 299 THEN 1 ELSE 0 END), 0) AS failed,
                COUNT(*) AS total
            FROM webhook_deliveries WHERE webhook_id = ?
        """, (webhook_id,)).fetchone()
        return dict(row)


def get_webhook_stats_all():
    """Get delivery stats for all webhooks (admin overview)."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT w.id AS webhook_id, w.dealer_id, w.url, w.description,
                   w.event_filter, w.enabled, w.created_at,
                   w.auth_type, w.account_filter,
                   d.name AS dealer_name, d.prefix AS dealer_prefix,
                   COALESCE(SUM(CASE WHEN wd.status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END), 0) AS delivered,
                   COALESCE(SUM(CASE WHEN wd.status_code IS NULL OR wd.status_code NOT BETWEEN 200 AND 299 THEN 1 ELSE 0 END), 0) AS failed,
                   COUNT(wd.id) AS total_attempts,
                   MAX(wd.delivered_at) AS last_attempt
            FROM webhooks w
            JOIN dealers d ON w.dealer_id = d.id
            LEFT JOIN webhook_deliveries wd ON wd.webhook_id = w.id
            GROUP BY w.id
            ORDER BY d.name, w.created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def cleanup_old_deliveries(days=7):
    """Purge delivered/failed queue entries and old delivery logs."""
    with get_db_rw() as conn:
        conn.execute(
            "DELETE FROM webhook_queue WHERE status IN ('delivered', 'failed') "
            "AND created_at < datetime('now', ? || ' days')",
            (f"-{days}",),
        )
        conn.execute(
            "DELETE FROM webhook_deliveries WHERE delivered_at < datetime('now', ? || ' days')",
            (f"-{days}",),
        )


# ---------------------------------------------------------------------------
# Account archive (soft delete)
# ---------------------------------------------------------------------------

def archive_account(account_id, dealer_id=None):
    """Soft-delete an account by setting archived_at."""
    now = _now()
    scope, scope_params = _dealer_scope(dealer_id)
    with get_db_rw() as conn:
        conn.execute(
            f"UPDATE accounts SET archived_at=?, updated_at=? WHERE account_id=? {scope}",
            (now, now, account_id) + scope_params,
        )


def restore_account(account_id, dealer_id=None):
    """Unarchive an account by clearing archived_at."""
    now = _now()
    scope, scope_params = _dealer_scope(dealer_id)
    with get_db_rw() as conn:
        conn.execute(
            f"UPDATE accounts SET archived_at=NULL, updated_at=? WHERE account_id=? {scope}",
            (now, account_id) + scope_params,
        )


# ---------------------------------------------------------------------------
# Single event lookup
# ---------------------------------------------------------------------------

def get_event(event_id, dealer_id=None):
    """Get a single event by ID, optionally scoped to a dealer."""
    scope, scope_params = _dealer_scope(dealer_id)
    with get_db() as conn:
        row = conn.execute(
            f"SELECT * FROM events WHERE id = ? {scope}",
            (event_id,) + scope_params,
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# API key CRUD
# ---------------------------------------------------------------------------

def create_api_key(key_hash, key_prefix, dealer_id=None, name=""):
    """Create an API key record. Returns the new key ID."""
    now = _now()
    with get_db_rw() as conn:
        conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, dealer_id, name, "
            "permissions, enabled, created_at) VALUES (?, ?, ?, ?, '*', 1, ?)",
            (key_hash, key_prefix, dealer_id, name, now),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def get_api_keys(dealer_id=None):
    """List API keys. If dealer_id given, only that dealer's keys + admin keys are excluded."""
    with get_db() as conn:
        if dealer_id is not None:
            rows = conn.execute(
                "SELECT id, key_prefix, dealer_id, name, permissions, enabled, "
                "created_at, last_used_at FROM api_keys WHERE dealer_id = ? ORDER BY created_at DESC",
                (dealer_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, key_prefix, dealer_id, name, permissions, enabled, "
                "created_at, last_used_at FROM api_keys ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def get_api_key_by_hash(key_hash):
    """Look up an API key by its SHA-256 hash. Returns full row or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,)
        ).fetchone()
        return dict(row) if row else None


def delete_api_key(key_id, dealer_id=None):
    """Delete/revoke an API key."""
    scope, scope_params = _dealer_scope(dealer_id)
    with get_db_rw() as conn:
        conn.execute(
            f"DELETE FROM api_keys WHERE id = ? {scope}",
            (key_id,) + scope_params,
        )


def update_api_key_last_used(key_id):
    """Stamp last_used_at on an API key."""
    now = _now()
    with get_db_rw() as conn:
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now, key_id),
        )
