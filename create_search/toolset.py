"""Which MCP tools the agent sees, classified from the live tool list by name.

Classes: search (budgeted, see executor), content (never exposed), lookup (everything else in a bundle).
The server sends no tool types, so names decide: dataset searches end in "_search"; shared company_/common_
tools are lookups. drift() logs tools or apps that no longer fit.
"""

from dataclasses import dataclass, field
from typing import Any, Iterable

APP_BUNDLES: dict[str, tuple[str, ...]] = {  # app key -> tool-name prefixes
    "SF": ("sec_filings_",), "AOE": ("agreements_exhibits_",), "CL": ("comment_letters_",), "ECT": ("transcripts_",),
    "SRC": ("securities_regulation_",), "ASG": ("accounting_",), "ESG": ("esg_",), "NAL": ("no_action_letters_",),
    "MA": ("mergers_acquisitions_",), "SEDAR": ("sedar_",), "RO": ("registered_offerings_",), "EO": ("exempt_offerings_",),
    "ENF": ("sec_enforcement_",), "FM": ("firm_memos_",), "ADV": ("adv_registrations_",),
}
SHARED_PREFIXES = ("company_", "common_")
DEFAULT_APPS = ("SF", "AOE")

CONTENT_MARKERS = ("_get_full_content", "_get_chunks_range", "_get_semantic_chunks", "_get_section_full_content", "_get_section_semantic_chunks")
CONTENT_PREFIXES = ("vault_",)


def is_search_tool(name: str) -> bool:
    """A dataset search: name ends in "_search" and it isn't a shared lookup (e.g. company_search)."""
    return name.endswith("_search") and not name.startswith(SHARED_PREFIXES)


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
    """Tools no app covers, and apps with no search tool on the server (e.g. a renamed prefix); empty when clean."""
    names = sorted(t.get("name", "") for t in available)
    problems = [f"'{n}' has no app prefix and is never exposed" for n in names if not is_content_tool(n) and app_of(n) is None]
    searchable = {app_of(n) for n in names if is_search_tool(n)}
    return problems + [f"app '{a}' ({', '.join(p)}) has no search tool on the server" for a, p in APP_BUNDLES.items() if a not in searchable]
