from pydantic import BaseModel
from typing import Optional, Literal

class BaseChatRequest(BaseModel):
    ConversationId: Optional[str] | None = None
    LexisToken: Optional[str] | None = None
    AuthCookie: Optional[str] | None = None