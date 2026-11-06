"""Which MCP tools the agent sees, bundled by tool-name prefix.

Classes: search (budgeted, see executor), content (never exposed), lookup (everything else in a bundle).
SEARCH_TOOLS is explicit so a server rename can't silently change a tool's class; drift() reports it.
"""

from dataclasses import dataclass, field
from typing import Any, Iterable

APP_BUNDLES: dict[str, tuple[str, ...]] = {  # app key -> tool-name prefixes
    "SF": ("sec_filings_",), "AOE": ("aoe_",), "CL": ("comment_letters_",), "ECT": ("transcripts_",),
    "SRC": ("securities_regulation_",), "ASG": ("accounting_",), "ESG": ("esg_",), "NAL": ("no_action_letters_",),
    "MA": ("mergers_acquisitions_",), "SEDAR": ("sedar_",), "RO": ("registered_offerings_",), "EO": ("exempt_offerings_",),
    "ENF": ("sec_enforcement_",), "FM": ("firm_memos_",), "ADV": ("adv_registrations_",),
}
SHARED_PREFIXES = ("company_", "common_")
DEFAULT_APPS = ("SF", "AOE")

SEARCH_TOOLS = frozenset({
    "sec_filings_search", "aoe_search_agreements_and_exhibits", "comment_letters_search_threads", "transcripts_search",
    "securities_regulation_search", "accounting_search_standards_guidance", "esg_search_reports_and_governance",
    "no_action_letters_search", "mergers_acquisitions_search_transactions", "sedar_search_filings", "registered_offerings_search",
    "exempt_offerings_search", "sec_enforcement_search", "firm_memos_search", "adv_registrations_search",
})
SEARCH_NAMED_LOOKUPS = frozenset({"company_search"})  # lookups with "search" in the name

CONTENT_MARKERS = ("_get_full_content", "_get_chunks_range", "_get_semantic_chunks", "_get_section_full_content", "_get_section_semantic_chunks")
CONTENT_PREFIXES = ("vault_",)


def is_search_tool(name: str) -> bool:
    return name in SEARCH_TOOLS


def is_content_tool(name: str) -> bool:
    return name.startswith(CONTENT_PREFIXES) or any(m in name for m in CONTENT_MARKERS)


def app_of(name: str) -> str | None:
    """App key of a tool, "SHARED" for shared lookups, None if unknown."""
    if name.startswith(SHARED_PREFIXES):
        return "SHARED"
    return next((app for app, prefixes in APP_BUNDLES.items() if name.startswith(prefixes)), None)


def normalize_apps(apps: Iterable[str] | None) -> list[str]:
    """Upper-cased, de-duplicated, order kept. "ALL" expands to every bundle."""
    result: list[str] = []
    for app in apps or DEFAULT_APPS:
        key = app.strip().upper()
        if key == "ALL":
            return list(APP_BUNDLES)
        if key not in APP_BUNDLES:
            raise ValueError(f"Unknown app '{app}'. Known: {', '.join(APP_BUNDLES)}, ALL")
        if key not in result:
            result.append(key)
    return result


@dataclass
class Toolset:
    apps: list[str]
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def search_tools(self) -> list[str]:
        return sorted(n for n in self.tools if is_search_tool(n))

    def schema(self, name: str) -> dict[str, Any] | None:
        tool = self.tools.get(name)
        return (tool.get("inputSchema") or {}) if tool else None


def select_tools(available: list[dict[str, Any]], apps: Iterable[str] | None) -> Toolset:
    """Shared lookups + the named apps' bundles, minus content tools."""
    selected = normalize_apps(apps)
    wanted = {"SHARED", *selected}
    return Toolset(apps=selected, tools={t["name"]: t for t in available
                                         if not is_content_tool(t.get("name", "")) and app_of(t.get("name", "")) in wanted})


def drift(available: list[dict[str, Any]]) -> list[str]:
    """Problems with the classification above; empty when clean."""
    names = {t.get("name", "") for t in available}
    problems: list[str] = []
    for name in sorted(n for n in names if not is_content_tool(n)):
        if app_of(name) is None:
            problems.append(f"'{name}' has no bundle prefix and is never exposed")
        elif "search" in name and name not in SEARCH_TOOLS | SEARCH_NAMED_LOOKUPS:
            problems.append(f"'{name}' looks like a search but is not in SEARCH_TOOLS")
    return problems + [f"'{n}' is in SEARCH_TOOLS but the server no longer has it" for n in sorted(SEARCH_TOOLS - names)]
