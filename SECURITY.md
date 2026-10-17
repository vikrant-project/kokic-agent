# Security

This application is designed for a trusted operator on a local Linux/WSL machine. Do not expose its listener publicly without an independent security review and appropriate isolation.

Autonomous mode allows agent commands to run as your Linux user. Private profiles and Git worktrees do not sandbox those commands. Treat MCP servers, plugins, skills and generated code as executable inputs requiring your judgment.

Never commit the runtime directory, `.env` files, databases, account profiles, authorization codes, session cookies or encryption keys. Documentation credentials are fictional and are not provisioned accounts.

If you find a vulnerability, avoid posting credentials or private data in a public issue. Use GitHub private vulnerability reporting if available; otherwise open a minimal issue requesting a private contact channel without disclosing exploit details or secrets.
