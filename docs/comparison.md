# J-Quants MCP Server Comparison

Survey date: 2026-07-04 — Comparing four known J-Quants MCP server projects.

## Capability Overview

| Dimension | [JPX Official](https://github.com/J-Quants/j-quants-doc-mcp) | [cygkichi](https://github.com/cygkichi/jquants-free-mcp-server) | [umicho](https://lobehub.com/) | **jquants-mcp** |
|---|:---:|:---:|:---:|:---:|
| API Coverage | 1/10 | 2/10 | 5/10 | **9/10** |
| Caching | 0/10 | 0/10 | 0/10 | **10/10** |
| Multi-user | 0/10 | 0/10 | 0/10 | **9/10** |
| Security | 1/10 | 1/10 | 1/10 | **8/10** |
| Deploy Flexibility | 1/10 | 1/10 | 1/10 | **9/10** |
| Test Coverage | 4/10 | 0/10 | 3/10 | **9/10** |

## General Information

| Item | JPX Official | cygkichi/free-mcp | umicho/api-mcp | **jquants-mcp** |
|---|---|---|---|---|
| Purpose | API docs reference & code generation | Data retrieval for free plan | V2 API endpoint calls | Full-plan data retrieval + multi-user service |
| Developer | JPX (Japan Exchange Group) | cygkichi (individual) | umicho (individual) | shigechika (individual) |
| Framework | Python | Python | Python | Python (FastMCP v3) |
| License | MIT | MIT | Unknown | MIT |
| Last Updated | 2026-03-09 (v0.9.0) | Unknown | Unknown | 2026-07-04 (active) |

## Tools & API Endpoint Coverage

| Endpoint | JPX Official | cygkichi | umicho | **jquants-mcp** |
|---|:---:|:---:|:---:|:---:|
| **Total tools** | 4 (doc tools) | 3 | Unknown | **55** |
| Equities master | — | search_company | Unknown | `get_equities_master` |
| Daily bars | — | Yes | Yes | Yes + AM close |
| Minute bars | — | No | Unknown | Yes |
| Financial summary | — | Yes | Unknown | Yes |
| Financial details | — | No | Unknown | Yes |
| Dividend | — | No | Unknown | Yes |
| Earnings calendar | — | No | Unknown | Yes (date-keyed accumulation) |
| Indices (Nikkei 225 / TOPIX) | — | No | Unknown | Yes |
| Derivatives (futures / options) | — | No | Unknown | Yes (3 tools) |
| Market statistics | — | No | Unknown | Yes (6 tools) |
| Investor types | — | No | Unknown | Yes |
| Bulk download | — | No | Unknown | Yes (2 tools) |
| Trading calendar | — | No | Unknown | Yes |

## Transport & Connectivity

| Item | JPX Official | cygkichi | umicho | **jquants-mcp** |
|---|:---:|:---:|:---:|:---:|
| stdio (local) | Yes | Yes | Yes | Yes |
| Streamable HTTP (remote) | No | No | No | **Yes** |
| TLS + Bearer token | No | No | No | **Yes** |
| OAuth 2.1 | No | No | No | **Yes** (Google / GitHub) |
| Supported clients | Claude Desktop, Cursor | Claude Desktop | LobeHub, Claude Desktop | Claude Desktop, Claude Code, Cursor, Connectors UI, generic |

## Caching & Performance

| Item | JPX Official | cygkichi | umicho | **jquants-mcp** |
|---|:---:|:---:|:---:|:---:|
| Cache mechanism | None | None | None | **2-tier SQLite cache** |
| Tier 1 (row-level) | — | — | — | Yes (code × date dedup) |
| Tier 2 (response-level) | — | — | — | Yes (TTL-based) |
| API call reduction | — | — | — | Significant (cached data reused) |

## Multi-user, Auth & Security

| Item | JPX Official | cygkichi | umicho | **jquants-mcp** |
|---|:---:|:---:|:---:|:---:|
| Multi-user | No | No | No | **Yes** |
| Auth method | N/A (docs only) | ID_TOKEN env var | API_KEY env var | Google/GitHub OAuth + per-user API key registration |
| API key encryption | — | No (plaintext env) | No (plaintext env) | **AES-256-GCM + random salt** |
| Plan auto-detection | — | No | No | **Yes** (API probe) |
| Daily API key validation | — | No | No | **Yes** (24h cycle) |
| Input validation | — | No | Unknown | **Yes** (code / date / sector) |
| SQL injection protection | — | — | — | **Yes** (whitelist-based) |
| Audit logging | No | No | No | **Yes** (structured JSON) |
| Error message safety | — | — | — | **Yes** (no internal ID leakage) |
| CSRF protection | — | — | — | **Yes** (/settings forms) |

## Deployment & Operations

| Item | JPX Official | cygkichi | umicho | **jquants-mcp** |
|---|:---:|:---:|:---:|:---:|
| Local execution | Yes (uvx) | Yes (uv/uvx) | Yes (uv) | Yes (uv) |
| Cloud deploy | No | No | No | **Yes** (Cloud Run + GCS) |
| Docker | No | No | No | **Yes** (Dockerfile included) |
| GCS persistence | — | — | — | **Yes** (SQLite writeback) |
| Settings Web UI | No | No | No | **Yes** (/settings) |

## Testing & Code Quality

| Item | JPX Official | cygkichi | umicho | **jquants-mcp** |
|---|:---:|:---:|:---:|:---:|
| Test count | Some (unknown) | None | Some (pytest) | **1,200+ tests** |
| Linter | Ruff | Unknown | Unknown | Ruff |
| CI/CD | GitHub Actions | No | Unknown | **GitHub Actions** (lint + test on Python 3.10–3.13) |
| Python requirement | 3.10+ | Unknown | Unknown | 3.10+ |

## Unique Strengths

| Project | Key Differentiators |
|---|---|
| **JPX Official** | Official backing from JPX; V1→V2 migration support; code generation |
| **cygkichi** | Minimal setup; free plan focus |
| **umicho** | Registered on LobeHub MCP registry; V2 endpoint support |
| **jquants-mcp** | Production-grade architecture; 2-tier cache (3.5 GB proven); multi-user OAuth; AES-256-GCM encryption; audit logging; Cloud Run deployment; 1,200+ automated tests |

## Summary

**JPX Official (j-quants-doc-mcp)** is an API documentation search and code generation tool — it does not retrieve market data. Fundamentally different in purpose.

**cygkichi/jquants-free-mcp-server** is a minimal 3-tool implementation for the free plan. Easy to start, but lacks caching, tests, and extensibility.

**umicho/j-quants-api-mcp** claims V2 API support, but details are unclear. Registered on the LobeHub MCP registry (distribution advantage), but the GitHub repository is not publicly available.

**shigechika/jquants-mcp** is the only project with multi-user OAuth, cloud deployment, encryption, audit logging, and a 2-tier cache. With 55 tools covering nearly all J-Quants API v2 endpoints and 1,200+ automated tests, it operates at a fundamentally different level.

## Positioning

There is effectively no competition in the "remote HTTP/SSE multi-user J-Quants MCP server" category — **jquants-mcp is the only one**.

Future differentiation opportunities: MCP registry listings (LobeHub, etc.), custom domain, expanded documentation, community building.

---

*Generated with Claude — 2026-03-28, refreshed 2026-07-04*
