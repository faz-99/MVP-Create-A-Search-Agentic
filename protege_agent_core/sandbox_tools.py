from agents import (
	function_tool
	)
from pathlib import Path
import json
import re
import subprocess
from agents.sandbox.entries import LocalDir
from agents.sandbox import Manifest
from pathlib import Path
from dotenv import load_dotenv

import os
load_dotenv()

def resolve_workspace_path(*, workspace_dir: Path, relative_path: str) -> Path:
	if not relative_path or not relative_path.strip():
		raise ValueError("relative_path is required")
	clean_relative_path = relative_path.replace("\\", "/").lstrip("/")
	resolved_path = (workspace_dir / clean_relative_path).resolve()
	if workspace_dir not in resolved_path.parents and resolved_path != workspace_dir:
		raise ValueError("Path escapes sandbox workspace")
	return resolved_path

def resolve_outputs_path(*, workspace_dir: Path, relative_path: str) -> Path:
	if not relative_path or not relative_path.strip():
		raise ValueError("relative_path is required")
	clean_relative_path = relative_path.replace("\\", "/").lstrip("/")
	if clean_relative_path.startswith("outputs/"):
		normalized_path = clean_relative_path
	else:
		normalized_path = f"outputs/{clean_relative_path}"
	resolved_path = resolve_workspace_path(workspace_dir=workspace_dir, relative_path=normalized_path)
	outputs_dir = (workspace_dir / "outputs").resolve()
	if outputs_dir not in resolved_path.parents and resolved_path != outputs_dir:
		raise ValueError("Path must stay inside outputs/")
	return resolved_path

def create_chat_completion_safe_tools(*, workspace_dir: Path) -> list:
	@function_tool(
		name_override="exec_command",
		description_override="Execute shell commands or scripts inside the sandbox workspace and return full output in the same call."
	)
	async def exec_command(
		command: str,
		timeout_seconds: int = 120,
	) -> str:
		completed = subprocess.run(
			command,
			cwd=str(workspace_dir),
			shell=True,
			capture_output=True,
			text=True,
			timeout=timeout_seconds,
		)
		output = (completed.stdout or "") + (completed.stderr or "")
		return json.dumps(
			{
				"running": False,
				"exit_code": completed.returncode,
				"output": output,
			},
			indent=2,
		)

	@function_tool(
		name_override="apply_patch",
		description_override="Create, update, or delete files in the sandbox workspace. Specify operation ('create', 'update', or 'delete'), relative_path, and content for create/update operations."
	)
	async def apply_patch(operation: str, relative_path: str, content: str = "") -> str:
		if operation not in ("create", "update", "delete"):
			raise ValueError(f"Invalid operation: {operation}. Must be 'create', 'update', or 'delete'")
		
		target_path = resolve_workspace_path(workspace_dir=workspace_dir, relative_path=relative_path)
		
		if operation == "create":
			if target_path.exists():
				raise ValueError(f"File already exists: {relative_path}")
			target_path.parent.mkdir(parents=True, exist_ok=True)
			target_path.write_text(content, encoding="utf-8")
			return json.dumps({
				"operation": "create",
				"path": relative_path,
				"bytes": target_path.stat().st_size,
				"ok": True
			}, indent=2)
		
		elif operation == "update":
			if not target_path.exists():
				raise ValueError(f"File not found: {relative_path}")
			target_path.write_text(content, encoding="utf-8")
			return json.dumps({
				"operation": "update",
				"path": relative_path,
				"bytes": target_path.stat().st_size,
				"ok": True
			}, indent=2)
		
		elif operation == "delete":
			if not target_path.exists():
				raise ValueError(f"File not found: {relative_path}")
			target_path.unlink()
			return json.dumps({
				"operation": "delete",
				"path": relative_path,
				"ok": True
			}, indent=2)

	return [exec_command, apply_patch]

def create_workspace_tools(*, workspace_dir: Path) -> list:
	@function_tool(name_override="list_workspace_artifacts", description_override="List persisted sandbox workspace artifacts and key metadata so the agent can reuse them without reloading raw content.")
	async def list_workspace_artifacts() -> str:
		artifact_rows = []
		for file_path in sorted(path for path in workspace_dir.rglob("*") if path.is_file()):
			relative_path = file_path.relative_to(workspace_dir).as_posix()
			artifact_rows.append(
				{
					"path": relative_path,
					"bytes": file_path.stat().st_size,
				}
			)
		return json.dumps({"workspace": ".", "artifacts": artifact_rows}, indent=2)

	@function_tool(name_override="read_workspace_artifact", description_override="Read a selected persisted sandbox artifact by relative path and optional line window, so the agent can inspect only the needed portion.")
	async def read_workspace_artifact(relative_path: str, start_line: int = 1, end_line: int = 200) -> str:
		resolved_path = resolve_workspace_path(workspace_dir=workspace_dir, relative_path=relative_path)
		if not resolved_path.exists() or not resolved_path.is_file():
			raise ValueError(f"Artifact not found: {relative_path}")
		if start_line < 1:
			start_line = 1
		if end_line < start_line:
			end_line = start_line
		with resolved_path.open("r", encoding="utf-8", errors="replace") as handle:
			lines = handle.readlines()
		selected_lines = lines[start_line - 1:end_line]
		cleaned_lined = []
		for s in selected_lines:
			if len(s) > 2000:
				cleaned_lined.append(f"{s[1:2000]} [Line too large total size {len(s)}] ...")
			else:
				cleaned_lined.append(s)
		return json.dumps(
			{
				"path": resolved_path.relative_to(workspace_dir).as_posix(),
				"start_line": start_line,
				"end_line": min(end_line, len(lines)),
				"content": "".join(cleaned_lined),
			},
			indent=2,
		)
	
	def _is_text_file(path: Path) -> bool:
		try:
			with path.open("r", encoding="utf-8", errors="strict") as handle:
				handle.read(1024)
			return True
		except Exception:
			return False

	@function_tool(
		name_override="rg_search",
		description_override="Recursively search text files in the sandbox workspace and return rg-style matches as path:line:text."
	)
	async def rg_search(
		pattern: str,
		file_glob: str = "*",
		max_results: int = 200,
		case_sensitive: bool = False,
	) -> str:
		if not pattern or not pattern.strip():
			raise ValueError("pattern is required")

		flags = 0 if case_sensitive else re.IGNORECASE
		regex = re.compile(pattern, flags)

		matches = []
		searched_files = 0

		for file_path in workspace_dir.rglob(file_glob):
			if not file_path.is_file():
				continue
			if not _is_text_file(file_path):
				continue

			searched_files += 1

			with file_path.open("r", encoding="utf-8", errors="replace") as handle:
				for line_number, line in enumerate(handle, start=1):
					if regex.search(line):
						relative_path = file_path.relative_to(workspace_dir).as_posix()
						matches.append(f"{relative_path}:{line_number}:{line.rstrip()}")
						if len(matches) >= max_results:
							return "\n".join(matches) + f"\n[rg_search truncated after {max_results} matches across {searched_files} files]"

		if matches:
			return "\n".join(matches)
		return f"[rg_search: no matches for pattern {pattern!r} in {searched_files} files]"

	@function_tool(name_override="write_workspace_output", description_override="Write text content to a file inside the sandbox outputs directory. Use this to persist final deliverables or intermediate results that should be reused later.")
	async def write_workspace_output(relative_path: str, content: str, overwrite: bool = False) -> str:
		resolved_path = resolve_outputs_path(workspace_dir=workspace_dir, relative_path=relative_path)
		if resolved_path.exists() and not overwrite:
			raise ValueError(f"Artifact already exists: {resolved_path.relative_to(workspace_dir).as_posix()}")
		resolved_path.parent.mkdir(parents=True, exist_ok=True)
		resolved_path.write_text(content, encoding="utf-8")
		return json.dumps(
			{
				"path": resolved_path.relative_to(workspace_dir).as_posix(),
				"bytes": resolved_path.stat().st_size,
				"overwritten": overwrite,
			},
			indent=2,
		)
	
	return [list_workspace_artifacts, read_workspace_artifact, write_workspace_output, rg_search]
