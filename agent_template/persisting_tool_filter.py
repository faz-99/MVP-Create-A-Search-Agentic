import json
import os 
import re
from agent_template.utils import (append_worklog_entry, utc_now, update_artifact_index, parse_json)
from agents.extensions import ( ToolOutputTrimmer )
from pathlib import Path
from typing import Any

DEFAULT_SANDBOX_PREPROCESS_THRESHOLD_BYTES = int(os.getenv("SANDBOX_PREPROCESS_THRESHOLD_BYTES", "50000"))
DEFAULT_SESSION_LIMIT = int(os.getenv("SESSION_TURN_LIMIT", "4"))
DEFAULT_INLINE_TOOL_OUTPUT_MAX_CHARS = int(os.getenv("SANDBOX_INLINE_TOOL_OUTPUT_MAX_CHARS", "8000"))
DEFAULT_TOOL_OUTPUT_PREVIEW_CHARS = int(os.getenv("SANDBOX_TOOL_OUTPUT_PREVIEW_CHARS", "600"))
DEFAULT_RECENT_TURNS = int(os.getenv("SANDBOX_RECENT_TURNS", "1"))
DEFAULT_COMPACTION_THRESHOLD = int(os.getenv("MODEL_COMPACTION_THRESHOLD", "60000"))
REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX_ROOT_DIR = Path(os.getenv("STORAGE_PATH", str(REPO_ROOT / "dataroom"))).resolve()


def sanitize_path_segment(value: str | None, *, fallback: str) -> str:
	if not value:
		return fallback
	sanitized = re.sub(r'[^A-Za-z0-9._-]+', '_', value).strip('._')
	return sanitized or fallback


class PersistingToolOutputFilter(ToolOutputTrimmer):
	def __init__(
		self,
		*,
		workspace_dir: Path,
		recent_turns: int = 2,
		max_output_chars: int = DEFAULT_INLINE_TOOL_OUTPUT_MAX_CHARS,
		preview_chars: int = DEFAULT_TOOL_OUTPUT_PREVIEW_CHARS,
		trimmable_tools: str | tuple[str, ...] | list[str] | None = None,
	) -> None:
		super().__init__(
			recent_turns=recent_turns,
			max_output_chars=max_output_chars,
			preview_chars=preview_chars,
			trimmable_tools=trimmable_tools,
		)
		self.workspace_dir = workspace_dir
		self.persist_dir = workspace_dir / "processed" / "tool_outputs"
		self.persist_dir.mkdir(parents=True, exist_ok=True)
		self.excluded_tools = {"list_workspace_artifacts", "read_workspace_artifact"}
	
	def _serialize_output(self, output: Any) -> str:
		
		try:

			if isinstance(output, list):
				tmp = output[0]
				if "type" in tmp and tmp["type"] == "input_text":
					return parse_json(tmp["text"]) or tmp["text"]
			elif isinstance(output, dict) and "type" in output  and output["type"] == "input_text":
				return parse_json(output["text"]) or output["text"]
			elif isinstance(output, dict) and "type" in output and output["type"] == "text":
				r = parse_json(output["text"])
				if r is None:
					return output["text"]
				else:
					return r
			else:
				if isinstance(output, str):
					return parse_json(output) or output
			return json.dumps(output, ensure_ascii=False, indent=2, default=str)
		except TypeError:
			if isinstance(output, str):
				return parse_json(output) or output
			return str(output)

	def _persist_function_output(
		self,
		item: dict[str, Any],
		tool_names: tuple[str, ...],
	) -> tuple[dict[str, Any] | None, int]:
		output_str = self._serialize_output(item.get("output", ""))
		output_len = len(output_str)
		if output_len <= self.max_output_chars:
			return None, 0

		tool_name = tool_names[0] if tool_names else "unknown_tool"
		bare_tool_name = tool_name.split(".")[-1]
		if bare_tool_name in self.excluded_tools:
			return None, 0

		call_id = str(item.get("call_id") or item.get("id") or "tool_output")
		filename = (
			f"{sanitize_path_segment(bare_tool_name, fallback='tool')}_"
			f"{sanitize_path_segment(call_id, fallback='call')}.txt"
		)
		artifact_path = self.persist_dir / filename
		is_new_artifact = not artifact_path.exists()
		if is_new_artifact:
			artifact_path.write_text(output_str, encoding="utf-8")

		relative_path = artifact_path.relative_to(self.workspace_dir).as_posix()
		artifact_entry = {
			"entry_key": f"{bare_tool_name}:{call_id}",
			"created_at": utc_now(),
			"kind": "tool_output",
			"tool_name": bare_tool_name,
			"call_id": call_id,
			"path": relative_path,
			"bytes": artifact_path.stat().st_size,
		}
		index_updated = update_artifact_index(workspace_dir=self.workspace_dir, artifact_entry=artifact_entry)
		if is_new_artifact or index_updated:
			append_worklog_entry(
				workspace_dir=self.workspace_dir,
				message=f"Persisted oversized output from {bare_tool_name} to {relative_path} ({output_len} chars)",
			)

		preview = output_str[: self.preview_chars]
		summary = (
			f"[Stored large tool output: {bare_tool_name} produced {output_len} chars. "
			f"Persisted at {relative_path}. Use list_workspace_artifacts or "
			f"read_workspace_artifact(relative_path=\"{relative_path}\") for details.]\n"
			f"Preview:\n{preview}..."
		)
		trimmed_item = dict(item)
		trimmed_item["output"] = summary
		return trimmed_item, output_len - len(summary)

	def __call__(self, data):
		from agents.run_config import ModelInputData as _ModelInputData

		model_data = data.model_data
		items = model_data.input
		if not items:
			return model_data

		call_id_to_names = self._build_call_id_to_names(items)
		new_items: list[Any] = []
		for item in items:
			if isinstance(item, dict) and item.get("type") == "function_call_output":
				call_id = str(item.get("call_id") or item.get("id") or "")
				tool_names = call_id_to_names.get(call_id, ())
				trimmed_item, _saved_chars = self._persist_function_output(item, tool_names)
				if trimmed_item is not None:
					new_items.append(trimmed_item)
					continue
			new_items.append(item)

		return _ModelInputData(input=new_items, instructions=model_data.instructions)
