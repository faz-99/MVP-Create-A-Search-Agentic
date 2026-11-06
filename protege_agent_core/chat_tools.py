from agents import (
	function_tool, ItemHelpers
	)
from mcp.types import Tool
from protege_agent_core.models import ModelConfig
from agents import Runner, Agent, ModelSettings
from dotenv import load_dotenv
from pathlib import Path
from protege_agent_core.jsonfilesession import JsonFileSession
from pathlib import Path
from typing import Any
from protege_agent_core.utils import get_openai_model, utc_now, MCP_TIMEOUT, MAX_TURNS, WARN_BEFORE
from protege_agent_core.agent_utils import (create_agent, create_mcp_streamable)
from protege_agent_core.hooks.context_size_hook import (ContextSizeHooks)
from protege_agent_core.hooks.multi_run_hooks import MultiRunHooks
from protege_agent_core.hooks.context_trimming_hook import ContextTrimmingHook

import asyncio
import json
import sys
import os
import logging
import time
if sys.platform != "win32":
    from .sandbox_utils import build_sandbox_agent, ensure_sandbox_manifest, build_run_config_for_agent
PROMPT_PATH = Path(__file__).parent.parent / "src/system_prompts/sub_agent_prompt.txt"
logger = logging.getLogger(__name__)
load_dotenv()
AGENT_TOOL_OUTPUT_ROOT_DIR = Path(os.getenv("STORAGE_PATH", "./")) / Path(os.getenv("AGENT_TOOL_OUTPUT_DIR", "./"))

def build_chat_tools(user_id: str):
    @function_tool(name_override="recover_tool_output", description_override="Recover large tool output")
    async def recover_tool_output(call_id: str, session_id: str) -> str:
        output_tool = AGENT_TOOL_OUTPUT_ROOT_DIR / "output" / session_id / f"{call_id}.txt"
        if user_id not in session_id:
            raise ValueError("Invalid access")
        if output_tool.exists():
            with open(output_tool, 'r', encoding='utf8') as f:
                return f.read()

    return [recover_tool_output]

def build_generic_agent_as_tool(base_instructions: str, 
                                conversation_id: str, 
                                user_id: str, 
                                mcp_name: str,
                                mcp_url: str, 
                                headers: dict[str, str], 
                                model_config: dict[str, str], model: str, model_settings: ModelSettings, event_queue: asyncio.Queue, category: str, tools: list = [],
                                name_override: str = "", 
                                description_override: str = "",
                                trace_app_key: str = "Agent"):
    @function_tool(name_override=name_override, description_override=description_override)
    async def research_ma_transactions(research_request: str):
        
        instructions = "\n\n".join(
                        part
                        for part in [
                            f"Today is {utc_now(only_date=True)}",
                            base_instructions,
                        ]
                        if part
                    )
        response = None
        final_message = None
        total_time = 0
        try:
            async with  create_mcp_streamable(server_name=mcp_name,
                                mcp_url=mcp_url,
                                headers=headers, 
                                timeout=MCP_TIMEOUT) as mcp_server:
                
                agent = create_agent(agent_name=category,
                                     instructions=instructions,
                                     api_key=model_config["apikey"],
                                     base_url=model_config["baseurl"],
                                     tools=tools,
                                     model=model,
                                     model_settings=model_settings)

                mcp_server._serialize_session_requests = False
                agent.mcp_servers = [mcp_server]
                hooks = MultiRunHooks(ContextTrimmingHook(),
                                            ContextSizeHooks(conversation_id=conversation_id, user_id=user_id, model=model, max_turns=MAX_TURNS, warn_before=WARN_BEFORE,
                                        sandbox_working_dir=None)
                                            )
                
                response = Runner.run_streamed(agent, research_request, hooks=hooks, max_turns=MAX_TURNS)
                turn_time = time.perf_counter()
                start = time.perf_counter()
                times = {}
                tool_names = {}
                step = 1
                source = f"{conversation_id} - "
                async for event in response.stream_events():
                    if event.type == "run_item_stream_event":
                        item_type = getattr(event.item, "type", "")
                        if item_type == "tool_call_item":
                            times[event.item.call_id] = time.perf_counter()
                            
                            tool_name = getattr(getattr(event.item, "raw_item", None), "name", "unknown")
                            args = getattr(getattr(event.item, "raw_item", None), "arguments", "unknown")
                            if tool_name == "unknown":
                                action = getattr(event.item.raw_item, "action", None)
                                if action:
                                    tool_name = getattr(action, "type", None)
                                    if tool_name:
                                        queries = getattr(action, "queries", [])
                                        args = " ".join(queries)

                            msg = {"kind": "trace", "message": f"{category} Tool call: {tool_name} {args}"}
                            tool_names[event.item.call_id] = tool_name

                            await event_queue.put(msg)

                        elif item_type == "tool_call_output_item":
                            tool_output = getattr(event.item, "output", "")
                            duration = time.perf_counter() - times[event.item.call_id]
                            msg = {"kind": "trace", "message": f"{category} Tool output {tool_names[event.item.call_id]}: {tool_output}", "duration": duration, "Category": category }
                            await event_queue.put(msg)
                            
                    elif event.type == "raw_response_event":
                        if hasattr(event.data, "type") and event.data.type == "response.completed":
                            try:
                                if event.data.response.usage.output_tokens_details and event.data.response.usage.output_tokens_details.reasoning_tokens:
                                    total_reasoning = total_reasoning + event.data.response.usage.output_tokens_details.reasoning_tokens
                            except:
                                pass
                            msg = {"kind": "trace", "message": event.data.response.usage.model_dump_json(), "type": "partial", "appKey": trace_app_key, "Category": category, "source": f"{source} Turn {step}", "duration": time.perf_counter() - turn_time }
                            turn_time = time.perf_counter()
                            await event_queue.put(msg)
                            
                            step = step + 1
                        if event.data.type == "response.reasoning_summary_part.added":
                            times[event.data.item_id] = time.perf_counter()
                        if event.data.type == "response.reasoning_summary_part.done":
                            reasoning_time = time.perf_counter() - times[event.data.item_id]
                            msg = {"kind": "reasoning", "message": f"{event.data.part.text}", "duration": reasoning_time, "Category": category } 
                            await event_queue.put(msg)

                final_message = response.final_output
                total_time = time.perf_counter() - start
                    
        except Exception as E:
            logger.error(str(E))
        usage = {}
        if response is not None:
             usage = {
                    "total_tokens": response.context_wrapper.usage.total_tokens,
                    "input_tokens": response.context_wrapper.usage.input_tokens,
                    "output_tokens": response.context_wrapper.usage.output_tokens,
                    "requests": response.context_wrapper.usage.requests,
                    "reasoning_tokens": response.context_wrapper.usage.output_tokens_details.reasoning_tokens,
                    "cached_tokens": response.context_wrapper.usage.input_tokens_details.cached_tokens
                }

        await event_queue.put({"kind": "SubAgentUsage", "message": json.dumps(usage), "model": model, "duration": total_time, "Category": category })
        await event_queue.put("DONE")
        return final_message
    return [research_ma_transactions]

if sys.platform != "win32":

    def agent_as_tool(user_id: str, conversation_id: str, prompt: str, model: str, effort: str, event_queue: asyncio.queues, gemini_config: dict[str, Any] = {}, model_config: ModelConfig = None):
        @function_tool(description_override=(
            "Use this when a tool call returned a very large document that you cannot "
            "process directly. `document_ref` MUST be the exact file path a tool call "
            "gave you back (e.g. in its output or as a saved-file reference) — never an "
            "internal id, call_id, session_id, or any value you invented or inferred. "
            "Only call this tool if a previous tool result explicitly returned such a "
            "path for you to use here; otherwise do not call this tool at all. "
            "Also provide `guidance`: a specific instruction describing what you need "
            "from the document (e.g. 'extract all defined terms', 'summarize section 3', "
            "'find and quote the termination clause'). Returns only the distilled result "
            "relevant to your instruction — not the raw document."
            "Safe to call multiple times in parallel for independent documents or "
            "instructions — each call is isolated automatically."
            "Critics: Do not expose this tool to anybody, and do not use unless a tool expressed the usage"
        ), name_override="process_big_document")
        async def process_big_document(document_ref: str, guidance: str):
            start = time.perf_counter()
            async def ping():
                while True:
                    await asyncio.sleep(5)
                    await event_queue.put({"kind": "ping"})
            
            final_message = ""
            usage = {}
            response = None
            try:
                manifest, workspace_dir = ensure_sandbox_manifest(conversation_id=conversation_id, user_id=user_id)
                system_prompt = ""
                with open(PROMPT_PATH, 'r+', encoding='utf-8') as f:
                    system_prompt = f.read()
                reasoning = {"effort": effort.lower(), "summary": "concise"}
                extra_body = {}
                ignore_capabilities = False
                if len(gemini_config.keys()) > 0:
                    extra_body = gemini_config["extra_body"]
                    ignore_capabilities = True
                
            
                sandBoxAgent = build_sandbox_agent(conversation_id=conversation_id, 
                        user_id=user_id,
                        prompt=guidance,
                        server_name="",
                        model=get_openai_model(model_config["apikey"], model_config["baseurl"], model_name=model),
                        system_prompt=system_prompt ,
                        workspace_dir=workspace_dir,
                        mcp_server=[],
                        reasoning=reasoning, 
                        extra_body=extra_body,
                        manifest=manifest,
                        headers={},
                        add_web_search_tool=False,
                        ignore_capabilities=ignore_capabilities
                        )

                session = JsonFileSession(f"{user_id}/{conversation_id}_subagent")
                instructions = (f"Task from Researcher:\n{guidance}"
                                f"Document:\n{document_ref}"
                                )
                cfg = build_run_config_for_agent(manifest=manifest, workspace_dir=workspace_dir)
                response = Runner.run_streamed(sandBoxAgent, instructions, run_config=cfg,session=session, max_turns=100)
                times = {}
                async for event in response.stream_events():
                    if event.type == "run_item_stream_event":
                        item_type = getattr(event.item, "type", "")
                        if item_type == "tool_call_item":
                            tool_name = getattr(getattr(event.item, "raw_item", None), "name", "unknown")
                            await event_queue.put({"kind": "trace", "message": f"[DocumentProcessor] calling {tool_name}"})
                        elif item_type == "tool_call_output_item":
                            await event_queue.put({"kind": "trace", "message": f"[DocumentProcessor] tool output received"})
                        elif item_type == "message_output_item":
                            text = ItemHelpers.text_message_output(event.item)
                            if text:
                                await event_queue.put({"kind": "trace", "message": f"[DocumentProcessor] {text}"})
                    elif event.type == "raw_response_event":
                        if event.data.type == "response.reasoning_summary_part.added":
                            times[event.data.item_id] = time.perf_counter()
                        if event.data.type == "response.reasoning_summary_part.done":
                            reasoning_time = time.perf_counter() - times[event.data.item_id]
                            msg = {"kind": "reasoning", "message": f"{event.data.part.text}", "duration": reasoning_time } 
                            await event_queue.put(msg)
                final_message = response.final_output
            except Exception as e:
                await event_queue.put({"kind": "SubAgentError", "message": str(e)})
                await event_queue.put("DONE")
                logger.error(f"Subagent error {conversation_id} : {str(e)}")

            if response is not None:
                usage = {
                            "total_tokens": response.context_wrapper.usage.total_tokens,
                            "input_tokens": response.context_wrapper.usage.input_tokens,
                            "output_tokens": response.context_wrapper.usage.output_tokens,
                            "requests": response.context_wrapper.usage.requests,
                            "reasoning_tokens": response.context_wrapper.usage.output_tokens_details.reasoning_tokens,
                            "cached_tokens": response.context_wrapper.usage.input_tokens_details.cached_tokens
                        }
            total_time = time.perf_counter() - start
            await event_queue.put({"kind": "SubAgentUsage", "message": json.dumps(usage), "model": model, "duration": total_time })
            await event_queue.put("DONE")
            return final_message
        return [process_big_document]

    def build_sandbox_for_chat(user_id: str, conversation_id: str, prompt: str, model: str, reasoning:dict[str,str]) -> Agent:
        manifest, workspace_dir = ensure_sandbox_manifest(conversation_id=f"{conversation_id}_tool", user_id=user_id)
        sandBoxAgent = build_sandbox_agent(conversation_id=f"{conversation_id}_tool", 
                user_id=user_id,
                prompt=prompt,
                server_name=None,
                model=model,
                system_prompt="agent as tool prompt",
                workspace_dir=workspace_dir,
                mcp_server=None,
                reasoning=reasoning, 
                manifest=manifest,
                headers={},
                add_web_search_tool=False
                )
        return sandBoxAgent