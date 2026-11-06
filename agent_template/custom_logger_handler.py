from agent_template.tracking_service import publish_message
from agent_template.utils import utc_now
from dotenv import load_dotenv
import logging
import os
load_dotenv()
class CustomLogger(logging.Handler):
    
    enabled: bool = False
    def __init__(self, level = 0):
        super().__init__(level)
        self.enabled = os.getenv("ENABLE_NETWORK_TRACE", "false").lower() == "true"
    
    def emit(self, record):
        if self.enabled:
            self.format(record=record)
            if record.name.startswith(("httpx", "openai", "httpcore")):
                obj = {"message": record.message, "DateTime": utc_now()}
                publish_message(message=obj, custom_exchange="network_trace")
                
