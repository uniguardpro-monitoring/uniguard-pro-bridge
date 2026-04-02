# Changes Log — Cloud Alarm Receiver VPS

## 2026-03-19 — Phase 1: Server Hardening & Prep

### 1. System Update
- Ran `apt update && apt upgrade -y`
- Upgraded: coreutils (9.4-3ubuntu6.1 → 9.4-3ubuntu6.2)

### 2. UFW Firewall
- Installed and enabled UFW
- Default policy: deny incoming, allow outgoing
- Allowed ports:
  - `22/tcp` — SSH
  - `12000/tcp` — SIA DC-09 (alarm receiver)
  - `443/tcp` — HTTPS / REST API
  - `8443/tcp` — Bold Manitou XML

### 3. fail2ban
- Installed fail2ban
- Created `/etc/fail2ban/jail.local`:
  - sshd jail enabled
  - maxretry: 3
  - bantime: 3600s (1 hour)
  - findtime: 600s (10 min)
  - backend: systemd

### 4. SSH Hardening
- Created `/etc/ssh/sshd_config.d/99-hardening.conf`:
  - `PermitRootLogin no`
  - `PasswordAuthentication no`
  - `PubkeyAuthentication yes`
  - `AuthenticationMethods publickey`
  - `X11Forwarding no`
  - `MaxAuthTries 3`
  - `ClientAliveInterval 300`
  - `ClientAliveCountMax 2`
  - `LoginGraceTime 30`
  - `AllowUsers claude`
- Updated main `sshd_config`: PermitRootLogin set to no
- Restarted SSH service; verified key-based access still works

### 5. Unattended Upgrades
- Installed `unattended-upgrades` and `apt-listchanges`
- Configured `/etc/apt/apt.conf.d/50unattended-upgrades`:
  - Security updates from noble, noble-security, ESM
  - AutoFixInterruptedDpkg enabled
  - MinimalSteps enabled
  - Automatic reboot disabled
- Configured `/etc/apt/apt.conf.d/20auto-upgrades`:
  - Daily package list update
  - Daily unattended upgrade
  - Weekly autoclean

### 6. Timezone & NTP
- Set timezone to UTC
- Installed and enabled chrony (NTP)
- Verified NTP sync active (stratum 3)

### 7. Logging
- Verified logrotate is installed
- Created `/etc/logrotate.d/alarm-receiver`:
  - Daily rotation, 30 days retention, compressed
  - Log path: `/var/log/alarm-receiver/*.log`
- Created `/var/log/alarm-receiver/` directory (owned by claude:claude)

## 2026-03-19 — Phase 2: Protocol Research & Selection

- Researched SIA DC-09, Bold Manitou XML, and alarm.com REST API
- Found alarm.com REST API is only accessible through licensed platforms (Bold, Patriot, DICE, etc.)
- Selected **SIA DC-09 with pysiaalarm** as primary receiver protocol
- Full report in `PHASE2_REPORT.md`

## 2026-03-19 — Phase 3: Receiver Software Installation

### 1. Python & pysiaalarm
- Python 3.12.3 already installed on VPS
- Created Python venv at `/opt/alarm-receiver/venv`
- Installed pysiaalarm 3.2.2 (+ pycryptodome 3.23.0, pytz)

### 2. Receiver Service
- Created `/opt/alarm-receiver/receiver.py` — SIA DC-09 receiver daemon
  - Listens on 127.0.0.1:12001 (TCP, behind TLS proxy)
  - Handles SIA-DCS, ADM-CID, and NULL (heartbeat) messages
  - Logs events to JSON lines (`/var/log/alarm-receiver/events.log`)
  - Logs raw messages to `/var/log/alarm-receiver/raw.log`
  - Outputs to journald via stdout
- Created `/opt/alarm-receiver/.env` for runtime configuration

### 3. systemd Service
- Created `/etc/systemd/system/alarm-receiver.service`
  - Runs as user `claude`, auto-restarts on failure
  - Security hardening: NoNewPrivileges, ProtectSystem, ProtectHome, PrivateTmp
  - Enabled on boot

### 4. UFW Update
- Added `80/tcp` for Let's Encrypt ACME challenges

### 5. TLS / Let's Encrypt
- Installed certbot 2.9.0
- DNS updated: `alarmreceiver.uniguardpro.io` → `162.222.206.18` (Cloudflare proxy disabled)
- Certificate issued: `/etc/letsencrypt/live/alarmreceiver.uniguardpro.io/` (expires 2026-06-17)
- Auto-renewal via certbot timer (daily check)

### 6. stunnel TLS Termination
- Installed stunnel4 5.72
- Created `/etc/stunnel/sia-dc09.conf`:
  - Accepts TLS on `0.0.0.0:12000`
  - Forwards plaintext to `127.0.0.1:12001` (pysiaalarm)
  - Combined cert at `/etc/stunnel/certs/sia-dc09.pem`
- Created renewal hook: `/etc/letsencrypt/renewal-hooks/deploy/stunnel-reload.sh`
  - Auto-rebuilds stunnel cert and restarts on LE renewal

### 7. Testing
- Local test: SIA DC-09 NULL (heartbeat) → ACK ✓
- Local test: SIA-DCS BA (Burglar Alarm) → ACK, event logged ✓
- Local TLS test: SIA-DCS FA (Fire Alarm) over TLS → ACK ✓
- **Public internet test**: heartbeat from Windows → TLS → stunnel → pysiaalarm → ACK ✓
- TLS verified: TLSv1.3, AES-256-GCM-SHA384, Let's Encrypt cert, verify OK
- Event log confirmed: structured JSON with SIA code descriptions, timestamps, zones
