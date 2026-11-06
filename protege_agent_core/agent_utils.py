from agents import ( Agent, ModelSettings)
from agents.mcp import MCPServerStreamableHttp
from agents.model_settings import ModelSettings
from protege_agent_core.utils import MCP_TIMEOUT, get_openai_model
from protege_agent_core.shared_transport import pooled_client_factory
import httpx


def create_mcp_streamable(server_name: str,
                                mcp_url: str,
                                headers: dict[str, str],
                                timeout: int):
    return MCPServerStreamableHttp(
			name=server_name,
			params={
				"url": mcp_url,
				"headers": headers,
				"timeout": timeout,
				"httpx_client_factory": pooled_client_factory,
			},
			client_session_timeout_seconds=MCP_TIMEOUT,
			cache_tools_list=True,
			max_retry_attempts=3,
		) 

def create_agent(agent_name: str,
                 instructions: str,
                 api_key: str,
                 base_url: str,
                 model:str,
                 tools: list,
                 model_settings: ModelSettings):
    agent = Agent(
                    name=agent_name,
                    instructions=instructions,
                    model=get_openai_model(api_key=api_key, base_url=base_url, model_name=model),
                    tools=tools,
                    reset_tool_choice=True,
                    model_settings=model_settings,
                )
    return agent