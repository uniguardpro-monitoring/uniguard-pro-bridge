"""Dashboard configuration."""
import os
import secrets

DB_PATH = os.environ.get("ARC_DB_PATH", "/opt/alarm-receiver/data/arc.db")
SECRET_KEY = os.environ.get("ARC_SECRET_KEY", secrets.token_hex(32))
SESSION_MAX_AGE = int(os.environ.get("ARC_SESSION_MAX_AGE", "28800"))  # 8 hours
DEBUG = os.environ.get("ARC_DEBUG", "false").lower() == "true"

# Rate limiting
LOGIN_MAX_ATTEMPTS = int(os.environ.get("ARC_LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_SECONDS = int(os.environ.get("ARC_LOGIN_LOCKOUT_SECONDS", "300"))
