#!/usr/bin/env python3
"""Reset admin password utility.

Usage: python3 reset_pw.py <new_password>
Run from the project root or /opt/arc-dashboard directory.
"""
import sys
import bcrypt

if len(sys.argv) < 2:
    print("Usage: python3 reset_pw.py <new_password>")
    print("  Resets the 'admin' user password.")
    sys.exit(1)

password = sys.argv[1].encode()
if len(password) < 8:
    print("Error: Password must be at least 8 characters.")
    sys.exit(1)

sys.path.insert(0, '/opt/arc-dashboard')
from app.database import get_db_rw, get_db

hashed = bcrypt.hashpw(password, bcrypt.gensalt()).decode()

with get_db_rw() as conn:
    result = conn.execute('UPDATE users SET password_hash = ? WHERE username = ?', (hashed, 'admin'))

with get_db() as conn:
    r = conn.execute('SELECT password_hash FROM users WHERE username = ?', ('admin',)).fetchone()
    if r and bcrypt.checkpw(password, r[0].encode()):
        print("Admin password reset successfully.")
    else:
        print("Error: Password reset failed.")
        sys.exit(1)
