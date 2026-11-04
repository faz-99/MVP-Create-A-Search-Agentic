"""Which MCP tools the agent sees, and how each one is treated.

The server advertises one flat list (73 tools on igz-demo v3.4.2), so bundles are built
here from the tool-name prefix. A request names its apps — primary first — and the agent
gets those bundles plus the shared company/common lookups.

Every tool falls in one class:

  search    runs a dataset search. Callable during the build, but budgeted, its page
            size clamped, and its result summarised (core/executor.py).
  content   reads document text. Out of scope for Create Search, never exposed.
  lookup    everything else in a known bundle: resolves names to ids, lists taxonomies.

SEARCH_TOOLS is explicit because many lookups also contain "search" (`company_search`)
and a rename on the server would otherwise change a tool's class silently — the POC's
gate leaked exactly that way. `drift()` reports anything that no longer fits.
"""

from dataclasses import dataclass, field
from typing import Any, Iterable

# App key -> tool-name prefixes. Keys follow the in-app AppKey values.
APP_BUNDLES: dict[str, tuple[str, ...]] = {
    "SF": ("sec_filings_",),
    "AOE": ("aoe_",),
    "CL": ("comment_letters_",),
    "ECT": ("transcripts_",),
    "SRC": ("securities_regulation_",),
    "ASG": ("accounting_",),
    "ESG": ("esg_",),
    "NAL": ("no_action_letters_",),
    "MA": ("mergers_acquisitions_",),
    "SEDAR": ("sedar_",),
    "RO": ("registered_offerings_",),
    "EO": ("exempt_offerings_",),
    "ENF": ("sec_enforcement_",),
    "FM": ("firm_memos_",),
    "ADV": ("adv_registrations_",),
}

# Shared lookups every bundle may use (company resolution, SIC/NAICS, countries...).
SHARED_PREFIXES: tuple[str, ...] = ("company_", "common_")

# Apps the agent gets when the request does not name any.
DEFAULT_APPS: tuple[str, ...] = ("SF", "AOE")

SEARCH_TOOLS: frozenset[str] = frozenset(
    {
        "sec_filings_search",
        "aoe_search_agreements_and_exhibits",
        "comment_letters_search_threads",
        "transcripts_search",
        "securities_regulation_search",
        "accounting_search_standards_guidance",
        "esg_search_reports_and_governance",
        "no_action_letters_search",
        "mergers_acquisitions_search_transactions",
        "sedar_search_filings",
        "registered_offerings_search",
        "exempt_offerings_search",
        "sec_enforcement_search",
        "firm_memos_search",
        "adv_registrations_search",
    }
)

# Lookups whose names contain "search". Anything else with "search" in its name that is
# not in SEARCH_TOOLS is reported by drift().
SEARCH_NAMED_LOOKUPS: frozenset[str] = frozenset({"company_search"})

CONTENT_MARKERS: tuple[str, ...] = (
    "_get_full_content",
    "_get_chunks_range",
    "_get_semantic_chunks",
    "_get_section_full_content",
    "_get_section_semantic_chunks",
)
CONTENT_PREFIXES: tuple[str, ...] = ("vault_",)


def is_search_tool(name: str) -> bool:
    return name in SEARCH_TOOLS


def is_content_tool(name: str) -> bool:
    return name.startswith(CONTENT_PREFIXES) or any(m in name for m in CONTENT_MARKERS)


def app_of(name: str) -> str | None:
    """The app key a tool belongs to, "SHARED" for shared lookups, None if unknown."""
    if name.startswith(SHARED_PREFIXES):
        return "SHARED"
    for app, prefixes in APP_BUNDLES.items():
        if name.startswith(prefixes):
            return app
    return None


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
    """The tools selected for one request, keyed by name."""

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
    toolset = Toolset(apps=selected)
    for tool in available:
        name = tool.get("name", "")
        if is_content_tool(name):
            continue
        if app_of(name) in wanted:
            toolset.tools[name] = tool
    return toolset


def drift(available: list[dict[str, Any]]) -> list[str]:
    """Human-readable problems with the classification above, empty when clean."""
    problems: list[str] = []
    names = {t.get("name", "") for t in available}
    for name in sorted(names):
        if is_content_tool(name):
            continue
        if app_of(name) is None:
            problems.append(f"'{name}' has no bundle prefix and is never exposed")
        elif "search" in name and name not in SEARCH_TOOLS and name not in SEARCH_NAMED_LOOKUPS:
            problems.append(f"'{name}' looks like a search but is not in SEARCH_TOOLS")
    for name in sorted(SEARCH_TOOLS - names):
        problems.append(f"'{name}' is in SEARCH_TOOLS but the server no longer has it")
    return problems
