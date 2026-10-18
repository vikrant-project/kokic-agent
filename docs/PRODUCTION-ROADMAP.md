# Production work plan — local/WSL first

Production readiness is a release criterion, not a feature count. This document tracks the requested 30 capabilities without treating placeholders as implementations.

| # | Capability | Current delivery / remaining work |
|---|---|---|
|1|Automatic failover|Quota failures requeue under every scheduling policy; another eligible account continues in the same worktree.|
|2|Checkpoints|Snapshots every 60 seconds and after failures include file status and recent WORK.md; workers receive the last checkpoint. Last 30 snapshots retained per task.|
|3|Smart model routing|Role-specific model overrides exist. Automatic task-dependent model selection remains.|
|4|Quota-aware scheduling|Available accounts sorted by reported model-group quota; no invented percentages.|
|5|Task board|Live state lanes, account, phase, attempts, code and activity actions.|
|6|Agent activity|Existing native tool events and terminal, plus Control Center counts.|
|7|Collaboration|Existing project-scoped MCP delegation and child merges. Dependency graph orchestration remains.|
|8|Independent review|Second eligible account can review a completed task on a separate branch.|
|9|Tests|Explicit Python unittest and JavaScript syntax checks with actual results. More project types remain.|
|10|Browser tests|Not implemented. Requires a browser-test worker and verified artifact capture.|
|11|Split preview|Chat and local static preview can share the workspace.|
|12|Click-to-edit|Not implemented. Needs a safe preview annotation bridge.|
|13|Version history|Main history and non-destructive restore commits.|
|14|GitHub sync|Initial public source release exists; in-product authenticated push and PR flow remain.|
|15|Deployments|Not implemented; local preview only. Requires a selected hosting adapter and credentials.|
|16|Templates|Static website, Python API and document-brief starters available at project creation. More stacks remain.|
|17|Memory|Owner-scoped editable decisions included in future requests.|
|18|Search|Owner-scoped tasks, messages and main-branch source search, bounded to 1000 source files.|
|19|Integration health|Configuration management exists; runtime checks and authenticated health remain.|
|20|Secrets|Encrypted project values, worker environment injection, reserved-name checks and best-effort output redaction.|
|21|Isolation|Not implemented. Worktrees are not containers; Docker binary availability is diagnostic only.|
|22|Approvals|Project-level autonomous or native permission review modes. Granular action policies remain.|
|23|Artifacts|Project-wide real artifact library with owner-scoped downloads.|
|24|Scheduling|One-time local jobs claimed transactionally, respect paused projects. Recurrence remains.|
|25|Notifications|Owner-scoped in-app feed and read status. OS/email delivery remains.|
|26|Usage|Actual task counts, attempts and timestamps. Provider token and cost reporting remains unavailable.|
|27|Backup|Plain or passphrase-encrypted main-source and project-memory backups, restored as new projects. Credentials, sessions, secrets and Git history are intentionally excluded; full runtime recovery remains.|
|28|Team workspace|Not implemented. Current project ownership stays private to one local user.|
|29|Mobile UI|Responsive controls, task board and Control Center; device/browser QA remains.|
|30|Voice|Browser speech recognition when supported, editable transcript before send; unavailable-browser feedback.|

## New-account onboarding

New accounts receive a private profile, Kokic identity, bundled skills and a native settings file automatically. User allow rules are preserved. Every build configures the profile again and uses the project execution mode. Google authorization still requires the account owner. Model catalog synchronization and native usage reporting remain provider-dependent.

## Release gates still required

- Validate restart, native limit handoff, concurrent jobs and account onboarding against multiple real accounts.
- Define retention limits for notices, checkpoints and events; add migration and recovery testing.
- Isolate test/build subprocesses, bound resource use and review the public-network threat model before any hosted deployment.
- Test supported browsers, narrow layouts, accessibility and artifact previews.
- Complete outstanding feature rows, review security boundaries, and run integration/load/recovery checks before calling this production-ready.
