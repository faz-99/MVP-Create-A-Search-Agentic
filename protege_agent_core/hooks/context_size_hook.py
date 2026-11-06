import json
from dataclasses import dataclass, asdict

from agents import Agent, RunHooks
from agents.run_context import RunContextWrapper
from agents.run_internal.turn_preparation import get_handoffs
from agents.models.openai_responses import Converter
from agents.items import TResponseInputItem
from protege_agent_core.tracking_service import publish_message
from protege_agent_core.utils import utc_now, env_flag, parse_json, update_artifact_index
from pathlib import Path
from typing import Any
import sys 

if sys.platform != "win32":
    from src.sandbox_utils import ensure_sandbox_manifest, write_workspace_output
APPROX_BYTES_PER_TOKEN = 4

def approx_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + APPROX_BYTES_PER_TOKEN - 1) // APPROX_BYTES_PER_TOKEN
# MAX_SIZE_TOOL = 1048576 / 2
MAX_SIZE_TOOL = 524288
SANDBOX_TOOL_ENABLED = env_flag("SANDBOX_TOOL_ENABLED", False)
@dataclass
class ContextBreakdown:
    instructions_tokens: int
    input_items_tokens: int
    tools_tokens: int
    total_tokens: int

class ContextSizeHooks(RunHooks):
    
    def __init__(self, conversation_id: str, user_id: str, model: str, model_char_limit: int = 32000000, 
                 max_turns: int = 50, warn_before: int = 5,
                 sandbox_working_dir: str = None):
        super().__init__()
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.model = model
        self.max_chars = model_char_limit
        self.current_turn = 0
        self.max_turns = max_turns
        self.warn_before = warn_before
        if sandbox_working_dir:
            self.sandbox_working_dir = Path(sandbox_working_dir) / user_id / conversation_id

    async def on_llm_start(
        self,
        context: RunContextWrapper,
        agent: Agent,
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        tools = await agent.get_all_tools(context)
        handoffs = await get_handoffs(agent, context)
        self.current_turn += 1 
        converted = Converter.convert_tools(
            tools=tools,
            handoffs=handoffs,
            model=agent.model if isinstance(agent.model, str) else None,
        )
        current_size = 0
        new_items = []
        names = {}
        for i in input_items:
            current_size = current_size + len(str(i))
            if "type" in i:
                if i["type"] == "function_call":
                    names[i["call_id"]] = i["name"]

                if i["type"] == "function_call_output":
                    for o in i["output"]:
                        if "text" in o and len(o["text"]) > MAX_SIZE_TOOL or current_size > self.max_chars:
                            if current_size > self.max_chars:
                                o["text"] = "[Input context trimmed] Tool removed due to large context window"
                                current_size = current_size - len(str(i))
                            else:
                                if sys.platform == "win32" or not SANDBOX_TOOL_ENABLED:
                                    o["text"] = "[Content trimmed Tool too long] Check if there are chunks."
                                    
                                else:
                                    working_dir = self.sandbox_working_dir / "processed" / "tool_outputs"
                                    working_dir.mkdir(parents=True, exist_ok=True)
                                    call_id = i["call_id"]
                                    parsed = parse_json(o["text"])
                                    if parsed:
                                        artifact_path = working_dir / f"{call_id}.json"
                                        if not artifact_path.exists():
                                            artifact_path.write_text(parsed, encoding="utf-8")
                                            artifact_entry = {
                                                "entry_key": f"{names[call_id]}:{call_id}",
                                                "created_at": utc_now(),
                                                "kind": "tool_output",
                                                "tool_name": names[call_id],
                                                "call_id": call_id,
                                                "path": artifact_path.relative_to(self.sandbox_working_dir).as_posix(),
                                                "bytes": artifact_path.stat().st_size,
                                            }

                                            outputs = self.sandbox_working_dir / "outputs"
                                            outputs.mkdir(parents=True, exist_ok=True)
                                            index_updated = update_artifact_index(workspace_dir=self.sandbox_working_dir, artifact_entry=artifact_entry)
                                    else:
                                        artifact_path = working_dir / f"{call_id}.txt"
                                        if not artifact_path.exists():
                                            artifact_path.write_text(o["text"], encoding="utf-8")
                                            artifact_entry = {
                                                "entry_key": f"{names[call_id]}:{call_id}",
                                                "created_at": utc_now(),
                                                "kind": "tool_output",
                                                "tool_name": names[call_id],
                                                "call_id": call_id,
                                                "path": artifact_path.relative_to(self.sandbox_working_dir).as_posix(),
                                                "bytes": artifact_path.stat().st_size,
                                            }
                                            outputs = self.sandbox_working_dir / "outputs"
                                            outputs.mkdir(parents=True, exist_ok=True)
                                            index_updated = update_artifact_index(workspace_dir=self.sandbox_working_dir, artifact_entry=artifact_entry)

                                    o["text"] = f"[Input context trimmed] Call process_big_document tool, document_ref is {artifact_path.relative_to(self.sandbox_working_dir).as_posix()}"
                                    
                                current_size = current_size - len(str(i))

            new_items.append(i)
        
        remaining = self.max_turns - self.current_turn
        
        input_items[:] = new_items
        if 0 < remaining <= self.warn_before:
            input_items.append({
                "type": "message",
                "role": "user",
                "content":[
                    {
                        "type": "input_text",
                        "text": (
                            f"[SYSTEM NOTICE] You have {remaining} turn(s) left before the "
                            f"conversation is forcibly stopped (max_turns={self.max_turns}). "
                            "Wrap up now: finish any pending tool calls, summarize progress, "
                            "and provide your final answer without starting new tool calls "
                            "unless strictly necessary."
                        )
                    }
                ]
            })
        
        instructions_text = system_prompt or ""
        input_text = json.dumps(input_items, default=str, ensure_ascii=False)
        tools_text = json.dumps(converted.tools, default=str, ensure_ascii=False)

        breakdown = ContextBreakdown(
            instructions_tokens=approx_tokens(instructions_text),
            input_items_tokens=approx_tokens(input_text),
            tools_tokens=approx_tokens(tools_text),
            total_tokens=approx_tokens(instructions_text + input_text + tools_text)
        )

        obj = {"kind": "TokenUsageDetail", 
               "message": json.dumps(asdict(breakdown)), 
               "UserId": self.user_id , 
               "Datetime": utc_now(), 
               "ConversationId": self.conversation_id,
               "Model": self.model}
        publish_message(obj)
