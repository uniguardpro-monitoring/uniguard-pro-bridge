# Cloud Alarm Receiver

Virtual Alarm Receiving Center (ARC) running on Ubuntu 24.04 LTS.

## Architecture

```
alarm.com  ──►  VPS (alarmreceiver.uniguardpro.io)  ──►  Event Log / Dispatch
                 │
                 ├── SIA DC-09 receiver (pysiaalarm)  port 12000/tcp
                 ├── TLS termination (stunnel)         port 12000 → local
                 └── Event logging (JSON lines)        /var/log/alarm-receiver/
```

## Server Details

| Item | Value |
|------|-------|
| OS | Ubuntu 24.04.4 LTS |
| Kernel | 6.8.0-106-generic |
| Host | 162.222.206.18 |
| Domain | alarmreceiver.uniguardpro.io |
| User | claude |
| Timezone | UTC |
| NTP | chrony |

## Receiver Stack

| Component | Details |
|-----------|---------|
| **Protocol** | SIA DC-09 (ANSI/SIA DC-09-2021) |
| **Library** | pysiaalarm 3.2.2 (Python 3.12) |
| **Port** | 12000/tcp |
| **Message formats** | SIA-DCS, ADM-CID (Contact ID), NULL (heartbeat) |
| **Encryption** | AES-128/192/256 (message layer), TLS 1.2+ (transport layer) |
| **Service** | systemd (`alarm-receiver.service`) |
| **Install path** | `/opt/alarm-receiver/` |

## Security

- **Firewall**: UFW — deny all incoming except SSH (22), SIA DC-09 (12000), HTTPS (443), Manitou (8443), HTTP (80)
- **SSH**: Key-only auth, root login disabled, max 3 auth tries, AllowUsers claude
- **Intrusion prevention**: fail2ban on SSH (3 retries, 1hr ban)
- **Auto-patching**: unattended-upgrades for security updates (daily)
- **Service hardening**: NoNewPrivileges, ProtectSystem, ProtectHome, PrivateTmp

## Logs

| Log | Path | Format |
|-----|------|--------|
| Alarm events | `/var/log/alarm-receiver/events.log` | JSON lines |
| Raw messages | `/var/log/alarm-receiver/raw.log` | Timestamped plaintext |
| Service logs | `journalctl -u alarm-receiver` | systemd journal |

Logrotate: daily rotation, 30-day retention, compressed.

## VPS File Layout

```
/opt/alarm-receiver/
├── receiver.py          # Main receiver daemon
├── .env                 # Runtime config (SIA_PORT, SIA_ACCOUNT_ID, etc.)
└── venv/                # Python virtual environment
```

## Project Files (local)

- `.env` — Credentials and config (git-ignored)
- `.ssh/` — SSH keys (git-ignored)
- `CHANGES.md` — All server changes documented
- `PHASE2_REPORT.md` — Protocol research & recommendation report
- `README.md` — This file

## alarm.com Integration Notes

alarm.com forwards signals through licensed central station platforms (Bold, Patriot, DICE, MAS EX, Microkey, SBN, SIMS, Stages). Direct REST API access requires a platform license. SIA DC-09 is the underlying protocol used by all these platforms.

## Phases

- [x] Phase 1 — Server Hardening & Prep
- [x] Phase 2 — Protocol Research & Selection
- [x] Phase 3 — Receiver Software Installation (TLS pending DNS)
- [ ] Phase 4 — alarm.com Integration
