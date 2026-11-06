"""Create Search on Intive's ProtegeBaseAgent (protege_agent_core, kept as shipped)."""

import os
from typing import Any, AsyncIterator, Iterable

from protege_agent_core import utils as template_utils

# base_agent fetches MODELS_SECRETS at import; without it (local runs) mark keys loaded so it skips.
if not os.getenv("MODELS_SECRETS"):
    template_utils.keys.setdefault("openai", {})

from protege_agent_core.base_agent import ProtegeBaseAgent  # noqa: E402
from protege_agent_core.models import AgentConfig, AgentRequest, McpConfig  # noqa: E402

from create_search import config  # noqa: E402
from create_search.run import create_search  # noqa: E402


class CreateSearchAgent(ProtegeBaseAgent):
    """Template contract; stream_research runs our own loops instead of the template's runner."""

    def build_agent_input(self, prompt, attachments=None, additional_data=None):
        return self._build_agent_input_default(prompt, attachments)

    def get_extra_tools(self, request, event_queue):
        return []

    def get_hooks(self, agent_request):
        return None

    def process_tool_output(self, tool_args, tool_output, additional_data=None):
        return None

    async def stream_research(self, request: AgentRequest, retry: int = 0, *, provider: str = "openai",
                              apps: Iterable[str] | None = None, stop_key=None) -> AsyncIterator[dict[str, Any]]:
        async for ev in create_search(request.Prompt, provider=provider, apps=apps,
                                      token=self.config.McpConfiguration.Headers.get(config.AUTH_HEADER, ""),
                                      model=self.config.Model or None, effort=request.Effort,
                                      conversation_id=request.ConversationId or None, stop_key=stop_key):
            yield ev


def create_agent(token: str, model: str | None = None) -> CreateSearchAgent:
    mcp = McpConfig(ServerName=config.MCP_SERVER_NAME, Url=config.MCP_SERVER_URL, Timeout=config.MCP_TIMEOUT,
                    Headers={config.AUTH_HEADER: token})
    return CreateSearchAgent(AgentConfig(Model=model or "", AgentName="CreateSearch", McpConfiguration=mcp,
                                         DefaultTitleGeneration=False))
