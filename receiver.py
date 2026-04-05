#!/usr/bin/env python3
"""SIA DC-09 Alarm Receiver Service.

Listens for SIA DC-09 alarm signals (SIA-DCS format) from alarm.com
and logs events to SQLite database, file, and stdout.

alarm.com account structure:
  - Prefix (3-digit): Dealer identifier, not transmitted in signal
  - Account (4-digit hex): Alarm system identifier, transmitted in SIA-DCS messages
  - Linecard/DINS (up to 5 hex): Receiver routing identifier assigned by us
"""
import calendar
import json
import logging
import os
import signal
import sqlite3
import sys
import time
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from pysiaalarm import SIAAccount, SIAClient, SIAEvent
from pysiaalarm.utils import CommunicationsProtocol

# --- Configuration ---
HOST = os.environ.get("SIA_HOST", "0.0.0.0")
PORT = int(os.environ.get("SIA_PORT", "12000"))
PROTOCOL = os.environ.get("SIA_PROTOCOL", "TCP").upper()
LOG_DIR = os.environ.get("SIA_LOG_DIR", "/var/log/alarm-receiver")
DB_PATH = os.environ.get("SIA_DB_PATH", "/opt/alarm-receiver/data/arc.db")
DEALER_PREFIX = os.environ.get("SIA_DEALER_PREFIX", "001")  # Legacy, used for catch-all account setup
LINECARD_DINS = os.environ.get("SIA_LINECARD_DINS", "01")   # Legacy fallback
ENCRYPTION_KEY = os.environ.get("SIA_ENCRYPTION_KEY") or None
ACCOUNT_IDS = os.environ.get("SIA_ACCOUNT_IDS", "")

# Event codes that bypass account validation (supervision/heartbeat signals)
SUPERVISION_CODES = frozenset({"RP", "RX"})

# --- Logging ---
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("alarm-receiver")

event_log_path = Path(LOG_DIR) / "events.log"
raw_log_path = Path(LOG_DIR) / "raw.log"


@contextmanager
def get_db():
    """Get a SQLite connection with WAL mode and busy timeout."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Linecard reconstruction — alarm.com splits 8-hex DNIS across R and L fields
# ---------------------------------------------------------------------------

def _reconstruct_linecard(receiver: str, line: str) -> str:
    """Reconstruct the 8-hex linecard from SIA R and L fields.

    alarm.com splits an 8-character DNIS like '34A67B58' into:
      R field: 'R0034A6' (first half, zero-padded with R prefix)
      L field: 'L007B58' (second half, zero-padded with L prefix)

    We strip the R/L prefix and leading zeros from each, then concatenate.
    Result is uppercased for consistent matching.
    """
    r_part = (receiver or "").lstrip("R").lstrip("0") if receiver else ""
    l_part = (line or "").lstrip("L").lstrip("0") if line else ""
    linecard = (r_part + l_part).upper()
    return linecard


# ---------------------------------------------------------------------------
# Dealer resolver — maps incoming events to dealer_id via linecard/DNIS
# ---------------------------------------------------------------------------

class DealerResolver:
    """Resolves incoming SIA events to a dealer_id using linecard/DNIS lookup."""

    def __init__(self):
        self._dnis_map = {}       # dnis (linecard) -> dealer_id
        self._lock = threading.Lock()
        self._last_load = 0

    def load(self):
        """Load dealer linecard data from the database."""
        try:
            with get_db() as conn:
                exists = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='dealers'"
                ).fetchone()[0]
                if not exists:
                    return
                rows = conn.execute(
                    "SELECT id, prefix, dnis FROM dealers WHERE enabled = 1"
                ).fetchall()
            dnis_map = {}
            for row in rows:
                # Store with uppercase for consistent matching
                dnis_map[row["dnis"].upper()] = row["id"]
                # Also store lowercase and original for fallback
                dnis_map[row["dnis"]] = row["id"]
            with self._lock:
                self._dnis_map = dnis_map
                self._last_load = time.monotonic()
            logger.info("Loaded %d dealer(s) for linecard-based routing", len(rows))
        except Exception:
            logger.exception("Error loading dealers from database")

    def _maybe_refresh(self):
        """Refresh cache if older than 60 seconds."""
        if time.monotonic() - self._last_load > 60:
            self.load()

    def resolve(self, linecard: str) -> int | None:
        """Resolve a dealer_id from a reconstructed linecard.

        Returns dealer_id or None if unrecognized.
        """
        self._maybe_refresh()
        if not linecard:
            return None
        with self._lock:
            return self._dnis_map.get(linecard.upper()) or self._dnis_map.get(linecard)


dealer_resolver = DealerResolver()


# ---------------------------------------------------------------------------
# Account validator — rejects signals from unregistered accounts
# ---------------------------------------------------------------------------

class AccountValidator:
    """Validates incoming account IDs against registered accounts in the DB."""

    def __init__(self):
        self._accounts: dict[int, set[str]] = {}  # dealer_id -> set of account_ids
        self._lock = threading.Lock()
        self._last_load = 0

    def load(self):
        """Load registered accounts from the database."""
        try:
            with get_db() as conn:
                exists = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='accounts'"
                ).fetchone()[0]
                if not exists:
                    return
                rows = conn.execute(
                    "SELECT account_id, dealer_id FROM accounts WHERE dealer_id IS NOT NULL"
                ).fetchall()
            accounts: dict[int, set[str]] = {}
            for row in rows:
                did = row["dealer_id"]
                acct = row["account_id"].upper()
                if did not in accounts:
                    accounts[did] = set()
                accounts[did].add(acct)
            with self._lock:
                self._accounts = accounts
                self._last_load = time.monotonic()
            total = sum(len(s) for s in accounts.values())
            logger.info("Loaded %d registered account(s) for validation", total)
        except Exception:
            logger.exception("Error loading accounts from database")

    def _maybe_refresh(self):
        """Refresh cache if older than 60 seconds."""
        if time.monotonic() - self._last_load > 60:
            self.load()

    def is_valid(self, account_id, dealer_id):
        """Check if an account_id is registered for the given dealer."""
        self._maybe_refresh()
        with self._lock:
            return account_id.upper() in self._accounts.get(dealer_id, set())


account_validator = AccountValidator()


def _extract_sia_code(event: SIAEvent) -> dict:
    """Safely extract SIA code details from an event."""
    if not event.sia_code:
        return {}
    code_obj = event.sia_code
    return {
        "code": getattr(code_obj, "code", event.code),
        "type": getattr(code_obj, "type", None),
        "description": getattr(code_obj, "description", None),
    }


def log_event_to_db(event_data: dict, dealer_id=None) -> None:
    """Write an event to the SQLite database."""
    sia = event_data.get("sia_code", {})
    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO events
                   (received_at, account_id, event_code, event_type, event_desc,
                    zone, partition, message_type, message, sequence, receiver, line,
                    encrypted, valid_message, valid_timestamp, raw_message, dealer_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_data.get("received_at"),
                    event_data.get("account", ""),
                    event_data.get("code", ""),
                    sia.get("type"),
                    sia.get("description"),
                    event_data.get("zone"),
                    event_data.get("partition"),
                    event_data.get("message_type"),
                    event_data.get("message"),
                    event_data.get("sequence"),
                    event_data.get("receiver"),
                    event_data.get("line"),
                    1 if event_data.get("encrypted") else 0,
                    1 if event_data.get("valid_message") else 0,
                    1 if event_data.get("valid_timestamp") else 0,
                    json.dumps(event_data, default=str),
                    dealer_id,
                ),
            )
            conn.commit()
    except Exception:
        logger.exception("Error writing event to database")


def _enqueue_webhooks(event_data: dict, dealer_id: int, event_id: int = 0) -> None:
    """Enqueue webhook deliveries for matching dealer webhooks.

    Looks up enabled webhooks for the dealer, checks each webhook's event_filter,
    builds the payload JSON, and inserts into webhook_queue. This is a fast
    synchronous SQLite operation — actual HTTP dispatch happens asynchronously
    in the dashboard's background worker.
    """
    event_code = event_data.get("code", "")
    if not event_code:
        return

    try:
        with get_db() as conn:
            # Check if webhooks table exists
            exists = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='webhooks'"
            ).fetchone()[0]
            if not exists:
                return

            webhooks = conn.execute(
                "SELECT id, url, secret, event_filter, auth_type, account_filter FROM webhooks "
                "WHERE dealer_id = ? AND enabled = 1",
                (dealer_id,),
            ).fetchall()

        if not webhooks:
            return

        # Resolve dealer and account info for payload
        # In the linecard system, event.account IS the account_id directly (no prefix)
        acct_id = event_data.get("account", "")
        account_name = ""
        zone = event_data.get("zone") or ""
        zone_name = ""
        try:
            with get_db() as conn:
                arow = conn.execute(
                    "SELECT name FROM accounts WHERE account_id = ? AND dealer_id = ?",
                    (acct_id, dealer_id),
                ).fetchone()
                if arow:
                    account_name = arow["name"]
                if zone:
                    zrow = conn.execute(
                        "SELECT zone_name FROM zones WHERE account_id = ? AND dealer_id = ? AND zone_number = ?",
                        (acct_id, dealer_id, zone),
                    ).fetchone()
                    if zrow:
                        zone_name = zrow["zone_name"]
        except Exception:
            logger.debug("Failed to resolve account/zone names for webhook payload")

        sia_code = event_data.get("sia_code", {})
        sia_type = sia_code.get("type", "")
        sia_desc = sia_code.get("description", "")

        # Build description: "Type - Zone Name" or "Type" or code
        desc_parts = []
        if sia_type:
            desc_parts.append(sia_type)
        if zone_name:
            desc_parts.append(zone_name)
        elif sia_desc:
            desc_parts.append(sia_desc)
        description = " - ".join(desc_parts) if desc_parts else event_code

        # timestamp as Unix epoch integer
        ts_iso = event_data.get("received_at", "")
        try:
            dt = datetime.fromisoformat(ts_iso)
            unix_ts = int(calendar.timegm(dt.utctimetuple()))
        except Exception:
            unix_ts = int(time.time())

        # Zone as integer if numeric, else string
        zone_val = int(zone) if zone and zone.isdigit() else zone

        payload = json.dumps({
            "event_id": f"evt_{event_id}",
            "account_id": acct_id,
            "event_code": event_code,
            "zone": zone_val,
            "zone_name": zone_name,
            "timestamp": unix_ts,
            "description": description,
            "dealer_id": str(dealer_id),
            "account_name": account_name,
        }, default=str)

        now = datetime.now(timezone.utc).isoformat()

        with get_db() as conn:
            for wh in webhooks:
                # Event code filter
                filt = (wh["event_filter"] or "*").strip()
                if filt != "*":
                    allowed = {c.strip().upper() for c in filt.split(",") if c.strip()}
                    if event_code.upper() not in allowed:
                        continue
                # Account filter — if set, only forward events for that account
                acct_filt = (wh["account_filter"] or "").strip().upper()
                if acct_filt and acct_id.upper() != acct_filt:
                    continue
                conn.execute(
                    "INSERT INTO webhook_queue (webhook_id, event_id, payload, "
                    "attempts, next_attempt_at, status, created_at) "
                    "VALUES (?, ?, ?, 0, ?, 'pending', ?)",
                    (wh["id"], event_id, payload, now, now),
                )
            conn.commit()

        logger.debug("Enqueued webhook deliveries for dealer %d, event code %s", dealer_id, event_code)

    except Exception:
        logger.exception("Error enqueueing webhooks")


def log_event_to_file(event_data: dict) -> None:
    """Append an event as a JSON line to the event log."""
    try:
        with open(event_log_path, "a") as f:
            f.write(json.dumps(event_data, default=str) + "\n")
    except OSError:
        logger.exception("Error writing to event log file")


def log_raw_to_file(raw_message: str) -> None:
    """Append raw message to the raw log."""
    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        with open(raw_log_path, "a") as f:
            f.write(f"{timestamp} | {raw_message}\n")
    except OSError:
        logger.exception("Error writing to raw log file")


def handle_event(event: SIAEvent) -> None:
    """Handle an incoming SIA event.

    Called by pysiaalarm for each received alarm signal. Writes to database,
    log files, and stdout. Must not raise — exceptions are caught and logged.
    """
    try:
        sia_code = _extract_sia_code(event)
        event_data = {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "account": event.account,
            "code": event.code,
            "message_type": str(event.message_type) if event.message_type else None,
            "zone": event.ri if event.code not in ("OP", "CL") else None,
            "partition": event.ri if event.code in ("OP", "CL") else None,
            "message": event.message,
            "timestamp": str(event.timestamp) if event.timestamp else None,
            "sequence": event.sequence,
            "receiver": event.receiver,
            "line": event.line,
            "encrypted": event.encrypted,
            "valid_message": event.valid_message,
            "valid_timestamp": event.valid_timestamp,
        }
        if sia_code:
            event_data["sia_code"] = sia_code

        # Reconstruct linecard from R + L fields and resolve dealer
        linecard = _reconstruct_linecard(event.receiver, event.line)
        did = dealer_resolver.resolve(linecard)
        is_supervision = event.code in SUPERVISION_CODES

        # The account_id is the full transmitted value (6-digit, no prefix stripping)
        acct_id = event.account or ""

        # Validate — supervision events always pass through
        if not is_supervision:
            if did is None:
                logger.warning(
                    "REJECTED | Unrecognized linecard | Linecard: %s | Account: %s | Code: %s | R: %s | L: %s",
                    linecard, event.account, event.code, event.receiver, event.line,
                )
                if event.full_message:
                    log_raw_to_file(event.full_message)
                return

            if not account_validator.is_valid(acct_id, did):
                logger.warning(
                    "REJECTED | Unregistered account | Dealer: %s | Account: %s | Code: %s | Linecard: %s",
                    did, acct_id, event.code, linecard,
                )
                if event.full_message:
                    log_raw_to_file(event.full_message)
                return

        logger.info(
            "ALARM EVENT | Dealer: %s | Linecard: %s | Account: %s | Code: %s | Zone: %s | Type: %s | Msg: %s",
            did or "global",
            linecard,
            acct_id,
            event.code,
            event.ri,
            sia_code.get("type", "unknown"),
            event.message or "",
        )

        log_event_to_db(event_data, dealer_id=did)
        log_event_to_file(event_data)

        # Enqueue webhook deliveries for this dealer's configured endpoints
        if did is not None:
            try:
                # Get the event ID that was just inserted
                evt_id = 0
                try:
                    with get_db() as conn:
                        row = conn.execute("SELECT MAX(id) FROM events").fetchone()
                        evt_id = row[0] or 0
                except Exception:
                    pass
                _enqueue_webhooks(event_data, dealer_id=did, event_id=evt_id)
            except Exception:
                logger.exception("Error enqueueing webhooks")

        if event.full_message:
            log_raw_to_file(event.full_message)

    except Exception:
        logger.exception("Error handling event")


def build_accounts() -> list[SIAAccount]:
    """Build SIA account list from configuration."""
    if not ACCOUNT_IDS.strip():
        logger.info("No specific accounts configured — accepting ALL accounts")
        return [SIAAccount(account_id="", key=ENCRYPTION_KEY)]

    accounts = []
    for acct_id in ACCOUNT_IDS.split(","):
        acct_id = acct_id.strip().upper()
        if acct_id:
            logger.info("Registering account: %s%s", DEALER_PREFIX, acct_id)
            accounts.append(SIAAccount(account_id=acct_id, key=ENCRYPTION_KEY))
    return accounts


def main() -> None:
    """Start the SIA DC-09 receiver."""
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

    # Verify database
    try:
        with get_db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        logger.info("Database connected: %s (%d existing events)", DB_PATH, count)
    except Exception:
        logger.exception("FATAL: Cannot connect to database at %s", DB_PATH)
        sys.exit(1)

    # Load caches
    dealer_resolver.load()
    account_validator.load()

    accounts = build_accounts()
    proto = CommunicationsProtocol.TCP if PROTOCOL == "TCP" else CommunicationsProtocol.UDP

    logger.info("=" * 60)
    logger.info("SIA DC-09 Alarm Receiver (SIA-DCS)")
    logger.info("Host: %s | Port: %d | Protocol: %s", HOST, PORT, PROTOCOL)
    logger.info("Dealer Prefix: %s | Linecard/DINS: %s", DEALER_PREFIX, LINECARD_DINS)
    logger.info("Accounts: %s", "CATCH-ALL" if not ACCOUNT_IDS.strip() else ACCOUNT_IDS)
    logger.info("Encrypted: %s", bool(ENCRYPTION_KEY))
    logger.info("Database: %s", DB_PATH)
    logger.info("Event log: %s", event_log_path)
    logger.info("=" * 60)

    client = SIAClient(
        host=HOST,
        port=PORT,
        accounts=accounts,
        function=handle_event,
        protocol=proto,
    )

    def shutdown_handler(signum, _frame):
        logger.info("Shutdown signal received (%s), stopping...", signum)
        client.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    client.start()
    logger.info("Receiver is listening on %s:%d (%s)", HOST, PORT, PROTOCOL)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, stopping...")
        client.stop()


if __name__ == "__main__":
    main()
