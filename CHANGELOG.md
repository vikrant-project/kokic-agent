# Changelog

## 2026-10-18 — Local workspace update

### Added
- Project Control Center with saved memory, search, schedules, notifications, artifacts and runtime diagnostics.
- Quota-triggered handoff across all scheduling modes with preserved worktrees and resume checkpoints.
- Periodic progress snapshots every 60 seconds, retaining the most recent 30 per task.
- Automatic private account setup with Kokic identity, bundled skills, settings and idle model-catalog synchronization.
- Task board, split chat/preview view, browser-dependent dictation and project starter templates.
- Encrypted project secrets with reserved environment-variable checks and best-effort plaintext output redaction.
- Main-branch history with restore commits, explicit local checks and second-account reviews.
- Passphrase-encrypted source backups and restore into new projects without overwriting existing projects.

### Fixed
- Planning parser handles JSON surrounded by Markdown or prose and validates routing fields.
- Malformed planning responses receive one bounded correction attempt.
- Streamed planning text can be consumed when the CLI final response field is empty.
- Planner instructions explain that uploaded brief text is already supplied and must not be reopened using tools.
- Source exports exclude common secret-file extensions and environment variants.

### Validation and limits
- 40 Python tests passed locally; JavaScript syntax and Markdown-rendering checks passed.
- Local/WSL development release. All 30 roadmap capabilities are not complete or production-certified.
- No container isolation, public-hosting integration or shared team permissions are claimed.
- Portable backups exclude provider credentials, vault secrets, sessions, databases and Git history.
- Real account information, runtime files and personal screenshots are excluded from the public source release.

See [the production roadmap](docs/PRODUCTION-ROADMAP.md) for remaining work.
