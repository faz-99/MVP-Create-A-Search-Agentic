import os
from dotenv import load_dotenv
from agents import Agent, RunHooks
from agents.run_context import RunContextWrapper
from agents.items import TResponseInputItem
load_dotenv()

class ContextTrimmingHook(RunHooks):
	
	def __init__(self):
		super().__init__()
		self._last_trimmed = False
		self._current_turn = 0
		self._max_pos = 0

	def _get_role(self, item: TResponseInputItem) -> str | None:
		if isinstance(item, dict):
			return item.get("role") or item.get("type") == "message"
		return getattr(item, "role", None)

	
	async def on_llm_start(
		self,
		context: RunContextWrapper,
		agent: Agent,
		system_prompt: str | None,
		input_items: list[TResponseInputItem],
	) -> None:
		enabled = os.getenv("ENABLE_CONTEXT_TRIMMING", "")
		if enabled in ("yes", "1"):
			self._current_turn = self._current_turn + 1
			new_items = []
			last_reasoning = None
			tools_to_keep = []
			if self._current_turn == 1:
				for item in input_items:
					item_type = item.get("type")
					if self._get_role(item=item):
						new_items.append(item)
					elif item_type == "reasoning":
						item["summary"] = []
						last_reasoning = item
					elif item_type == "function_call":
						fnName = item.get("name")
						if "load_tools" in fnName or "list_tools" in fnName or "find" in fnName or "search" in fnName:
							new_items.append(item)
							tools_to_keep.append(item.get("call_id"))
					elif item_type == "function_call_output":
						if item.get("call_id") in tools_to_keep:
							new_items.append(item)
					self._max_pos = self._max_pos + 1
				if last_reasoning is not None:
					new_items.insert(-1, last_reasoning)
				input_items[:] = new_items
				return
			else:
				current_pos = 0
				
				for item in input_items:
					current_pos = current_pos + 1
					if current_pos > self._max_pos:
						if last_reasoning is not None:
							new_items.insert(-1, last_reasoning)
							last_reasoning = None
						new_items.append(item)
					else:
						item_type = item.get("type");
						if self._get_role(item=item):
							new_items.append(item)
						if item_type == "reasoning":
							item["summary"] = []
							last_reasoning = item
						elif item_type == "function_call":
							if "find" in item.get("name") or "search" in  item.get("name"):
								new_items.append(item)
								tools_to_keep.append(item.get("call_id"))
						elif item_type == "function_call_output":
							if item.get("call_id") in tools_to_keep:
								new_items.append(item)
				input_items[:] = new_items
				return
		return   