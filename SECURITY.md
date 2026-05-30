# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities **privately** via
[GitHub Security Advisories](https://github.com/shigechika/jquants-mcp/security/advisories/new)
rather than opening a public issue.

Include as much detail as you can: affected version, a description of the issue,
and reproduction steps if available. You will receive an acknowledgement, and a
fix or mitigation will be coordinated through the advisory before any public
disclosure.

Please do not include real secrets (API keys, tokens, encryption keys) in a
report — redact them.

## Supported Versions

Security fixes are released against the latest published version only. Run a
recent release before reporting; the issue may already be fixed.

## Scope

jquants-mcp handles sensitive material and is the kind of project where these
areas matter most:

- **Per-user J-Quants API keys** are stored encrypted with AES-256-GCM
  (`crypto.py`). Issues in key storage, encryption, or key rotation are in scope.
- **OAuth / authentication** — token verification, the email allowlist, session
  cookies, and the `/settings` flow.
- **Multi-user isolation** — one user reading or acting as another.

Out of scope: vulnerabilities in the upstream J-Quants API itself, and findings
that require an already-compromised host or a self-inflicted misconfiguration
(for example, deploying with secrets in plain environment variables instead of a
secret manager).
