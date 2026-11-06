from typing import Dict, Any
from agents import ( 
	Agent, 
	Runner, 
	ItemHelpers,
	WebSearchTool,
	RunConfig,
	SessionSettings,
	OpenAIChatCompletionsModel,
	handoff,
	set_default_openai_client, 
	set_default_openai_key, 
	set_tracing_disabled )
from agents.mcp import MCPServerStreamableHttp
from agents.model_settings import ModelSettings
from agents.sandbox import Manifest, SandboxRunConfig
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from google.auth import default
from google.auth.transport.requests import Request
from dotenv import load_dotenv
from collections.abc import AsyncIterator
from openai import AsyncOpenAI
from pathlib import Path
from src.api.request import AttachToConversationRequest
from src.citation_links import extract_chunk_texts_from_tool_output
from src.tools.mcp_tools import (build_mcp_tools)
from .trimmers.persisting_tool_filter import (PersistingToolOutputFilter)
from .hooks.context_size_hook import (ContextSizeHooks)
from .hooks.multi_run_hooks import MultiRunHooks
from .utils import (utc_now)
from .jsonfilesession import JsonFileSession
from src.sandbox_utils import (
	ensure_sandbox_manifest, build_sandbox_agent
)
from pathlib import Path

import time
import json
import os
import re

load_dotenv()
ENV = os.getenv("ENV", "dev")

DEFAULT_SANDBOX_PREPROCESS_THRESHOLD_BYTES = int(os.getenv("SANDBOX_PREPROCESS_THRESHOLD_BYTES", "50000"))
DEFAULT_SESSION_LIMIT = int(os.getenv("SESSION_TURN_LIMIT", "4"))
DEFAULT_INLINE_TOOL_OUTPUT_MAX_CHARS = int(os.getenv("SANDBOX_INLINE_TOOL_OUTPUT_MAX_CHARS", "8000"))
DEFAULT_TOOL_OUTPUT_PREVIEW_CHARS = int(os.getenv("SANDBOX_TOOL_OUTPUT_PREVIEW_CHARS", "600"))
DEFAULT_RECENT_TURNS = int(os.getenv("SANDBOX_RECENT_TURNS", "1"))
DEFAULT_COMPACTION_THRESHOLD = int(os.getenv("MODEL_COMPACTION_THRESHOLD", "60000"))
REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX_ROOT_DIR = Path(os.getenv("STORAGE_PATH", str(REPO_ROOT / "dataroom"))).resolve()

def configure_openai_client(api_key: str, base_url: str | None = None) -> None:
	# set_tracing_disabled(True)
	
	if base_url:
		set_default_openai_client(AsyncOpenAI(api_key=api_key, base_url=base_url))
		return
	set_default_openai_key(api_key)

async def stream_gemini_sandbox(prompt: str, mcp_url: str, server_name: str, model: str, timeout: float, headers: Dict[str, str],
					   conversation_id: str,
					   system_prompt: str = None,
					   on_event = None,
					   effort: str = "medium",
					   model_config: dict[str,str] = None,
					   user_id: str = None,
					   retry: int = 0,
					   attachments: list[AttachToConversationRequest] = None,
					   chunk_text_by_doc: dict[str, dict[str, str]] | None = None) -> AsyncIterator[dict[str, str]]:
	if user_id is None:
		session = JsonFileSession(f"{conversation_id}")
	else:
		# session = OpenAIResponsesCompactionSession(session_id=f"{user_id}/{conversation_id}", underlying_session=JsonFileSession(f"{user_id}/{conversation_id}"))
		session = JsonFileSession(f"{user_id}/{conversation_id}", retry=retry)
	set_tracing_disabled(True)
	os.environ.pop("GOOGLE_API_KEY", None)
	os.environ.pop("GEMINI_API_KEY", None)
	realModel = f'google/{model_config["model"]}'
	os.environ["GEMINI_MODEL"] = f'{model_config["model"]}'
	os.environ["GOOGLE_CLOUD_PROJECT"] = model_config["project"]
	os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
	BASE_URL = (
		f"https://aiplatform.googleapis.com/v1/"
		f"projects/{model_config['project']}/locations/global/endpoints/openapi"
	)

	async with MCPServerStreamableHttp(
		name=server_name,
		params={
			"url": mcp_url,
			"headers": headers,
			"timeout": timeout,
		},
		client_session_timeout_seconds=30,
		cache_tools_list=True,
		max_retry_attempts=3,
		use_structured_content=False,

	) as mcp_server:
		credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
		auth_request = Request()
		credentials.refresh(auth_request)
		client = AsyncOpenAI(api_key=credentials.token, base_url=BASE_URL)
		gemini_model = OpenAIChatCompletionsModel(model=realModel, openai_client=client)
		manifest, workspace_dir = ensure_sandbox_manifest(conversation_id=conversation_id, user_id=user_id)
		base_instructions = system_prompt

		instructions = "\n\n".join(
			part
			for part in [
				f"Today is {utc_now(only_date=True)}",
				base_instructions,
			]
			if part
		)

		if effort in ("xhigh"):
			effort = "high"
		extra_body = {
						"google": {
							"thinking_config": {
								"thinking_level": effort.upper(),
								"include_thoughts": True
							}
						}
					}
		if "2.5" in model:
			extra_body = None

		researcher = build_sandbox_agent(conversation_id=conversation_id,
								   user_id=user_id,
								   prompt=prompt,
								   server_name=server_name,
								   model=gemini_model,
								   system_prompt=system_prompt,
								   workspace_dir=workspace_dir,
								   mcp_server=mcp_server,
								   reasoning=None,
								   manifest=manifest,
								   headers=headers,
								   extra_body=extra_body,
								   add_web_search_tool=False)
		
		hooks = MultiRunHooks(
			ContextSizeHooks(conversation_id=conversation_id, user_id=user_id, model=model),
			
		)
		cfg = build_run_config(manifest=manifest, workspace_dir=workspace_dir)
		response = Runner.run_streamed(researcher, 
								 prompt, 
								 session=session, 
								 run_config=cfg,
								 max_turns=50, 
								 hooks=hooks
								 ) 
		times = {}
		start = time.perf_counter()
		tool_names = {}
		tool_args = {}
		source = f"{ENV.upper()} - {conversation_id} - "
		step = 1
		etypes = {}

		last_ping = time.perf_counter()

		async for event in response.stream_events():
			current_ping_time = time.perf_counter()
			if current_ping_time - last_ping > 5:
				last_ping = current_ping_time
				yield {"kind": "ping"}

			if event.type == "run_item_stream_event":
				item_type = getattr(event.item, "type", "")
				if item_type not in etypes:
					etypes[item_type] = 0
				etypes[item_type] = etypes[item_type]+1
				if item_type == "tool_call_item":
					tool_name = getattr(getattr(event.item, "raw_item", None), "name", "unknown")
					args = getattr(getattr(event.item, "raw_item", None), "arguments", "unknown")
					msg = {"kind": "trace", "message": f"Tool called: {tool_name} {args}"}
					tool_names[event.item.call_id] = tool_name
					tool_args[event.item.call_id] = args
					times[event.item.call_id] = time.perf_counter()

					yield msg
				elif item_type == "tool_call_output_item":
					tool_output = getattr(event.item, "output", "")
					if chunk_text_by_doc is not None:
						chunk_result = extract_chunk_texts_from_tool_output(tool_args.get(event.item.call_id), tool_output)
						if chunk_result:
							result_doc_id, result_chunks = chunk_result
							chunk_text_by_doc.setdefault(result_doc_id, {}).update(result_chunks)
					duration = time.perf_counter() - times[event.item.call_id]
					msg = {"kind": "trace", "message": f"Tool output {tool_names[event.item.call_id]}: {tool_output}", "duration": duration }
					yield msg
				elif item_type == "message_output_item":
					text = ItemHelpers.text_message_output(event.item)
					if text:
						msg = {"kind": "text", "message": f"{text}"} 
						yield msg
			elif event.type == "raw_response_event":
				event_type = getattr(event.data, "type", None)
				
				if event_type == "response.completed":
					msg = {"kind": "trace", "message": event.data.response.usage.model_dump_json()}
					yield msg
				elif event_type == "response.reasoning_summary_text.delta":
					msg = {"kind": "reasoning", "message": f"{event.data.delta}"}
					
					yield msg
				elif event.data.type == "response.reasoning_summary_part.added":
					times[event.data.item_id] = time.perf_counter()
				elif event_type == "response.reasoning_summary_part.done":
					msg = {"kind": "reasoning", "message": f"{event.data.part.text}"} 
					if event.data.item_id in times:
						reasoning_time = time.perf_counter() - times[event.data.item_id]
						msg["duration"] = reasoning_time
					
					yield msg	
				elif event_type == "response.reasoning_text.delta":
					msg = {"kind": "reasoning", "message": f"{event.data.delta}"}
					yield msg
				elif event_type == "response.output_item.added":
					pass
				elif event_type == "response.output_text.delta":
					pass
		
		total_time = time.perf_counter() - start
		yield { "kind": "done", "message": response.final_output, "duration": total_time }
		usage = {
			"total_tokens": response.context_wrapper.usage.total_tokens,
			"input_tokens": response.context_wrapper.usage.input_tokens,
			"output_tokens": response.context_wrapper.usage.output_tokens,
			"requests": response.context_wrapper.usage.requests
		}
		
		yield {"kind": "usage", "message": json.dumps(usage)}
		session.complete()


async def stream_research_sandbox(prompt: str, mcp_url: str, server_name: str, model: str, timeout: float, headers: Dict[str, str],
					   conversation_id: str,
					   system_prompt: str = None,
					   on_event = None,
					   effort:str = "medium",
					   user_id: str = None,
					   attachments: list[AttachToConversationRequest] = None,
					   retry: int = 0,
					   chunk_text_by_doc: dict[str, dict[str, str]] | None = None) -> AsyncIterator[dict[str, str]]:
	if user_id is None:
		session = JsonFileSession(f"{conversation_id}")
	else:
		session = JsonFileSession(f"{user_id}/{conversation_id}", retry=retry)

	async with MCPServerStreamableHttp(
		name=server_name,
		params={
			"url": mcp_url,
			"headers": headers,
			"timeout": timeout,
		},
		client_session_timeout_seconds=30,
		cache_tools_list=True,
		max_retry_attempts=3,
		use_structured_content=False,

	) as mcp_server:
		manifest, workspace_dir = ensure_sandbox_manifest(conversation_id=conversation_id, user_id=user_id)
		
		reasoning={"effort": effort, "summary": "detailed"}
		if "gpt-4" in model:
			reasoning = None
		
		researcher = build_sandbox_agent(conversation_id=conversation_id,
								   user_id=user_id,
								   prompt=prompt,
								   server_name=server_name,
								   model=model,
								   system_prompt=system_prompt,
								   workspace_dir=workspace_dir,
								   mcp_server=mcp_server,
								   reasoning=reasoning,
								   manifest=manifest,
								   headers=headers)
		
		hooks = MultiRunHooks(
			ContextSizeHooks(conversation_id=conversation_id, user_id=user_id, model=model),
		)
		cfg = build_run_config(manifest=manifest, workspace_dir=workspace_dir)
		response = Runner.run_streamed(researcher, 
								 prompt, 
								 session=session, 
								 run_config=cfg,
								 max_turns=50, 
								 hooks=hooks
								 ) 
		times = {}
		start = time.perf_counter()
		tool_names = {}
		tool_args = {}
		source = f"{ENV.upper()} - {conversation_id} - "
		step = 1
		ping_Start = time.perf_counter()

		async for event in response.stream_events():
			ping_end = time.perf_counter()
			if ping_end - ping_Start > 5:
				ping_Start = ping_end
				# yield {"kind": "ping" }
			if event.type == "run_item_stream_event":
				item_type = getattr(event.item, "type", "")
				if item_type == "tool_call_item":
					times[event.item.call_id] = time.perf_counter()
					tool_name = getattr(getattr(event.item, "raw_item", None), "name", "unknown")
					args = getattr(getattr(event.item, "raw_item", None), "arguments", "unknown")
					msg = {"kind": "trace", "message": f"Tool call: {tool_name} {args}"}
					tool_names[event.item.call_id] = tool_name
					tool_args[event.item.call_id] = args
					yield msg
				elif item_type == "tool_call_output_item":
					tool_output = getattr(event.item, "output", "")
					if chunk_text_by_doc is not None:
						chunk_result = extract_chunk_texts_from_tool_output(tool_args.get(event.item.call_id), tool_output)
						if chunk_result:
							result_doc_id, result_chunks = chunk_result
							chunk_text_by_doc.setdefault(result_doc_id, {}).update(result_chunks)
					duration = time.perf_counter() - times[event.item.call_id]
					msg = {"kind": "trace", "message": f"Tool output {tool_names[event.item.call_id]}: {tool_output}", "duration": duration }
					yield msg
				elif item_type == "message_output_item":

					msg = {"kind": "text", "message": f"{ItemHelpers.text_message_output(event.item)}"}
					yield msg
			elif event.type == "raw_response_event":
				if hasattr(event.data, "type") and event.data.type == "response.completed":
					
					msg = {"kind": "trace", "message": event.data.response.usage.model_dump_json(), "type": "partial", "appKey": "Agent", "source": f"{source} Turn {step}"}
					yield msg
					step = step + 1
				if event.data.type == "response.reasoning_summary_part.added":
					times[event.data.item_id] = time.perf_counter()
				if event.data.type == "response.reasoning_summary_part.done":
					reasoning_time = time.perf_counter() - times[event.data.item_id]
					msg = {"kind": "reasoning", "message": f"{event.data.part.text}", "duration": reasoning_time } 
					yield msg
		
		total_time = time.perf_counter() - start
		yield { "kind": "done", "message": response.final_output, "duration": total_time }
		usage = {
			"total_tokens": response.context_wrapper.usage.total_tokens,
			"input_tokens": response.context_wrapper.usage.input_tokens,
			"output_tokens": response.context_wrapper.usage.output_tokens,
			"requests": response.context_wrapper.usage.requests,
		}
		
		yield {"kind": "usage", "message": json.dumps(usage), "Source": source}
		
		if hasattr(session, "underlying_session"):
			session.underlying_session.complete()
		else:
			session.complete()
		
def load_prompt(file_name: str) -> str:
	return (Path("src") / file_name).read_text(encoding="utf-8")

def build_citation_agent(*, model: str, mcp_server: object, reasoning: dict | None = None) -> Agent:
	instructions = load_prompt("citation_system_prompt.txt")
	return Agent(
		name="Citation",
		instructions=instructions,
		mcp_servers=[mcp_server],
		model=model,
		tools=[],
		handoffs=[],
		model_settings=ModelSettings(
			tool_choice="auto",
			truncation="disabled",
			store=False,
			reasoning=reasoning,
			verbosity="medium",
		),
	)

def build_run_config(*, manifest: Manifest, workspace_dir: Path) -> RunConfig:
	return RunConfig(
		session_settings=SessionSettings(limit=DEFAULT_SESSION_LIMIT),
		trace_include_sensitive_data=True,
		sandbox=SandboxRunConfig(client=UnixLocalSandboxClient(), manifest=manifest),
		call_model_input_filter=PersistingToolOutputFilter(
			workspace_dir=workspace_dir,
			recent_turns=DEFAULT_RECENT_TURNS,
			max_output_chars=DEFAULT_INLINE_TOOL_OUTPUT_MAX_CHARS,
			preview_chars=DEFAULT_TOOL_OUTPUT_PREVIEW_CHARS,
		),
	)
