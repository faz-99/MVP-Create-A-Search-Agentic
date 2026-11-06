from pydantic import BaseModel
from typing import Optional, Literal

class AttachToConversationRequest(BaseModel):
    Type: str = ""
    Value: str = ""
    AppKey: str = ""
    DocumentId: str = ""
    SectionId: str = ""
    ThreadId: str = ""

class ChatRequest(BaseModel):
    ConversationId: Optional[str] | None = None
    Prompt: Optional[str] | None = None
    McpUrl: Optional[str] | None = None
    Model: Optional[str] | None = None
    SystemPrompt: Optional[str] | None = None
    Provider: Optional[str] | None = None
    Effort: Optional[Literal["None", "Low", "Medium", "High", "XHigh", "Max"]] = "Medium"
    LexisToken: Optional[str] | None = None
    AuthCookie: Optional[str] | None = None
    Mode: Optional[Literal["Chat", "Sandbox"]] | None  = "Chat"
    Attachments: Optional[list[AttachToConversationRequest]] = None
    IsIncognito: bool = False
    SourceMode: list[str] = ["Igz"]
    ResponseMode: Optional[Literal["Fast", "Thinking"]] = "Thinking"

class SearchRequest(BaseModel):
    Value: Optional[str] = ""
    Type: Optional[str] = "boolean"
    IgzToken: Optional[str] = ""

class RegenerateRequest(ChatRequest):
    ConversationId: str
    Turn: int
    Prompt: str

class ForkRequest(ChatRequest):
    ConversationId: str 
    Source: str
    Turn: int
    Prompt: str

class ShareDetailInfo(BaseModel):
    UserId: str = ""

class ShareRequest(BaseModel):
    ConversationId: str
    ShareId: str
    Type: int = 1
    Users: Optional[list[ShareDetailInfo]] = None
    IgzToken: Optional[str] = None

class PDERequest(BaseModel):
    ConversationId: str
    SelectedOption: Literal["Print", "Download", "Email"] = "Download"
    OutputFormat: Literal["HTML", "PDF", "DOCX"] = "PDF"
    Recipients: list[str] = []
    Filename: str
    IncludePageNumbers: bool = False