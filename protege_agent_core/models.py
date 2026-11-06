from pydantic import BaseModel
from typing import Any
from protege_agent_core.api.request import AttachToConversationRequest

class ModelConfig(BaseModel):
    ApiKey: str = ""
    BaseUrl: str = ""
    

class McpConfig(BaseModel):
    ServerName: str = ""
    Url: str = ""
    Timeout: float = 300
    Headers: dict[str, str]


class AgentRequest(BaseModel):
    Prompt: str
    ConversationId: str = ""
    SystemPrompt: str = ""
    Effort: str = "medium"
    UserId: str = None
    ConversationTitle: str = ""
    Attachments: list[AttachToConversationRequest] | None = []
    ModelConfiguration: ModelConfig = None
    SourceMode: list[str] = ["Igz"]
    Incognito: bool = False
    AdditionalData: dict[str, dict] = None
    ReasoningSummary: str = "detailed"

class AgentConfig (BaseModel):
    DefaultTitleGeneration: bool = True
    ModelApiKey: str = ""
    ModelBaseUrl: str = ""
    Model: str = ""
    McpConfiguration: McpConfig = None
    AgentName: str = "Researcher"
    SystemPrompt: str = ""
    TraceAppKey: str = "Agent"
    ReasoningSummary: str = "detailed"
    
class DoneMessage(BaseModel):
    LastMessage: Any = None
    Activity: list | None = []
    ConversationId: str = ""
    UserId: str = ""
    Model: str = ""
    UserPrompt: str = ""
    duration: float = 0.0