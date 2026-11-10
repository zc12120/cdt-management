# Refactor and deployment acceptance

Sequence: implement and verify locally → push the reviewed implementation to the fork → deploy that commit on SFO → perform a controlled stop/recovery and real proxy test.

- [x] Atomic admin setup, session validation, trusted proxies and secure cookies
- [x] Serialized/journaled controls and atomic event progress
- [x] Independent cloud/usage observations with explicit stale-data policy
- [x] Account/credential separation and deduplicated account usage
- [x] Validated cloud responses, stop-mode translation and billing pagination
- [x] Scheduler lifecycle, singleton ownership and readiness
- [x] Engine boundaries, pure policy functions and frontend data hooks
- [x] Optional external service health, bounded notification delivery
- [x] Versioned migration, consistent backup/restore and regression coverage
- [x] Fork release configuration, locked dependencies and CI
- [x] Random URL prefix, reverse-proxy authentication and panel authentication
- [ ] Push verified implementation before deployment
- [ ] 1Panel-managed SFO website, exact Cloudflare DNS and TLS verification
- [ ] Controlled ECS stop → automatic restart → real Reality request

No claim of zero possible defects. Release gates cover the observed failures and explicitly exercised failure modes. Preserve a rollback artifact and report limits of real spot-capacity recovery.
