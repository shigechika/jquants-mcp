# Contributing to jquants-mcp

Thank you for your interest in contributing!

## Getting Started

```bash
git clone https://github.com/shigechika/jquants-mcp.git
cd jquants-mcp
uv sync --dev
```

## Development Workflow

1. **Fork** the repository and create a feature branch from `main`:
   ```bash
   git checkout -b feat/your-feature
   ```

2. **Make changes** — keep commits focused and atomic.

3. **Run tests and lint** before pushing:
   ```bash
   uv run ruff check src/ tests/
   uv run ruff format src/ tests/
   uv run pytest -v
   ```
   CI runs the same checks on Python 3.10–3.13.

4. **Open a Pull Request** against `main`. Describe *what* and *why* in the PR body.

## Branch Naming

| Type | Pattern |
|---|---|
| Feature | `feat/<short-description>` |
| Bug fix | `fix/<short-description>` |
| Docs | `docs/<short-description>` |
| Chore | `chore/<short-description>` |

## Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/) style:

```
feat(tools): add get_equities_bars_minute tool
fix(cache): handle empty fins_summary response
docs(deploy): add Docker Compose local setup guide
```

## Code Style

- Python 3.10+ syntax; type hints on all public functions
- `ruff` for lint and format (configured in `pyproject.toml`)
- Tool functions are closures inside `register()`, capturing `get_client` and `get_cache`
- Docstrings in English (for pydoc / IDE); inline comments in English

## Tests

- Tests live in `tests/` and use `pytest` + `pytest-asyncio`
- Patch `server_module._settings`, `_client`, `_cache` globals directly (see existing tests)
- Add tests for new tools and any bug fixes
- All tests must pass on Python 3.10–3.13

## Reporting Issues

- Search [existing issues](https://github.com/shigechika/jquants-mcp/issues) before opening a new one
- Include your J-Quants plan, Python version, and the full error message
- For security issues, please email instead of opening a public issue
