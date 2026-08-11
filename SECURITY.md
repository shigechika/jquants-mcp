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
- **Authentication happens outside this process.** The server is stdio-only,
  binds no network socket, and verifies no tokens itself. On the hosted
  deployment an `oauth2-proxy` + `mcp-stdio serve` gateway authenticates the
  user and passes the verified email to a per-session child process; a flaw in
  that gateway belongs to the gateway's project, not here. What *is* in scope
  here is what this server does with the identity it is handed — above all the
  `JQUANTS_ALLOWED_EMAILS` email allowlist (`allowlist.py`), and any way to
  make the process act on an identity the gateway did not issue.
- **Multi-user isolation** — one user reading or acting as another.

Out of scope: vulnerabilities in the upstream J-Quants API itself, and findings
that require an already-compromised host or a self-inflicted misconfiguration
(for example, deploying with secrets in plain environment variables instead of a
secret manager).
