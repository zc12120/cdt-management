# Fork 1.4.0: reliable control and protected deployment

Upstream baseline: 3ccf6a7. The assessment branch is historical; this implementation fixes the reproduced problems and adds release gates before deployment.

Changes:

- Authentication routes are isolated in `http/auth.py`. Administrator initialization is transactional, stale/revoked session failures fail closed, private proxy addresses are not automatically trusted, cookies can be Secure and scoped to an operator-selected random prefix.
- Short SQLite read/modify/write operations use explicit immediate transactions. Event steps cannot overwrite concurrent appends. Manual operations reserve a persistent intent before cloud I/O and serialize against the scheduler; conflicting requests wait for actual prior cloud state confirmation.
- `accounts.py` and versioned migrations separate encrypted credentials and verified cloud account identities from instances. Usage aggregation counts verified accounts once; unknown identities are shown as unresolved instead of added as independent quotas. Rotation does not create another verified account allowance.
- `observations.py` independently collects ECS and traffic. Failed traffic queries retain timestamps and known over-limit values. `policy.py` explicitly chooses conservative cost protection or operator-selected availability priority. The default pauses starts after stale usage expires; it never invents a zero reading on errors.
- Cloud requests use HTTPS, parse per-instance result codes, translate ordinary stop to KeepCharging and paginate bills with repeated-token guards. Errors do not propagate raw signed request URLs into chained exceptions.
- Schedule calculations moved to `scheduling.py`; manual command execution moved to `controls.py`; application lifecycle and readiness are separate from HTTP liveness. The mature group transition implementation is retained behind those boundaries so its existing tested timing/failover behavior is preserved.
- A controller file lock prevents accidental multiple worker processes. Readiness requires active scheduler/confirmation threads, recent heartbeats and writable storage. Shutdown stops workers and closes connections.
- Notifications use a persistent bounded outbox outside the cloud-control request path. An optional protected probe configuration performs real HTTPS egress checks through a local SOCKS client and distinguishes degraded proxy health from VM Running, without automatic reboot loops.
- The frontend uses a dedicated non-overlapping polling hook, displays account-scoped traffic and exposes the stale-usage policy. Relative assets and prefix-aware API requests work beneath a random URL prefix.
- Runtime dependencies are hash-locked; base images are pinned by manifest digest. PR CI, release testing, fork image/update targets, loopback-only defaults, consistent SQLite/key backups and installer rollback replace the upstream publishing assumptions.

Validation on Python 3.13.18 / Node 22.22.2:

- 371 unit tests in 25 isolated processes passed, including concurrency, migration, backup integrity, auth/prefix, cloud-response and outbox tests.
- The HTTP smoke suite passed all 141 checks against a temporary dry-run server.
- TypeScript and Vite production build passed; undefined-name static checks passed.
- Chromium loaded the random-prefix site, initialized a synthetic administrator, rendered the dashboard and fetched the authenticated overview with no browser page errors. Static asset requests are also covered by regression tests.
- The existing zero-known-vulnerability dependency audit was retained for the same resolved runtime versions; the lockfile is the deployment source of truth, not floating requirements.

No production credentials, instance IDs, domain paths, authentication material or real deployment state are committed. Configuration and backup files remain operator-only outside Git. The implementation cannot guarantee available spot capacity, zero cloud-metering delay, exactly-once external notification delivery, or absence of all latent bugs. Those boundaries are explicit rather than reported as successful checks.

Deployment is gated on this commit being pushed to the user's fork. Production readiness, TLS/authentication checks, recovery after a controlled ECS stop and actual proxy egress are separate subsequent acceptance steps; their result belongs in the private deployment ledger.
