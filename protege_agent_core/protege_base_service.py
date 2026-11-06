import sys

if sys.platform != "win32":
	from src.sandbox import stream_research_sandbox, stream_gemini_sandbox

from abc import abstractmethod, ABC
from dotenv import load_dotenv
from pathlib import Path

from protege_agent_core.utils import ( get_model_config, utc_now, create_stop_request, generate_stop_key, enqueue_dismiss, generate_dismiss_key, list_pending_dismiss, remove_dismiss, env_flag, http_get, http_post, http_put, get_user_preferences, upload_conversation_to_vault, read_system_prompt)
from protege_agent_core.s3_service import S3Sync
from protege_agent_core.tracking_service import publish_message 
from protege_agent_core.api.request import ChatRequest, SearchRequest, RegenerateRequest, ForkRequest, ShareRequest, PDERequest
from protege_agent_core.jsonfilesession import JsonFileSession
from typing import List, Any

from protege_agent_core.base_agent import ProtegeBaseAgent
from protege_agent_core.models import AgentConfig, McpConfig, AgentRequest, ModelConfig, DoneMessage
import asyncio
import datetime
import logging
from async_lru import alru_cache
import json
import os
import uuid
load_dotenv()

PARENT = Path(os.getenv("STORAGE_PATH", "./"))

CONVERSATIONS_DIR = PARENT / "conversations"
SESSIONS_DIR = PARENT / "sessions"
CONVERSATION_ROOT = Path("conversations")
SESSION_ROOT = Path("sessions")
AGENT_TOOL_OUTPUT_DIR = os.getenv("AGENT_TOOL_OUTPUT_DIR", "tool_output")
BASE_URL = os.getenv("MCP_AUTH_URL")
AGENT_API_ENABLED = env_flag("AGENT_API_ENABLED", False)
DEFAULT_CHAT_MODEL = os.getenv("DEFAULT_OPENAI_MODEL", "open_ai_gpt_5_6_terra")
DEFAULT_EFFORT = os.getenv("DEFAULT_EFFORT", "Medium")
IGNORE_DEBUG_RESPONSE = env_flag("IGNORE_DEBUG_RESPONSE", False)
DEFAULT_FAST_MODEL = os.getenv("DEFAULT_FAST_MODEL", "open_ai_gpt_5_6_terra")
logger = logging.getLogger(__name__)
class ReadOnlyConversationError(Exception):
	pass

class ApiService(ABC):
	s3_sync = S3Sync()
	header_name = os.getenv("AUTH_HEADER", "lna")
	def __init__(self, agent: ProtegeBaseAgent):
		self.publisher = publish_message
		self.user_id = None
		self.agent = agent

	@abstractmethod
	async def pre_stream(self, existing_chat: Any) -> dict[str, dict]:
		"""process and return additional data"""
		return None

	@abstractmethod
	async def on_done(self, event_queue: asyncio.Queue, done_message: DoneMessage):
		pass


	def set_user_id(self, user_id: str):
		self.user_id = user_id
		self.cleaned_user_id = user_id.replace(":", "")

	async def run_stream(self, request: ChatRequest, queue: asyncio.Queue = None, retry: int = 0, messages_input: list = None, activity_input: list = None,
	                     ):
		activity = []
		messages = []
		if not request.Mode:
			request.Mode = "Chat"

		if request.ResponseMode == "Thinking":
			request.Provider = "openai"
			request.Model = DEFAULT_CHAT_MODEL
			request.Effort = DEFAULT_EFFORT
		else:
			request.Provider = "openai"
			request.Effort = "None"
			request.Model = DEFAULT_FAST_MODEL
		
		if request.IsIncognito:
			request.ConversationId = f"{request.ConversationId}_incognito"

		if messages_input is not None:
			messages = messages_input
			activity = activity_input
		async def ping():
			while True:
				await asyncio.sleep(5)
				msg = {"kind": "ping"}
				await queue.put(f"data: {json.dumps(msg)}\n\n")
		heartbeat_task = asyncio.create_task(ping())
		
		if retry == 0:
			data = self.create_message_emit({"kind": "ping"}, activity=activity, conversation_id=request.ConversationId, user_id="user", model="model")
			await queue.put(data)
			messages.append(self.create_message(role="user", content=request.Prompt, req=request))
		
		user_id = self.user_id
		clean_user_id = user_id.replace(":", "")
		
		copy_task: asyncio.Task | None = None
		save_task: asyncio.Task | None = None
		existing_conversation_path = PARENT / CONVERSATION_ROOT / f"{clean_user_id}/{request.ConversationId}.json"
		existing_chat = await asyncio.to_thread(self.s3_sync.read_json, existing_conversation_path, {}, PARENT / CONVERSATION_ROOT)
		title = self.get_conversation_title(existing_chat)
		if retry == 0:
			try:
				await asyncio.to_thread(self._ensure_writable, chat=existing_chat)
			except ReadOnlyConversationError as e:
				error_obj = {"kind": "error", "message": str(e)}
				await queue.put(f"data: {json.dumps(error_obj)}\n\n")
				await queue.put("|*DONE*|")
				return
			copy_task = asyncio.create_task(asyncio.to_thread(self.copy_conversation, user_id=clean_user_id, conversation_id=request.ConversationId, chat=existing_chat))
			tmp_messages = messages.copy()
			tmp_messages.append(self.create_message(role="assistant", content="Generating a response...", req=request ))
			save_task = asyncio.create_task(self.save_conversation(user_id=clean_user_id, conversation_id=request.ConversationId, req=request, message=tmp_messages, title=None, start_porcessing=True))
		dismiss_key = generate_dismiss_key(user_id=clean_user_id, id=request.ConversationId)
		enqueue_dismiss(dismiss_key)
		original_provider = request.Provider
		if original_provider == "sandbox":
			request.Provider = "openai"
		cfg = get_model_config(request.Provider, request.Model)
		request.Provider = original_provider
		system_prompt = request.SystemPrompt
		mcp_url = None
		if self.agent.config.McpConfiguration:
			mcp_url = self.agent.config.McpConfiguration.Url

		if retry == 0:
			self.publisher({"kind": "UserInput", "message": request.Prompt, "DateTime": utc_now(), "ConversationId": request.ConversationId, "UserId": user_id, "Model": cfg["model"]})
		lastMessage = None
		duration = 0
		latest_message = None
		self.additional_data = await self.pre_stream(existing_chat=existing_chat)

		try:
			if system_prompt is None or system_prompt.strip() == "":
				system_prompt = await asyncio.to_thread(read_system_prompt)
			chat_title = await asyncio.to_thread(self.get_conversation_title, chat=existing_chat)
			agent_request = AgentRequest(
				Prompt=request.Prompt,
				UserId=user_id,
				ConversationId=request.ConversationId,
				ConversationTitle=title,
				Attachments=request.Attachments,
				Effort=request.Effort,
				Incognito=request.IsIncognito,
				SourceMode=request.SourceMode,
				AdditionalData=self.additional_data,
				SystemPrompt=system_prompt,
				ReasoningSummary=self.agent.config.ReasoningSummary,
				ModelConfiguration=ModelConfig(ApiKey=self.agent.config.ModelApiKey, 
				                               BaseUrl=self.agent.config.ModelBaseUrl),
                                                 )
                    			
			if request.Provider == "openai":
				if request.Mode == "Chat":
					async for item in self.agent.stream_research(agent_request, retry=retry):
						
						latest_message = item
						item_kind = item["kind"]
						if item_kind == "done":
							lastMessage= item["message"]
							duration = item["duration"]
						if item_kind == "title":
							chat_title = item["message"]

						data = self.create_message_emit(item=item, activity=activity, conversation_id=request.ConversationId, user_id=user_id, model=cfg["model"], user_prompt=request.Prompt, duration=duration)
						if not IGNORE_DEBUG_RESPONSE or (IGNORE_DEBUG_RESPONSE and item_kind not in ("trace")):
							await queue.put(data)
						if item_kind == "done":
							try:
								await self.on_done(queue, done_message=DoneMessage(LastMessage=lastMessage, Activity=activity, ConversationId=request.ConversationId, UserId=user_id, Model=cfg["model"], duration=duration))
							except Exception as e:
								logger.error(str(e))
				else:
					if not sys.platform == "win32":
						async for item in stream_research_sandbox(prompt=request.Prompt, 
											mcp_url=mcp_url,
											server_name="Intelligize MCP", 
											model=cfg["model"], 
											timeout=30, 
											headers={self.header_name: request.LexisToken},
											conversation_id=request.ConversationId,
											effort=str(request.Effort).lower(),
											system_prompt=system_prompt,
											user_id=clean_user_id,
											retry=retry,
											chunk_text_by_doc=chunk_text_by_doc):
							latest_message = item
							item_kind = item["kind"]
							if item_kind == "done":
								lastMessage= item["message"]
								duration = item["duration"]

							data = self.create_message_emit(item=item, activity=activity, conversation_id=request.ConversationId, user_id=user_id, model=cfg["model"], user_prompt=request.Prompt, duration=duration)
							await queue.put(data)
							if item_kind == "done":
								await self._emit_citations(lastMessage, chunk_text_by_doc, vault_metadata_by_doc, auth_token, activity, request.ConversationId, user_id, cfg["model"], request.Prompt, duration, queue)

			messages.append(self.create_message(role="assistant", content=lastMessage, activity=activity, duration=duration))
		except Exception as e:
			error_text = str(e)
			original_prompt = request.Prompt
			error_lower = error_text.lower()
			
			if "input exceed" in error_lower and retry == 0:
				request.Prompt = f"""Please ask the user to refine the request. This was the error {error_text}.
				Important: Treat the error hint as internal guidance only. Do not mention, describe, quote, or allude to the error, its cause, system limitations, context limits, token limits, or internal processing."""
				await self.run_stream(request=request, queue=queue, retry=retry + 1, messages_input=messages, activity_input=activity)
				session = JsonFileSession(f"{clean_user_id}/{request.ConversationId}")
				session.remove_last_user_message()
				await session.complete()
				request.Prompt = original_prompt
			elif "our servers are currently overloaded"  in error_lower:
				await asyncio.sleep(5)
				await self.run_stream(request=request, queue=queue, retry=retry + 1, messages_input=messages_input, activity_input=activity_input)
			else:
				if latest_message is not None and latest_message["kind"] == "text":
					latest_message["kind"] = "done"
					data = f"data: {json.dumps(latest_message)}\n\n"
					await queue.put(data)
				else:
					error_obj = {"kind": "error", "message": error_text}
					data =  f"data: {json.dumps(error_obj)}\n\n"
					await queue.put(data)
				messages.append(self.create_message(role="assistant", content=lastMessage, activity=activity, duration=duration))
		finally:
			heartbeat_task.cancel()
			await asyncio.gather(heartbeat_task, return_exceptions=True)
			if copy_task is not None:
				try:
					await copy_task
				except Exception:
					logging.getLogger(__name__).exception("copy_conversation failed " + request.ConversationId)

			if save_task is not None:
				try:
					await save_task
				except Exception:
					logging.getLogger(__name__).exception("temp_save failed " + request.ConversationId)

			if retry == 0:
				await self.save_conversation(user_id=clean_user_id, conversation_id=request.ConversationId, message=messages, req=request, title=chat_title)
				await queue.put({"dismiss_key": dismiss_key})
				await queue.put("|*DONE*|")

	
	def get_citations_chat(self, existing_chat: dict | None) -> dict[str, dict]:
		citations: dict[str, dict] = {}

		if not isinstance(existing_chat, dict):
			return citations

		for message in existing_chat.get("messages") or []:
			if not isinstance(message, dict) or message.get("role") != "assistant":
				continue

			for activity in message.get("activity") or []:
				if not isinstance(activity, dict) or activity.get("kind") != "citations":
					continue

				data = activity.get("data")
				if not isinstance(data, dict):
					continue

				for citation_key, citation_data in data.items():
					if not isinstance(citation_data, dict):
						continue

					existing = citations.setdefault(citation_key, {})
					existing.update({
						field: value
						for field, value in citation_data.items()
						if value is not None
					})

		return citations

	def contains_vault_attachment(self, request: ChatRequest, message: List[dict[str,str]]) -> bool:
		if request and request.Attachments and len(request.Attachments) > 0:
			for a in request.Attachments:
				if a.Type == "vault":
					return True

		if message and len(message) > 0:
			for m in message:
				role = m.get("role")
				if role == "assistant":
					activity = m.get("activity", [])
					for a in activity:
						kind = a.get("kind", "")
						msg = a.get("message", "")
						if kind == "trace" and "vault_" in msg:
							return True
		return False 


	def create_message_emit(self, item: dict[str,str], activity: list, conversation_id: str, user_id: str, model:str, user_prompt: str = None, duration: int = 0):

		item_kind = item["kind"]
		valid_kind = item_kind not in ("ping", "response_delta", "reasoning_delta", "reasoning_start", "reasoning_end", "response_start", "response_end")
		if valid_kind:
			activity.append(item)
		trace = item.copy()
		trace["ConversationId"] = conversation_id
		trace["UserId"] = user_id
		trace["DateTime"] = utc_now()
		if item_kind == "usage" or (item_kind == "trace" and item.get("type", "") == "partial"):
			trace["Model"] = model

		if item_kind == "SubAgentUsage" and "duration" in trace:
			trace["TotalTime"] = str(datetime.timedelta(seconds=trace["duration"]))
		elif "duration" in trace:
			trace["TotalTime"] = str(datetime.timedelta(seconds=trace["duration"]))
		elif duration > 0:
			trace["TotalTime"] = str(datetime.timedelta(seconds=duration))

		if valid_kind:
			source = os.getenv("ENV")
			if "source" in trace:
				trace["Source"] = trace["source"]
				del trace["source"]
			else:
				trace["Source"] = f"{source} - {conversation_id}"
			trace["AppKey"] = self.agent.config.TraceAppKey
			trace["UserPrompt"] = user_prompt
			self.publisher(trace)
		if item_kind == "title":
			del item["path"]
		return f"data: {json.dumps(item)}\n\n"

	def _ensure_writable(self, chat: dict | None = None, *, user_id: str | None = None, conversation_id: str | None = None) -> None:
		if chat is None and user_id and conversation_id:
			path = PARENT / CONVERSATION_ROOT / f"{user_id}/{conversation_id}.json"
			chat = self.s3_sync.read_json(path, None, PARENT / CONVERSATION_ROOT)
		if chat and chat.get("readOnly"):
			raise ReadOnlyConversationError("This conversation is a read-only shared snapshot and cannot be modified.")

	async def get_conversation_by_id(self, lna: str, id: str, user_id: str = None, auth_cookie: str = None):
		if AGENT_API_ENABLED:
			url = f"{BASE_URL}/api/agent/conversation?lna={lna}&id={id}"
			headers = {}
			if auth_cookie:
				headers["Authorization"] = f"Bearer {auth_cookie}"
			response = await http_get(url=url, headers=headers)
			result = response.json()
			if result:
				result_path = result.get("Path")
				if result_path:
					conversation_root = (PARENT / CONVERSATION_ROOT).resolve()
					path = (PARENT / CONVERSATION_ROOT / Path(result_path)).resolve()
					if not path.is_relative_to(conversation_root):
						raise ValueError("Resolved conversation path escapes the conversation root")
					tmp_path = path.with_suffix(".tmp")
					if tmp_path.exists():
						path = tmp_path
					obj = self.s3_sync.read_json(path, {}, PARENT / CONVERSATION_ROOT)
					readonly = result["Type"] != 0 or tmp_path.exists()
					obj["readOnly"] = readonly
					if "Initials" in result:
						obj["initials"] = result["Initials"]
					obj["inProgress"] = tmp_path.exists()
					if "mode" not in obj:
						obj["mode"] = "Chat"
					if "responseMode" not in obj:
						obj["responseMode"] = "Thinking"
					return obj
				else:
					return {}
				
		if (lna or "") != "":
			if user_id is None:
				user_id = await self.get_user_id(lna=lna)
			path = PARENT / CONVERSATION_ROOT / f"{user_id}/{id}.json"
		else:
			path = self.s3_sync.find_file(PARENT / CONVERSATION_ROOT, id=id)
		tmp_path= path.with_suffix(".tmp")
		if tmp_path.exists():
			path = tmp_path
		obj = self.s3_sync.read_json(path, {}, PARENT / CONVERSATION_ROOT)
		if "mode" not in obj:
			obj["mode"] = "Chat"
		obj["readOnly"] = tmp_path.exists()
		obj["inProgress"] = tmp_path.exists()
		return obj
	
	async def _update_chat_title(self, id: str, lna:str, title: str, igz_token: str = None):
		user_id = ""
		clean_user_id = ""
		if (lna or "") != "":
			user_id = await self.get_user_id(lna=lna, remove_colon=False)
			clean_user_id = user_id.replace(":", "")
		await self.update_title_conversation(user_id=clean_user_id, conversation_id=id, title=title, complete_user_id=user_id, igz_token=igz_token)

	def create_message(self, role: str, content: str, activity: list[dict[str, str]] | None = None, duration: int = 0, req: ChatRequest = None) -> dict:
		message = {
			"id": uuid.uuid4().hex,
			"role": role,
			"content": content,
			"timestamp": utc_now(),
		}

		attachments = []
		if req is not None and req.Attachments is not None and len(req.Attachments) > 0:
			for a in req.Attachments:
				attachments.append(a.model_dump())
			message["attachments"] = attachments

		if activity:
			message["activity"] = activity
		if duration > 0:
			message["duration"] = duration

		return message
		
	
	async def update_title_conversation(self, user_id: str, conversation_id: str, title: str, complete_user_id: str, igz_token: str = None) -> None:
		if user_id == "":
			path = self.s3_sync.find_file(CONVERSATION_ROOT, id=conversation_id)
		else:
			path = PARENT / CONVERSATION_ROOT / f"{user_id}/{conversation_id}.json"
		messages = self.s3_sync.read_json(path, None, PARENT / CONVERSATION_ROOT)
		self._ensure_writable(messages)
		messages["title"] = title
		self.s3_sync.write_json(path, messages, PARENT / CONVERSATION_ROOT)
		if AGENT_API_ENABLED:
			url = f"{BASE_URL}/api/agent/conversationtitle"
			headers = {"content-type": "application/json"}
			if igz_token:
				headers["Authorization"] = igz_token
			await http_put(url=url, headers={"content-type": "application/json"}, body={"UserId": complete_user_id, "ConversationId": conversation_id, "Title": title})

	def copy_conversation(self, user_id:str, conversation_id:str, chat: dict | None) -> bool:
		if chat is None or not chat.get("messages"):
			return False
		turn = 1 + sum(1 for m in chat["messages"] if m["role"] == "user")
		path = PARENT / CONVERSATION_ROOT / f"{user_id}/{conversation_id}_{turn}.json"
		self.s3_sync.write_json(path, chat, PARENT, ignore_upload=True)
		return True


	def get_conversation_title(self,  chat: dict = None) -> str:
		return chat.get("title", "")
		

	async def save_conversation(self, user_id:str, conversation_id:str, message: List[dict[str,str]], req: ChatRequest, title: str = None, start_porcessing: bool = False) -> None:
		path = PARENT / CONVERSATION_ROOT / f"{user_id}/{conversation_id}.json"
		tmp_path = PARENT / CONVERSATION_ROOT / f"{user_id}/{conversation_id}.tmp"
		ignore_upload = False
		if start_porcessing:
			self.s3_sync.copy_file(path, tmp_path)
			path = tmp_path
			ignore_upload = True
		else:
			if tmp_path.exists():
				tmp_path.unlink()
		chat = self.s3_sync.read_json(path, None, PARENT / CONVERSATION_ROOT)
		contains_vault_attachment = self.contains_vault_attachment(request=req, message=message)
		if chat is None:
			chat = {
				"id": conversation_id,
				"title": title or req.Prompt,
				"created_at": utc_now(),
				"updated_at": utc_now(),
				"provider": req.Provider,
				"model": req.Model,
				"messages": message,
				"mode": req.Mode,
				"responseMode": req.ResponseMode
			}
		else:
			chat["updated_at"] = utc_now()
			chat["responseMode"] = req.ResponseMode
			if not chat.get("title"):
				chat["title"] = title or req.Prompt
				if not contains_vault_attachment:
					contains_vault_attachment = self.contains_vault_attachment(request=None, message=chat["messages"])
			for m in message:
				chat["messages"].append(m)
		if contains_vault_attachment:
			ignore_upload = True

		self.s3_sync.write_json(path, chat, PARENT, ignore_upload=ignore_upload)
		if not start_porcessing and contains_vault_attachment:
			content = json.dumps(chat, ensure_ascii=True)
			await upload_conversation_to_vault(request=req, content=content)
	
	
	@staticmethod
	@alru_cache(maxsize=1000, ttl=600)	
	async def get_user_id(lna: str, remove_colon: bool=True, igz_token: str = None) -> str:
		url = f"{BASE_URL}/api/mcpauth/userid?lna={lna}"
		headers={"content-type": "application/json"}
		if igz_token:
			headers["Authorization"] = f"Bearer {igz_token}"
		response = await http_get(url=url, headers=headers)
		userId =response.json() 
		if remove_colon:
			return userId.replace(":","")
		return userId


	async def get_customer_id(self, lna: str) -> str:
		url = f"{BASE_URL}/api/mcpauth/customerid?lna={lna}"
		response = await http_get(url=url, headers={"content-type": "application/json"})
		userId = response.json() 
		return userId

	async def still_processing(self, lna:str, id: str, queue: asyncio.Queue) -> None:
		while True:

			user_id = await self.get_user_id(lna=lna)
			path = PARENT / CONVERSATION_ROOT / f"{user_id}/{id}.tmp"
			exists_pathlib = await asyncio.to_thread(path.exists)
			if not exists_pathlib:
				msg = {"kind": "done"}
				await queue.put(f"data: {json.dumps(msg)}\n\n")
				await queue.put("|*DONE*|")
				break
			else:
				msg = {"kind": "ping"}
				await queue.put(f"data: {json.dumps(msg)}\n\n")
				
			await asyncio.sleep(2)

	async def get_conversations(self, lna:str, igz_token: str = None):
		if (lna or "") != "":
			user_id = await self.get_user_id(lna=lna, igz_token=igz_token)
			root = PARENT / CONVERSATION_ROOT / user_id
		else:
			root = PARENT / CONVERSATION_ROOT
		if AGENT_API_ENABLED:
			return await self.get_conversations_from_api(lna=lna, igz_token=igz_token)
		
		paths = self.s3_sync._iter_local_files(root)
		result = []
		for p in paths:
			if not "_" in p.name and not p.name.endswith(".tmp"):
				d = self.s3_sync.read_json(p, {}, root_dir=root)
				title = ""
				if "title" in d:
					title = d["title"]
				
				result.append({"Title": title, "Id": d["id"], "Created": d["created_at"], "Updated": d["updated_at"]})
		
		return sorted(result, key=lambda x: x["Created"])

	async def get_conversations_from_api(self, lna:str, keywords:str = None, includeArchived: bool = False, igz_token = None):
		url = f"{BASE_URL}/api/agent/conversations"
		user_preferences = await get_user_preferences(igz_token=igz_token)
		retention = user_preferences.get("retention", {})
		retention_period = retention.get("effectiveRetentionDays", -1)
		body = {
			"Lna": lna,
			"Keywords": keywords,
			"IncludeArchived": includeArchived,
			"RetentionPeriod": retention_period
		}
		headers = { "Authorization": f"Bearer {igz_token}"}
		response = await http_post(url=url, headers=headers, body=body)
		responseJson = response.json()
		result = []
		for r in responseJson:
			result.append({"Title": r["Title"], "Id": r["ConversationId"], "Created": r["DateCreated"], "Updated": r["DateUpdated"]})
		return sorted(result, key= lambda x: x["Created"])

	async def update_conversation_api(self, lna: str, id: str, status: int, igz_token: str = None):
		url = f"{BASE_URL}/api/agent/conversationstatus"
		headers = {
		}
		if igz_token:
			headers["Authorization"] = f"Bearer {igz_token}"
		await http_put(url=url, headers=headers, body={"ConversationId": id, "Lna": lna, "Status": status})
	
	async def delete_chat(self, lna:str, id: str, igz_token: str = None):
		sessions_path = []
		
		if (lna or "") != "":
			if AGENT_API_ENABLED:
				self._ensure_writable(await self.get_conversation_by_id(lna=lna, id=id, auth_cookie=igz_token))
				await self.update_conversation_api(lna=lna, id=id, status=4, igz_token=igz_token)
				return
			user_id = await self.get_user_id(lna=lna)
			self._ensure_writable(user_id=user_id, conversation_id=id)
			path = PARENT / CONVERSATION_ROOT / f"{user_id}/{id}.json"
			dir = PARENT / SESSION_ROOT / user_id
			sessions_path = dir.glob(f"{id}*")
			tool_output_dir = PARENT / AGENT_TOOL_OUTPUT_DIR / "output" / user_id / id
			if tool_output_dir.exists():
				files = os.listdir(tool_output_dir)
				for f in files:
					full_path = tool_output_dir / f
					if full_path.exists():
						full_path.unlink()
		else:
			return
		
		self.s3_sync.delete_file(file_path=path, s3_key=path.relative_to(PARENT))
		for f in sessions_path:
			self.s3_sync.delete_file(file_path=f, s3_key=f.relative_to(PARENT))

	async def search_in_conversations(self, lna: str, request: SearchRequest | None = None):
		if request is None or (request.Value or "").strip() == "":
			return await self.get_conversations(lna=lna)
		if AGENT_API_ENABLED:
			return await self.get_conversations_from_api(lna=lna, keywords=request.Value, igz_token=request.IgzToken)
		root = await self.sync_conversations(lna=lna)
		paths = self.s3_sync._iter_local_files(root)
		result = []
		
		for p in paths:
			d = self.s3_sync.read_json(p, {}, root_dir=root)
			title = d["title"]
			for item in d["messages"]:
				if "content" in item:
					content = (item["content"] or "").lower()
					if request.Value.lower() in content:
						result.append({"Title": title, "Id": d["id"], "Created": d["created_at"], "Updated": d["updated_at"]})
						break
			
		return sorted(result, key=lambda x: x["Created"])	

	async def sync_conversations(self, lna: str):
		root = ""
		if (lna or "") != "":
			user_id = await self.get_user_id(lna=lna)
			root = CONVERSATION_ROOT / user_id
			session_root = SESSION_ROOT / user_id
		else:
			root = CONVERSATION_ROOT
			session_root = SESSION_ROOT

		self.s3_sync.sync_data_dir_from_s3(PARENT, s3_dir=root)
		self.s3_sync.sync_data_dir_from_s3(PARENT, s3_dir=session_root)

		return PARENT / root

	def get_default_system_prompt(self, mode: str) -> str:
		path = "src/system_prompts/system_prompt.txt"
		if mode == "Sandbox":
			path = "src/system_prompts/system_prompt_sandbox.txt"
		system_prompt = ""
		with open(Path(path), 'r', encoding='utf8') as f:
			system_prompt = f.read()

		return system_prompt

	async def create_stop_request(self, lna: str, id: str):
		user_id = await self.get_user_id(lna=lna)
		key = generate_stop_key(user_id=user_id, id=id)
		create_stop_request(key=key)

	async def list_pending_dismiss(self, lna: str):
		user_id = await self.get_user_id(lna=lna)
		return list_pending_dismiss(user_id=user_id)

	async def dismiss(self, lna:str, id: str):
		clean_user_id = await self.get_user_id(lna=lna)
		remove_dismiss(generate_dismiss_key(user_id=clean_user_id, id=id))

	def internal_dismiss(self, key: Path):
		remove_dismiss(key=key)

	async def regenerate(self, lna: str, regenerate_request: RegenerateRequest):
		user_id = await self.get_user_id(lna=lna)
		clean_user_id=  user_id.replace(":", "")
		session = JsonFileSession(f"{clean_user_id}/{regenerate_request.ConversationId}", ignore_copy=True)
		chat = None
		conversation = PARENT / CONVERSATION_ROOT / f"{clean_user_id}/{regenerate_request.ConversationId}.json"
		conversation_turn = PARENT / CONVERSATION_ROOT / f"{clean_user_id}/{regenerate_request.ConversationId}_{regenerate_request.Turn}.json"

		self._ensure_writable(user_id=clean_user_id, conversation_id=regenerate_request.ConversationId)

		if regenerate_request.Turn == 1:
			chat = await self.get_conversation_by_id(lna=lna, id=regenerate_request.ConversationId, user_id=user_id)
			self.s3_sync.delete_file(file_path=conversation, s3_key=conversation.relative_to(PARENT))

		if conversation_turn.exists():
			conversation_content = self.s3_sync.read_json(conversation_turn, [], PARENT / CONVERSATION_ROOT)
			self.s3_sync.write_json(conversation, conversation_content, PARENT)

		session.recreate_from(regenerate_request.Turn)
		
		if chat is None:
			chat = await self.get_conversation_by_id(lna=lna, id=regenerate_request.ConversationId, user_id=user_id)
		next_turn = regenerate_request.Turn + 1

		while True:
			conversation_turn = PARENT / CONVERSATION_ROOT / f"{clean_user_id}/{regenerate_request.ConversationId}_{next_turn}.json"
			if not conversation_turn.exists():
				break
			self.s3_sync.delete_file(file_path=conversation_turn, s3_key=conversation_turn.relative_to(PARENT))
			next_turn = next_turn + 1

		return chat

	async def fork(self, lna: str, fork_request: ForkRequest):
		user_id = await self.get_user_id(lna=lna)
		clean_user_id = user_id.replace(":", "")

		new_conversation_id = fork_request.ConversationId
		new_conversation_path = PARENT / CONVERSATION_ROOT / f"{clean_user_id}/{new_conversation_id}.json"
		new_session_path = PARENT / SESSION_ROOT / f"{clean_user_id}/{new_conversation_id}.json"

		source_turn = fork_request.Turn + 1
		source_conversation_turn = PARENT / CONVERSATION_ROOT / f"{clean_user_id}/{fork_request.Source}_{source_turn}.json"
		source_conversation = PARENT / CONVERSATION_ROOT / f"{clean_user_id}/{fork_request.Source}.json"
		source_chat = None

		if source_conversation_turn.exists():
			source_chat = self.s3_sync.read_json(source_conversation_turn, {}, PARENT / CONVERSATION_ROOT)
		elif source_conversation.exists():
			source_chat = await self.get_conversation_by_id(lna=lna, id=fork_request.Source, user_id=user_id)
		else:
			raise ValueError("Source conversation turn was not found")

		self._ensure_writable(source_chat)

		self.s3_sync.sync_data_dir_from_s3(PARENT, s3_dir=SESSION_ROOT / clean_user_id)
		source_session_turn = PARENT / SESSION_ROOT / f"{clean_user_id}/{fork_request.Source}_{source_turn}.json"
		source_session_items = None

		if source_session_turn.exists():
			source_session_items = self.s3_sync.read_json(source_session_turn, [], PARENT / SESSION_ROOT)
		else:
			source_session = PARENT / SESSION_ROOT / f"{clean_user_id}/{fork_request.Source}.json"

			if source_session.exists():
				source_session_items = self.s3_sync.read_json(source_session, [], PARENT / SESSION_ROOT)
			else:
				raise ValueError("Source session turn was not found")

		new_chat = {
			"id": new_conversation_id,
			"title": "",
			"created_at": utc_now(),
			"updated_at": utc_now(),
			"provider": fork_request.Provider if fork_request.Provider else source_chat["provider"],
			"model": fork_request.Model if fork_request.Model else source_chat["model"],
			"messages": source_chat["messages"],
			"mode": fork_request.Mode if fork_request.Mode else source_chat["mode"],
			"forked_from": fork_request.Source
		}

		self.s3_sync.write_json(new_conversation_path, new_chat, PARENT)
		self.s3_sync.write_json(new_session_path, source_session_items, PARENT)

		for turn_num in range(2, source_turn + 1):
			source_conversation_turn_file = PARENT / CONVERSATION_ROOT / f"{clean_user_id}/{fork_request.Source}_{turn_num}.json"
			if source_conversation_turn_file.exists():
				turn_content = self.s3_sync.read_json(source_conversation_turn_file, {}, PARENT / CONVERSATION_ROOT)
				turn_content["id"] = new_conversation_id
				turn_content["forked_from"] = fork_request.Source
				new_turn_file = PARENT / CONVERSATION_ROOT / f"{clean_user_id}/{new_conversation_id}_{turn_num}.json"
				self.s3_sync.write_json(new_turn_file, turn_content, PARENT)

		for turn_num in range(2, source_turn + 1):
			source_session_turn_file = PARENT / SESSION_ROOT / f"{clean_user_id}/{fork_request.Source}_{turn_num}.json"
			if source_session_turn_file.exists():
				session_content = self.s3_sync.read_json(source_session_turn_file, [], PARENT / SESSION_ROOT)
				new_session_turn_file = PARENT / SESSION_ROOT / f"{clean_user_id}/{new_conversation_id}_{turn_num}.json"
				self.s3_sync.write_json(new_session_turn_file, session_content, PARENT)

		fork_request.ConversationId = new_conversation_id

		return new_chat

	async def share(self, lna: str, share_request: ShareRequest):
		owner_user_id = await self.get_user_id(lna=lna, remove_colon=False)
		clean_user_id = owner_user_id.replace(":", "")

		source_path = PARENT / CONVERSATION_ROOT / f"{clean_user_id}/{share_request.ConversationId}.json"
		source_chat = self.s3_sync.read_json(source_path, None, PARENT / CONVERSATION_ROOT)

		if source_chat is None:
			source_chat = await self.get_conversation_by_id(lna=lna, id=share_request.ConversationId, user_id=clean_user_id, auth_cookie=share_request.IgzToken)

		self._ensure_writable(source_chat)

		if not source_chat:
			raise ValueError(f"Conversation {share_request.ConversationId} was not found")

		shared_chat = dict(source_chat)
		shared_chat["id"] = share_request.ShareId
		shared_chat["readOnly"] = True
		shared_chat["updated_at"] = utc_now()

		owner_share_path = f"{clean_user_id}/{share_request.ShareId}.json"
		shared_path = PARENT / CONVERSATION_ROOT / owner_share_path
		self.s3_sync.write_json(shared_path, shared_chat, PARENT)
		share_users = []
		for share_user in share_request.Users or []:
			target_user_id = (share_user.UserId or "").replace(":", "")
			if not target_user_id:
				continue
			target_path = f"{target_user_id}/{share_request.ShareId}.json"
			target_conversation_path = PARENT / CONVERSATION_ROOT / target_path
			self.s3_sync.write_json(target_conversation_path, shared_chat, PARENT)
			share_users.append({
				"UserId": share_user.UserId,
				"Path": target_path
			})

		if AGENT_API_ENABLED:
			url = f"{BASE_URL}/api/agent/share"
			customer_id = await self.get_customer_id(lna=lna)
			share_api_body = {
				"ConversationId": share_request.ShareId,
				"CustomerId": customer_id,
				"Type": share_request.Type,
				"Path": owner_share_path,
				"OwnerUserId": owner_user_id,
				"Title": shared_chat.get("title"),
				"Lna": lna,
				"OriginalConversationId": share_request.ConversationId,
			}

			if share_users:
				share_api_body["Users"] = share_users

			await http_post(url=url, body=share_api_body)

		return {"ShareId": share_request.ShareId}

	async def pde(self, lna: str, pdeRequest: PDERequest):
		user_id = None

		if (lna or "") != "":
			if user_id is None:
				user_id = await self.get_user_id(lna=lna)
			
			path = PARENT / CONVERSATION_ROOT / f"{user_id}/{pdeRequest.ConversationId}.json"
		else:
			path = self.s3_sync.find_file(PARENT / CONVERSATION_ROOT, id=pdeRequest.ConversationId)
		
		conversation = self.s3_sync.read_json(path, {}, PARENT / CONVERSATION_ROOT)
		
		if "mode" not in conversation:
			conversation["mode"] = "Chat"
		
		if AGENT_API_ENABLED:
			url = f"{BASE_URL}/api/pde"
			body = {
				"UserId": user_id,
				"Conversation": json.dumps(conversation),
				"SelectedOption": pdeRequest.SelectedOption,
				"OutputFormat": pdeRequest.OutputFormat,
				"Filename": pdeRequest.Filename,
				"Recipients": pdeRequest.Recipients,
				"IncludePageNumbers": pdeRequest.IncludePageNumbers
				}

			return await http_post(url=url, body=body)

		raise Exception("API is not enabled")