# Phase 2 — Protocol Research & Recommendation Report

**Date:** 2026-03-19
**Server:** 162.222.206.18 (Ubuntu 24.04 LTS)

---

## 1. Protocol Comparison

### 1.1 SIA DC-09 (Primary — RECOMMENDED)

**What it is:** ANSI/SIA DC-09-2021 (updated to DC-09-2026) is the open industry standard for transmitting alarm events from premises equipment to a central monitoring station over IP. It is maintained by the Security Industry Association (SIA).

| Property | Details |
|----------|---------|
| **Transport** | TCP (primary), UDP (supported) |
| **Default Port** | 12000/tcp (configurable) |
| **Encryption** | AES-128/192/256 at message layer; TLS 1.2+ at transport layer |
| **Message Formats** | ADM-CID (Ademco Contact ID), SIA-DCS (SIA Data Communication Standard) |
| **Heartbeat** | NULL supervision messages, typically every 60-90 seconds |
| **Standard Cost** | ~$100-200 USD from SIA/ANSI webstore |
| **Open Source Tools** | Yes — pysiaalarm (Python), ioBroker.sia, sia-server |

**Message format overview:**
```
<LF><CRC><0LLL><"SIA-DCS"|"ADM-CID"><SEQ><RECEIVER><PREFIX><ACCOUNT><DATA><TIMESTAMP><CR>
```
- Encrypted messages use `*SIA-DCS` / `*ADM-CID` prefix
- Receiver must ACK each message in the same framing format
- CRC-16/CCITT checksum for integrity

**TLS Requirements (DC-09-2021+):**
- TLS 1.2 minimum recommended for new installations
- Both transport-layer (TLS) and message-layer (AES) encryption supported
- CA-signed certificate recommended; self-signed acceptable for testing
- Mutual TLS (mTLS) discussed but not universally mandated

### 1.2 Bold Manitou XML (Secondary)

**What it is:** Proprietary XML-over-TCP protocol developed by Bold Communications for their Manitou central station software. Widely used by ARCs in the UK and Europe.

| Property | Details |
|----------|---------|
| **Transport** | TCP |
| **Default Port** | 8443/tcp |
| **Encryption** | TLS required (TLS 1.2+) |
| **Message Format** | Proprietary XML |
| **Specification** | NOT publicly available — must be obtained from Bold Group |
| **Open Source Tools** | **None** — no known open-source receiver implementations |

**Key limitation:** The XML schema is proprietary. To implement a receiver, you must either:
1. Contact Bold Group for the integration spec (requires partner agreement)
2. Work with alarm.com dealer support to get the exact schema they transmit
3. Capture and reverse-engineer sample traffic (not recommended)

**Approximate XML structure (unverified):**
```xml
<MonitoringMessage>
  <AccountNumber>1234567</AccountNumber>
  <EventCode>BA</EventCode>
  <ZoneUser>001</ZoneUser>
  <Timestamp>2026-03-19T12:30:00Z</Timestamp>
  <MessageType>alarm</MessageType>
</MonitoringMessage>
```

### 1.3 Affiliated Alarm / REST API (Tertiary)

**What it is:** alarm.com's Platform Connect API offers multi-tiered integration (cloud-to-cloud, cloud-to-hardware). This is the most developer-friendly approach but requires partner-level access.

| Property | Details |
|----------|---------|
| **Transport** | HTTPS (REST) |
| **Default Port** | 443/tcp |
| **Auth** | API keys / OAuth (partner credentials required) |
| **Format** | JSON |
| **Specification** | Partner-only access via alarm.com Partner Portal |
| **Open Source Tools** | pyalarmdotcom (unofficial Home Assistant integration) |

**Key limitation:** alarm.com's API documentation is gated behind partner portal access. There is no public webhook/event forwarding API documented. The unofficial `pyalarmdotcom` library scrapes the customer-facing web interface and is NOT suitable for a production ARC.

---

## 2. Open-Source SIA DC-09 Receiver Candidates

### 2.1 pysiaalarm (Python) — RECOMMENDED

| Property | Details |
|----------|---------|
| **Repo** | [github.com/eavanvalkenburg/pysiaalarm](https://github.com/eavanvalkenburg/pysiaalarm) |
| **Stars** | 52 |
| **Forks** | 38 |
| **Latest Release** | v3.2.2 (January 28, 2026) |
| **Last Commit** | December 28, 2024 |
| **License** | MIT |
| **Language** | Python 3 |
| **Dependencies** | pycryptodome (AES), asyncio |

**Strengths:**
- Most mature and widely-adopted open-source SIA DC-09 receiver
- Powers the Home Assistant SIA integration (real-world validation)
- Supports both threaded TCP server and asyncio coroutine modes
- AES encryption support (16/24/32-char keys)
- Timestamp validation for replay attack prevention
- Supports all defined SIA event codes
- Tested with Ajax Systems panels (and others via Home Assistant community)
- Active release cycle (v3.2.2 in Jan 2026)
- MIT license — no restrictions

**Weaknesses:**
- 36 open issues on GitHub
- Built against DC-09-2012 base; may not have full DC-09-2021 TLS compliance
- No native TLS transport support (AES message-layer only) — we may need to wrap it
- Primarily designed for home automation, not commercial ARC use

### 2.2 pySIA (Python)

| Property | Details |
|----------|---------|
| **Repo** | [github.com/llastowski/pySIA](https://github.com/llastowski/pySIA) |
| **Activity** | Fork/derivative of pysiaalarm ecosystem |

Not enough differentiation from pysiaalarm to recommend separately.

### 2.3 sia-server (Python)

| Property | Details |
|----------|---------|
| **Repo** | [github.com/nimnull/sia-server](https://github.com/nimnull/sia-server) |
| **Stars** | 1 |
| **Last Commit** | January 7, 2020 |
| **Status** | Inactive / abandoned |

Derived from ioBroker.sia. Too inactive and minimal to consider.

### 2.4 ioBroker.sia (JavaScript/Node.js)

| Property | Details |
|----------|---------|
| **Repo** | [github.com/schmupu/ioBroker.sia](https://github.com/schmupu/ioBroker.sia) |
| **Language** | JavaScript |

Part of the ioBroker home automation ecosystem. Tightly coupled to ioBroker; not suitable as a standalone receiver.

### 2.5 SIA Official Java SDK

SIA has published reference implementations, but these are typically member-access only (not freely available open-source). The standard document itself must be purchased (~$100-200). No freely downloadable Java SDK was found.

---

## 3. Port Summary for Firewall Rules

| Protocol | Port | Transport | Status |
|----------|------|-----------|--------|
| SIA DC-09 | 12000/tcp | TCP | Already open in UFW |
| Bold Manitou XML | 8443/tcp | TCP+TLS | Already open in UFW |
| REST API (HTTPS) | 443/tcp | TCP+TLS | Already open in UFW |
| SSH (admin) | 22/tcp | TCP | Already open in UFW |

No additional firewall changes needed.

---

## 4. TLS / Certificate Requirements

| Protocol | TLS Required? | Certificate Type | Notes |
|----------|--------------|-----------------|-------|
| SIA DC-09 | Recommended (DC-09-2021+) | CA-signed preferred | Also supports AES message-layer encryption independently of TLS |
| Bold Manitou XML | Yes | CA-signed required | alarm.com likely requires trusted CA cert |
| REST API | Yes | CA-signed required | Standard HTTPS |

**Recommendation:** Obtain a domain name pointing to 162.222.206.18 and use Let's Encrypt (certbot) for free CA-signed certificates. A single certificate can serve all three protocols.

---

## 5. alarm.com-Specific Findings

### How alarm.com forwards signals:
alarm.com does **NOT** forward signals directly to arbitrary IP receivers. Instead, alarm.com
routes events through **licensed third-party central station automation platforms**:

- **Bold** (Manitou)
- **Bykom**
- **DICE**
- **MAS EX**
- **Microkey**
- **Patriot** (Patriot Systems)
- **SBN**
- **SIMS**
- **Stages**

alarm.com's REST API is only accessible through these platforms — there is no direct
webhook or standalone API endpoint for receiving alarm events.

### What this means for our project:
- To receive alarm.com events, you either need a license for one of the above platforms,
  OR you need to build a receiver that speaks the same protocol these platforms use (SIA DC-09).
- **SIA DC-09 remains the correct approach** — it is the underlying standard protocol that
  all these platforms use for signal transport. alarm.com ultimately delivers signals in
  SIA DC-09 / Contact ID format regardless of which platform is in the middle.
- When configuring forwarding in the alarm.com dealer portal, you select one of these platforms
  and provide receiver IP, port, and account details.

### Configuration requirements (dealer portal):
- Select a central station platform (e.g., Patriot, Bold)
- Receiver IP address (162.222.206.18)
- Receiver port (12000 for DC-09)
- Account number mapping
- Encryption key (AES key shared between alarm.com and receiver)
- alarm.com validates receiver connectivity before activating forwarding

### Integration path:
The most practical approach without a platform license is to build a standalone SIA DC-09
receiver that is protocol-compatible with what alarm.com expects to deliver. This may require
working with alarm.com dealer support to configure the forwarding route to point at our
custom receiver rather than a licensed platform instance.

---

## 6. RECOMMENDATION

### Primary Path: SIA DC-09 with pysiaalarm

**Architecture:**
```
alarm.com cloud → SIA DC-09 (TCP:12000) → pysiaalarm (Python) → Event Log + Dispatch
                                            │
                                            ├── TLS termination (stunnel or native)
                                            ├── AES message decryption
                                            ├── Event parsing & logging
                                            └── /var/log/alarm-receiver/events.log
```

**Implementation plan:**
1. Install Python 3.12+ and pysiaalarm on the VPS
2. Create a systemd service for the receiver
3. Configure AES encryption key (store in .env)
4. Set up TLS termination:
   - Option A: Use stunnel as a TLS proxy in front of pysiaalarm
   - Option B: Extend pysiaalarm with native TLS (asyncio ssl)
   - Option C: Use nginx as a TCP TLS proxy (stream module)
5. Obtain a domain name and Let's Encrypt certificate
6. Test with a SIA DC-09 test client before connecting alarm.com
7. Configure alarm.com dealer portal to forward to our receiver

### Secondary Path: Custom Manitou XML receiver (deferred)
- Only pursue if alarm.com specifically requires Manitou XML
- Will need Bold Group's spec or sample traffic capture
- Python asyncio TCP/TLS server with XML parsing

### Tertiary Path: REST API (not viable without platform license)
- alarm.com API is only accessible through licensed platforms (Bold, Patriot, etc.)
- Would require purchasing a platform license to access
- Deferred indefinitely unless a platform license is obtained

### Recommended Next Steps (Phase 3):
1. Install pysiaalarm and its dependencies
2. Create a wrapper service with systemd integration
3. Set up TLS (Let's Encrypt + stunnel or nginx stream)
4. Build an event logging pipeline
5. Test with a local SIA DC-09 test client
6. Verify reachability from the public internet

---

## Sources

- [pysiaalarm GitHub](https://github.com/eavanvalkenburg/pysiaalarm) — Primary SIA DC-09 receiver implementation
- [SIA DC-09-2021 Standard](https://www.securityindustry.org/industry-standards/dc-09-2021/) — Official standard page
- [Home Assistant SIA Integration](https://www.home-assistant.io/integrations/sia/) — Real-world usage of pysiaalarm
- [Patriot Systems DC09 Receiver](https://www.patriotsystems.com/p68library/DC09.html) — Commercial receiver reference
- [alarm.com Partner KB](https://answers.alarm.com/Partner) — Partner documentation portal
- [pyalarmdotcom](https://github.com/pyalarmdotcom/alarmdotcom) — Unofficial alarm.com integration
- [Roombanker SIA Guide](https://www.roombanker.com/blog/sia-alarm-protocol/) — DC-09 protocol overview
