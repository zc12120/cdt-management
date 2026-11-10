# Release and rollback

Build and test the source commit before deployment. Pin `CDT_IMAGE` to that build,
create `data/` owned by 10001:10001 and mode 0700, and create a protected `config/`.
Use `[]` for `config/probes.json` when no SOCKS health client is configured.
Create the administrator through the loopback API before publishing the website.
Do not put the administrator password or RAM keys in compose or Git.

`CDT_BASE_PATH` is a random path such as `/random-value`; it is **not a password**.
Publish only that prefix through a 1Panel-managed TLS site, protect it with Basic
authentication, and keep the application's own login. All other public paths,
including health endpoints, must return 404. The reverse proxy must preserve the
prefix, set Host and X-Forwarded-Proto, overwrite X-Forwarded-For, and disable
access logging and caching for this administrative service. Trust only the exact
proxy address in `CDT_TRUSTED_PROXIES`.

Before upgrades, use `scripts/backup.py` against the stopped controller or its
operator-only data directory, and record the previous image digest and compose
file. SQLite backup includes committed WAL data; master.key must be backed up
together. Never copy a live database file alone.

Stage the candidate on a separate loopback port and separate restored data in
`CDT_DRY_RUN=1`, so two controllers cannot operate the same real cloud instance.
After tests pass, stop the old controller, take the final backup, start the new
one and require readiness plus authenticated UI/API checks. On failure, stop
the candidate, restore **both** database and key from the final backup, and start
the previous image. Never run old code against an upgraded database without an
explicit compatible migration plan.

Keep one current and one previous artifact/backup by policy; operational records
and Docker logs have bounded retention. Do not remove the previous copy until
the new version has passed cloud recovery and service probes.

The installed singleton lock prevents multiple worker processes from controlling
the same data directory. Do not scale replicas or set Uvicorn workers > 1.
Cloud acceptance, VM Running, and actual proxy health are distinct checks.
Notifications are retried up to three times via a durable outbox, with possible
duplicate delivery after a crash between external delivery and local commit.
