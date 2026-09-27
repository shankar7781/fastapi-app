# AI Question-Answering API

A production-shaped FastAPI service that answers questions via an LLM (Google Gemini), with JWT auth, role-based access control, Redis-backed rate limiting and caching, retry/fallback handling around the LLM call, full audit logging to Postgres, and Prometheus metrics.

## Contents

- [Architecture overview](#architecture-overview)
- [Running locally](#running-locally)
- [Running with Docker Compose](#running-with-docker-compose)
- [API endpoints](#api-endpoints)
- [SSO / OIDC extension path](#sso--oidc-extension-path)
- [RBAC design](#rbac-design)
- [Scaling to 100-500 RPS](#scaling-to-100-500-rps)
- [Migrating from single-server to production scale](#migrating-from-single-server-to-production-scale)

---

## Architecture overview

```
Client → FastAPI (/auth/login, /chat, /health, /metrics)
              │
              ├── Postgres  (users, chat_logs — audit trail)
              ├── Redis     (rate limiting, response cache)
              └── Gemini API (LLM provider, called with retry/backoff)
```

Auth is JWT-based (no server-side session state), so the app itself is stateless and horizontally scalable — state lives only in Postgres and Redis.

## Running locally

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Requires a local Postgres and Redis (see `docker-compose.yml` for matching credentials), and a `.env` file (see `.env.example`) with `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET`, `GEMINI_API_KEY`, etc.

## Running with Docker Compose

```bash
docker-compose up --build
```

This starts the app, Postgres, and Redis together, networked so the app reaches its dependencies by service name (`postgres`, `redis`) rather than `localhost`.

## API endpoints

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/auth/login` | POST | none | Exchange username/password for a JWT |
| `/chat` | POST | admin, user | Ask a question, get an LLM-generated answer |
| `/health` | GET | none | Reports app + dependency (DB/Redis) health |
| `/metrics` | GET | none* | Prometheus metrics |

\* In production, `/metrics` should be restricted to internal/admin access only (network policy or an auth layer), not left open — see RBAC section.

---

## SSO / OIDC extension path

The current auth model issues its own JWTs after checking a username/password against the local `users` table. To extend this to enterprise SSO (Okta, Azure AD, Google Workspace, etc.) via OpenID Connect, the flow becomes:

```
User → App → redirect to IdP (Okta/Azure AD/etc.) → user authenticates with IdP
     → IdP redirects back with an authorization code
     → App exchanges code for an ID token + access token (OIDC token endpoint)
     → App verifies the ID token (signature, issuer, audience, expiry)
     → App issues its OWN internal JWT (same shape as today), mapping the
       IdP's claims (email, groups) to a local role
     → Rest of the system (require_role, /chat, etc.) is unchanged
```

**Why keep issuing our own JWT rather than using the IdP's token directly everywhere:** it keeps every downstream dependency (`get_current_user`, `require_role`, rate limiting keyed by username) exactly as it is today — the IdP becomes a pluggable *front door*, not a rewrite of the whole auth layer. This also means local password-based login and SSO can coexist during a migration, rather than being a hard cutover.

**Key pieces to add:**
- An OIDC client library (e.g. `authlib` or `python-jose` already in use, extended for JWKS verification) to validate ID tokens against the IdP's public signing keys.
- A mapping layer from IdP claims (typically `email`, and a `groups`/`roles` claim if the IdP is configured to send one) to our internal `Role` enum (`admin`/`user`/`readonly`) — this mapping is a deliberate, explicit table (e.g. IdP group `qa-admins` → `admin`), not automatic, so role assignment stays auditable.
- `GET /auth/sso/login` (redirects to IdP) and `GET /auth/sso/callback` (handles the IdP's redirect back, does the code exchange, issues our JWT) as new routes alongside the existing `/auth/login`.
- Users authenticated via SSO either get a `User` row created on first login (just-in-time provisioning) or must already exist locally — a policy decision depending on whether the org wants to pre-provision accounts.

**Trade-off:** SSO adds real complexity (token verification, JWKS key rotation, redirect handling, session/state management during the OAuth dance) in exchange for centralized identity management, MFA inherited from the IdP, and no more locally-stored passwords to protect — the right call once there's a real user directory to integrate with, but unnecessary overhead for a small app with a handful of known users.

---

## RBAC design

Three roles, matching what's implemented in `models.Role`: **admin**, **user**, **readonly**.

| Resource | admin | user | readonly |
|---|---|---|---|
| `POST /chat` | ✅ | ✅ | ❌ (403) |
| `GET /chat/history` (own) | ✅ | ✅ | ✅ |
| `GET /chat/history` (all users) | ✅ | ❌ | ❌ |
| `GET /metrics` | ✅ | ❌ | ❌ |
| User management (create/edit/delete users) | ✅ | ❌ | ❌ |

**Design decisions:**

- **401 vs 403 distinction is deliberate**, not incidental: 401 means "I don't know who you are" (missing/invalid/expired token) — handled by `get_current_user`. 403 means "I know who you are, and you're not allowed" (valid token, wrong role) — handled by `require_role`. Conflating these would leak information (a 401 to a logged-in-but-wrong-role user would incorrectly suggest their session died) and makes debugging access issues harder for both users and whoever's reading the logs.
- **`require_role(*allowed_roles)` as a dependency factory** rather than per-route boolean checks: each protected route declares which roles it accepts in one line (`Depends(require_role("admin", "user"))`), keeping the authorization rule colocated with the route definition rather than scattered through the function body — easy to audit by scanning route signatures alone.
- **`readonly` is excluded from `/chat` entirely**, not given a degraded version of it — the assumption is `readonly` is for users who should see history/results but not consume LLM budget by generating new queries. This is a policy choice that would need revisiting if the real use case is different (e.g. read-only *dashboard* users who never see individual Q&A history either).
- **Generic 401 on login failure** (not "wrong password" vs "user not found") to avoid username enumeration — an attacker probing for valid usernames shouldn't be able to tell them apart from the error message.

---

## Scaling to 100-500 RPS

**Target:** steady 100 requests/second, bursting to 500 requests/second.

```
                         ┌─────────────────┐
 Users ──────────────────▶  Load Balancer  │  (health checks hit /health)
                         └────────┬────────┘
                                  │
                 ┌────────────────┼────────────────┐
                 ▼                ▼                ▼
          ┌───────────┐   ┌───────────┐   ┌───────────┐
          │ FastAPI-1 │   │ FastAPI-2 │   │ FastAPI-N │   ◀── Kubernetes HPA scales
          └─────┬─────┘   └─────┬─────┘   └─────┬─────┘      pods based on CPU / RPS
                │                │                │
                └────────────────┼────────────────┘
                                 ▼
                      ┌─────────────────────┐
                      │  Redis (shared)      │ ── rate limiting + response cache
                      └─────────┬───────────┘
                                 │
                      ┌─────────▼───────────┐
                      │  Background Queue    │ ── async LLM calls, retries
                      │  (Celery / RQ)       │
                      └─────────┬───────────┘
                                 ▼
                      ┌─────────────────────┐
                      │  LLM Provider (Gemini)│ ── rate-limited (RPM/TPM/concurrency)
                      └─────────────────────┘
                                 │
                      ┌─────────▼───────────┐
                      │ Postgres (audit log)  │
                      └─────────────────────┘
```

**Horizontal scaling.** The app is already stateless in the way that matters: no in-memory sessions (JWT auth), no in-memory rate-limit counters or cache (both in Redis already). That means any instance can serve any request — scaling is "run more copies," not a rewrite.

**Load balancing.** Distributes requests across instances and uses the existing `/health` endpoint (which checks real DB/Redis connectivity, not just process liveness) to pull unhealthy instances out of rotation automatically.

**Kubernetes HPA.** Watches a metric — CPU%, or ideally a custom metric derived from the Prometheus `/metrics` endpoint (request rate, LLM latency p99) — and scales pod count within a min/max range (e.g. 3 baseline, up to ~20 under the 500 RPS burst), then back down once load subsides.

**Redis — caching and distributed rate limiting.** The atomic `INCR` + `expire` rate-limit pattern already implemented works correctly across many app instances sharing one Redis, unlike an in-memory counter which would reset per-instance and let a user bypass limits by landing on a different pod. Caching identical questions also directly cuts LLM calls, which matters most at peak load since LLM calls are the slowest, most expensive path.

**Background queues.** The current `/chat` is synchronous — the HTTP request blocks until the LLM responds. Fine at low volume; at 500 RPS, synchronous LLM waits would exhaust available request-handling capacity fast. Fix: `/chat` enqueues the request (Celery/RQ backed by Redis) and returns immediately with a request ID; the client polls or receives a push once the answer is ready. This decouples "accept the request" from "wait on a slow external API."

**Rate limiting — two layers.** Per-user (already built, protects the API from any single user overloading it) and a *global* limiter in front of the LLM provider call itself, since the provider's own limits apply across the whole fleet, not per-instance.

**LLM API limits (RPM/TPM/concurrency).** These are provider-side ceilings independent of how many app instances you run. A shared, Redis-backed token-bucket/semaphore that every instance checks before calling the provider keeps the *aggregate* call rate under the provider's limit, with excess requests queueing rather than failing.

**Concurrent LLM calls.** Handled by a pool of background workers pulling from the queue, making calls concurrently up to a bounded limit that respects the provider's concurrent-connection cap — ideally using an async HTTP client so one process can hold many in-flight calls without a thread per call.

**Failure recovery.** Already the strongest existing piece: `tenacity` retry with exponential backoff on transient errors only, a timeout that fails fast, and a clean fallback to `503` with full audit logging either way. At this scale, add a circuit breaker (stop attempting calls to a provider that's clearly down, for a cooldown window) and a graceful-degradation fallback (cached/generic answer or an honest "try again shortly") rather than a bare error under sustained provider outage.

---

## Migrating from single-server to production scale

**Scenario:** a Python LLM app on one EC2 instance, built for ~10 users, needs to support 10,000.

**Target:** `Users → Load Balancer → Kubernetes/ECS → FastAPI instances → Redis/Queue → LLM Gateway → LLM APIs`

**Scaling the application.** First step is making the app stateless and separating it from its dependencies — state (DB, cache) moves to managed, shared services (RDS, managed Redis) so app instances become disposable and replaceable rather than each holding unique local state.

**Handling LLM API limits.** Centralize LLM calls through a gateway/queue layer so the *aggregate* call rate across all instances respects the provider's RPM/TPM/concurrency ceiling, rather than each instance independently calling the provider.

**Handling slow/failing LLM requests.** Move LLM calls off the synchronous request path into the background queue; apply bounded retry with backoff on transient errors, a circuit breaker for sustained outages, and a real fallback (cached answer, degraded response, or a clear "queued" status) instead of a bare failure.

**Where Redis and queues fit.** Redis: rate limiting (per-user and global-to-provider), response caching, and as the queue's message broker. Queue: decouples request acceptance from LLM processing, absorbs traffic spikes as queue depth rather than dropped/failed requests, and is where retry logic naturally lives (per background task, not blocking a web worker).

**Retries, timeouts, fallbacks.** Same pattern already implemented, moved to the background task layer: bounded retries with exponential backoff on transient errors only, a timeout so no call can hang the system indefinitely, and a fallback once retries exhaust — with the same kind of audit logging (success/error/cached status + real latency) this project already does, since that log is the primary tool for diagnosing slowness at scale.

**Monitoring.** Prometheus + Grafana on the metrics already exposed (request rate, error rate, LLM latency percentiles, token usage, queue depth, cache hit rate), plus alerting on thresholds, plus centralized structured logging — with many replaceable pods, there's no single server to SSH into and read logs from anymore.

**Handling failures.** Multiple replicas mean one pod crashing doesn't take the service down — the orchestrator restarts failed containers automatically. Health checks let the load balancer route around a pod that's up but can't reach its dependencies. The database moves to a managed service with automated backups/failover rather than a single EC2-hosted instance with no redundancy.

**Migrating with minimal downtime.** A strangler-fig approach rather than a hard cutover: stand up the new architecture alongside the existing EC2 server, shift a small percentage of traffic (or specific routes) to it via the load balancer, verify correctness and stability, then gradually increase the shifted traffic (canary/blue-green), keeping the old server as an instant rollback path until confidence is high.

**Managing secrets/configuration.** Move off plaintext `.env` files on a single server to a managed secrets store (AWS Secrets Manager, or Kubernetes Secrets backed by something like Vault), injected into containers at runtime rather than baked into images or committed to source control. Non-secret config (feature flags, thresholds) lives separately (ConfigMaps) so it can be rotated and audited independently of actual secrets.
