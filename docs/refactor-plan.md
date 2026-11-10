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
- [x] Push verified implementation before deployment
- [x] 1Panel-managed SFO website, exact Cloudflare DNS and TLS verification
- [x] Controlled ECS stop → automatic restart → real Reality request

Runtime release: `9222c0fa41dd6027fc472c2a8086ba60d1bdc36d`. Both main-branch CI jobs passed. The subsequent checklist update is documentation-only; the deployed image remains tied to that verified runtime commit. Detailed deployment evidence and access information are retained privately outside this repository.

No claim of zero possible defects. Release gates cover the observed failures and explicitly exercised failure modes. Preserve a rollback artifact and report limits of real spot-capacity recovery.
