# Development rules

Pisar is an offline Python >=3.11 CLI for Git-backed knowledge repositories.
Canonical documents, `.wiki.toml` and `wiki:` links are data formats, not branding.
Keep runtime state outside all Git repositories. Preserve source bytes and user
changes. Personal/work boundaries and explicit global-inbox acceptance are mandatory.

Use test-first changes and run the full unittest suite before claiming success.
Tests must use temporary data roots, never live knowledge or task data.
Never store credentials, private knowledge, agent logs or workstation-specific
session metadata in this public repository. No external model/index service.
Use Conventional Commits. Release-please owns version bumps and changelog.
Only publish, push or change GitHub settings when explicitly authorized by the user.
Implementation workers do not publish; the supervisor owns publication and release.
