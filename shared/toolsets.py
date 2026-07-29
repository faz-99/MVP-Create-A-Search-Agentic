"""Which MCP tools each phase is allowed to touch.

Execution is user-gated, so the build phase must not be *able* to run a search — a
withheld tool is a guarantee, a prompt instruction is a request. This module owns
that split for both providers.

EXECUTE_TOOLS is a denylist rather than an allowlist of build tools: the build phase
should get every new lookup or validation tool the server grows, automatically, while
still being unable to execute.
"""

from typing import Any

from shared.config import MCP_SERVER_NAME, MCP_SERVER_URL

# Confirmed against igz-demo v3.4.2 via `python -m probe.probe_mcp`.
#
# There is no single /Search tool. The server exposes one search executor per
# dataset, so a created search must name which one it targets — see SearchPlan.tool.
# These nine are withheld from the build phase and are the only tools the Run button
# will invoke.
EXECUTE_TOOLS: set[str] = {
    "accounting_search_standards_guidance",
    "aoe_search_agreements_and_exhibits",
    "comment_letters_search_comments_responses",
    "dbm_sections_search",  # renamed to filing_sections_search; kept so an older
    "filing_sections_search",  # server deployment stays gated too
    "esg_search_reports",
    "no_action_letters_search",
    "sec_filings_search",
    "sec_rules_search",
    "transcripts_search",
}

# Tools whose names contain "search" but which only look things up — they resolve ids,
# list taxonomies, or match mappings, and the build phase needs them. Listed
# explicitly so that a NEW search tool cannot slip through unclassified: see
# unclassified_search_tools().
KNOWN_LOOKUP_TOOLS: set[str] = {
    "accounting_search_pasu_mappings",
    "aoe_search_document_types",
    "comment_letters_search_topic_section_mappings",
    "company_search",
    "company_search_by_filters",
    "company_search_detailed",
    "search_asc_topics",
    "search_asu_updates",
    "search_headquarters_addresses",
    "search_law_firms",
    "search_naics_codes",
    "search_sic_codes",
    "sec_filings_find_form_types",
    "sec_filings_find_sections",
    "sec_filings_find_section_type_mappings",
}


def unclassified_search_tools(available: list[dict[str, Any]]) -> list[str]:
    """Search-looking tools that are in neither list — i.e. a possible gate hole.

    This exists because the gate silently sprang a leak: the server renamed
    `dbm_sections_search` to `filing_sections_search`, and since the executor list is
    hardcoded, the renamed tool became callable during the build phase. A hardcoded
    allowlist cannot notice a rename, so the drift has to be detected and surfaced
    rather than assumed away.
    """
    return sorted(
        name
        for name in (t.get("name", "") for t in available)
        if "search" in name
        and name not in EXECUTE_TOOLS
        and name not in KNOWN_LOOKUP_TOOLS
    )

# Deliberately NOT withheld: the *_get_full_content / *_get_semantic_chunks /
# *_get_chunk_range retrieval tools. They require a documentId that only a search
# returns, so they are inert during the build phase; withholding them would add
# config surface for no gain. The resolver and taxonomy tools (company_search,
# sec_filings_find_form_types, search_sic_codes, resolve_country, list_*) are the
# ones the build phase actively needs.


def is_execute_tool(name: str) -> bool:
    return name in EXECUTE_TOOLS


def resolve_execute_tool(available: list[dict[str, Any]]) -> str | None:
    """First execute tool present on the server. Diagnostic only.

    Used by the probe to confirm the gate matches reality. It is NOT how the Run
    button picks a tool — with nine search executors there is no single answer, so
    the created search names its own target (SearchPlan.tool).
    """
    for tool in available:
        if is_execute_tool(tool.get("name", "")):
            return tool["name"]
    return None


def available_execute_tools(available: list[dict[str, Any]]) -> list[str]:
    """Every execute tool the server actually advertises."""
    return sorted(t["name"] for t in available if is_execute_tool(t.get("name", "")))


def build_phase_tool_names(available: list[dict[str, Any]]) -> list[str]:
    """Every tool except the ones that execute a search."""
    return [t["name"] for t in available if not is_execute_tool(t.get("name", ""))]

# The hosted-connector helpers (claude_build_tools / claude_mcp_servers /
# openai_build_tools) were removed. They configured provider-side MCP connectors,
# which cannot authenticate to this server: auth is a custom `lna` header and no
# provider can send one. The loop in shared/loop.py registers the build-phase tools
# directly instead — see build_phase_tools there.
