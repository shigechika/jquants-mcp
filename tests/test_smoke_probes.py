"""Every registered tool must carry a smoke-test probe spec.

This is the CI half of the smoke test: the live run (scripts/smoke_test.py)
needs a real cache and API key, but the *coverage* question — did someone add
a tool without deciding how we would know it works? — is answerable offline,
so it is enforced here on every push.
"""

from __future__ import annotations

import sys
from pathlib import Path

import jquants_mcp.server as server_module

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import smoke_probes  # noqa: E402 - needs the sys.path line above
from smoke_harness import Probe  # noqa: E402


async def _registered_tool_names() -> set[str]:
    return {tool.name for tool in await server_module.mcp.list_tools()}


async def test_every_registered_tool_has_a_probe():
    registered = await _registered_tool_names()
    missing = sorted(registered - set(smoke_probes.PROBES))
    assert not missing, (
        f"Tool(s) registered with no smoke-test probe: {missing}. "
        "Add an entry to scripts/smoke_probes.py — known-good arguments plus what "
        "a working answer looks like, or an explicit skip= reason."
    )


async def test_no_probe_targets_a_removed_tool():
    """A spec for a tool that no longer exists hides drift rather than showing it."""
    registered = await _registered_tool_names()
    stale = sorted(set(smoke_probes.PROBES) - registered)
    assert not stale, f"Probe spec(s) for tools that are no longer registered: {stale}"


async def test_destructive_tools_are_skipped():
    """The smoke test must never call a tool that mutates stored state."""
    for name in ("cache_clear", "register_api_key", "delete_api_key"):
        probe = smoke_probes.PROBES[name]
        assert probe.skip, f"{name} must be skipped, not exercised"


def test_probes_are_probe_instances():
    for name, probe in smoke_probes.PROBES.items():
        assert isinstance(probe, Probe), f"{name} is not a Probe"


def test_specs_use_date_tokens_rather_than_hardcoded_dates():
    """A hardcoded date rots into 'tested nothing' once it ages out of cache."""
    offenders = []
    for name, probe in smoke_probes.PROBES.items():
        for key, value in probe.args.items():
            if isinstance(value, str) and value[:2] in {"19", "20"} and "-" in value:
                offenders.append(f"{name}.{key}={value}")
    assert not offenders, f"use date tokens instead of literal dates: {offenders}"
