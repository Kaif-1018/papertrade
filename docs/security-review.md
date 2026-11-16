# Security Review and Threat Model

**Project:** Crypto Paper-Trading Simulator  
**Scope:** Core API, WebSocket Gateways, Authentication Engine, Database Concurrency & Invariants  
**Environment Audited:** Python 3.12, FastAPI, PostgreSQL 16, Redis 7  
**Date:** November 2026  

---

## 1. Executive Summary

A comprehensive security review and penetration assessment was conducted across all subsystems of the Crypto Trading Simulator. The application handles high-concurrency simulated financial transactions, real-time WebSocket market feeds, and stateful user balances.

The primary objectives were to ensure:
1. Complete isolation of financial state and prevention of negative balances, float rounding drift, and race condition exploits.
2. Protection against authentication bypass, session hijacking, token theft/replay, and user enumeration.
3. Resilience against Denial of Service (DoS) through connection starvation, memory exhaustion, and rate limit abuse.
4. Defense-in-depth API posture via strict schema validation, security headers, reverse proxy trust boundaries, and least-privilege role authorization.

---

## 2. Threat Vector Checklist & Findings

| Category | Vector / Vulnerability Tested | Initial Finding | Resolution / Mitigation |
|---|---|---|---|
| **Authentication & Tokens** | Weak `JWT_SECRET` in production | Potential brute-force if default or short secrets were permitted in production environments. | **Fixed:** Application refuses to start if `ENVIRONMENT != "development"` and `JWT_SECRET` is shorter than 32 characters or equals the example default. |
| **Authentication & Tokens** | Token Replay & Stolen Refresh Tokens | Refresh tokens could be reused if intercepted by an attacker. | **Fixed:** Implemented Refresh Token Rotation with Reuse Detection. Tokens belong to a family UUID (`parent_id`). Reusing an expired/consumed token triggers instant revocation of the entire family. |
| **Authentication & Tokens** | Token Confusion Attacks | Access tokens submitted to the refresh route or refresh tokens used as Bearer access tokens. | **Fixed:** Explicit `token_type` claim validation in JWT payload (`access` vs `refresh`). Endpoints reject tokens intended for alternate scopes. |
| **Authentication & Tokens** | User Enumeration via Timing & Errors | Login / register error messages differing between existing and non-existent users. | **Fixed:** Auth endpoints return uniform generic error messages (`INVALID_CREDENTIALS` / 401). Timing normalization is maintained via constant bcrypt iteration rounds. |
| **Authorization (RBAC)** | Privilege Escalation & Admin Bypass | Standard users accessing pair management, platform statistics, or user portfolio resets. | **Fixed:** Role-based access control dependency `require_admin` enforces strict `Role.ADMIN` claims across all `/api/admin/*` endpoints, returning 403 Forbidden for regular users and 401 for unauthenticated requests. |
| **Authorization (IDOR)** | Insecure Direct Object References | Users attempting to view or manipulate other users' portfolios, watchlists, or trade histories. | **Fixed:** All user-scoped endpoints (`/api/portfolio`, `/api/trades`, `/api/watchlist`, `/ws/portfolio`) derive `user_id` strictly from the authenticated JWT session subject, eliminating IDOR surfaces. |
| **Input Validation & API** | Mass Assignment & Parameter Injection | Callers injecting extra fields into request payloads (e.g., trying to supply `role`, `virtual_balance`, `id`). | **Fixed:** Strict Pydantic v2 schemas across all routes configured with `model_config = ConfigDict(extra="forbid")`. Extraneous attributes return 422 Unprocessable Entity. |
| **Input Validation & API** | Oversized Payloads (Memory DoS) | Attackers streaming gigabyte-sized JSON payloads to consume memory buffers. | **Fixed:** Implemented `RequestBodyLimitMiddleware` rejecting requests exceeding `MAX_REQUEST_BODY_BYTES` (64 KB) with HTTP 413 Payload Too Large prior to parsing. |
| **Input Validation & API** | Malformed / Large Numeric Floating Point Exploits | IEEE 754 precision loss creating arbitrage or fractional cent generation. | **Fixed:** Floating-point parsing is forbidden on trade quantities and prices. Schema custom validators convert JSON strings/numbers directly to `Decimal` using string representations, quantized to 8 decimal places. |
| **Input Validation & API** | Excessive Order Volume Abuse | Unbounded trade sizes or quantities triggering numeric overflow in database columns. | **Fixed:** Enforced `MAX_QUANTITY` (1,000,000) and `MAX_TRADE_VALUE` ($10,000,000) upper boundaries in both schema and domain service layers. |
| **Denial of Service (DoS)** | Brute Force & API Flooding | Endpoint spamming exhausting database connections or worker threads. | **Fixed:** Sliding-window rate limiter powered by atomic Redis Lua scripts (`rate_limit()` dependency). Specific tiers applied: Auth (10/min/IP), Trading (30/min/user), General API (120/min/user or IP). Returns HTTP 429 with `Retry-After` and `X-RateLimit-*` headers. |
| **Denial of Service (DoS)** | Redis Dependency Outage | Redis server down blocking traffic or leaving endpoints vulnerable. | **Fixed:** Strategic fail behavior: Fail **CLOSED** on `/api/auth/login` and `/api/auth/register` (preventing unmetered brute-force), and Fail **OPEN** on general API endpoints (preserving platform availability). |
| **Network & Transport** | Spoofed IP Addresses behind Proxies | Attackers spoofing `X-Forwarded-For` to bypass IP-based rate limiting. | **Fixed:** IP extraction checks `settings.TRUST_PROXY`. Header `X-Forwarded-For` is only parsed when trust proxy mode is explicitly enabled; otherwise, socket client IP is used. |
| **Browser Security** | Clickjacking & MIME Sniffing | Framing in malicious iframes or MIME type confusion. | **Fixed:** `SecurityHeadersMiddleware` injects `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy`, and a restrictive `Content-Security-Policy`. |
| **Browser Security** | Cross-Origin Resource Sharing (CORS) | Credentialed requests from untrusted origins or wildcard `*` with credentials. | **Fixed:** Explicit origin allowlist configured via `CORS_ORIGINS`. Wildcard `*` cannot be combined with `allow_credentials=True`. |
| **Real-time (WebSockets)** | WebSocket Handshake Flooding | Rapid creation of persistent WebSocket connections exhausting socket descriptors. | **Fixed:** Inbound WebSocket rate limit enforcing 20 handshakes per minute per IP. Violations are closed immediately with WS code `1008 Policy Violation`. |
| **Real-time (WebSockets)** | WebSocket Frame / Slow-loris Bombing | Excessive message sizes or high-frequency command spam on open sockets. | **Fixed:** Max frame size enforced at 16 KB (`WS_MAX_MESSAGE_BYTES`) and per-connection message rate limit (max 30 messages per 10-second rolling window). Sockets violating limits are closed with code `1008`. |
| **Real-time (WebSockets)** | Cross-Site WebSocket Hijacking (CSWSH) | Malicious origins initiating WebSocket handshakes from user browsers. | **Fixed:** Origin validation on WebSocket connection handshake matching `CORS_ORIGINS`. Disallowed origins are rejected immediately with code `1008`. |
| **Observability & Privacy** | Credential Leakage in Server Logs | Passwords, tokens, or authorization headers logged in plain text. | **Fixed:** Custom `redact_sensitive_data` structlog processor recursively scrubs fields named `password`, `token`, `authorization`, `secret`, `access_token`, and `refresh_token` across all logging sinks. |
| **Data Integrity & Concurrency** | Double Spending & Concurrent Race Conditions | Simultaneous buy or sell requests attempting to drain balances or oversell holdings. | **Fixed:** Pessimistic row-level locking (`SELECT ... FOR UPDATE`) in PostgreSQL. Deterministic acquisition order: always lock `users` row before `holdings` row, eliminating deadlocks. Database `CHECK` constraints guarantee `virtual_balance >= 0` and `quantity >= 0`. |

---

## 3. Detailed Fix Architecture

### 3.1 Rate Limiting Engine (`app/core/rate_limit.py`)
Rate limiting uses a Redis-backed sliding window counter implemented via an atomic Lua script (`SLIDING_WINDOW_LUA`).
- Every request records an entry in a sorted set (ZSET) scored by microsecond timestamps.
- Old timestamps outside the rolling window `(now - window)` are pruned atomically.
- If the current cardinality exceeds the limit, the script computes the exact remaining time to window expiration and returns `[0, remaining, retry_after]`.
- Rate limit metadata is injected into standard response headers:
  - `X-RateLimit-Limit`: configured threshold.
  - `X-RateLimit-Remaining`: available quota in current window.
  - `X-RateLimit-Reset`: Unix timestamp when quota resets.
  - `Retry-After`: seconds to wait (on HTTP 429).

### 3.2 Refresh Token Rotation with Reuse Revocation (`app/services/auth_service.py`)
To prevent replay attacks from compromised refresh tokens:
1. Each refresh token issued contains a cryptographic UUID `token_id` and a lineage `parent_id`.
2. When a refresh token is exchanged, it is immediately marked as consumed in PostgreSQL.
3. If an already-consumed token is presented again (indicating token theft or duplicate transmission):
   - The auth service flags the incident in `audit_logger`.
   - All tokens sharing the family `parent_id` are revoked instantly.
   - The request is rejected with `401 Unauthorized` (`REFRESH_TOKEN_REUSED`).

### 3.3 Strict Numerical Representation & Invariants
- All monetary amounts (USD balances, executed trade totals, realized PnL) and asset holdings are stored as SQL `Numeric(18, 8)` and processed in Python exclusively via `decimal.Decimal`.
- The database enforces hard check constraints at the storage engine level:
  ```sql
  CONSTRAINT check_user_balance_non_negative CHECK (virtual_balance >= 0)
  CONSTRAINT check_holding_quantity_positive CHECK (quantity > 0)
  CONSTRAINT check_trade_quantity_positive CHECK (quantity > 0)
  ```
- Any application-level race condition or logic flaw is physically prevented from persisting inconsistent or negative balances by the database engine.

---

## 4. Consciously Out of Scope

The following items are recognized as out of scope for the current paper-trading simulator iteration:
1. **Multi-Factor Authentication (MFA / 2FA):** TOTP / WebAuthn is not implemented as accounts are virtual simulated trading profiles without real capital risk.
2. **Email Verification & Password Reset Flows:** The system does not integrate an external SMTP / SES provider for transactional email verification; user emails are validated via regex format but not verified via magic links.
3. **Hardware Security Modules (HSM) / KMS:** JWT signing keys are managed via environment variables (`JWT_SECRET`) rather than cloud KMS or Vault-managed asymmetric key pairs (RS256/EdDSA).
4. **Distributed DDoS Mitigation Layer:** Edge network mitigation (e.g., Cloudflare WAF, AWS Shield, L4 SYN flood protection) is assumed to sit in front of the application reverse proxy in production deployments.
5. **Real-world Fiat Banking / Ledger Rails:** No integration with real-world payment processors (Stripe, Plaid) or on-chain crypto settlement networks; all deposits and executions occur against virtual paper-trading ledgers.
