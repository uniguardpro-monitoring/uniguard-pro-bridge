import sys
sys.path.insert(0, '/opt/arc-dashboard')
import bcrypt
from app.database import get_db_rw, get_db

password = b'Pa$$w0rd16'
hashed = bcrypt.hashpw(password, bcrypt.gensalt()).decode()

with get_db_rw() as conn:
    conn.execute('UPDATE users SET password_hash = ? WHERE username = ?', (hashed, 'admin'))
    # No manual commit needed — get_db_rw auto-commits

with get_db() as conn:
    r = conn.execute('SELECT password_hash FROM users WHERE username = ?', ('admin',)).fetchone()
    ok = bcrypt.checkpw(password, r[0].encode())
    print('Auto-commit verified:', ok)
