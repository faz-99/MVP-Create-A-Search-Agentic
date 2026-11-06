import sys
from agents import (Agent, WebSearchTool, RunConfig, SessionSettings)
from agents.model_settings import ModelSettings
from agents.sandbox.entries import LocalDir
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Filesystem, Shell
if sys.platform != "win32":
	from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
	from .persisting_tool_filter import PersistingToolOutputFilter
from pathlib import Path
from dotenv import load_dotenv
from agent_template.sandbox_tools import (resolve_outputs_path, create_workspace_tools, create_chat_completion_safe_tools)
from agent_template.utils import utc_now, env_flag
from typing import Any
import json
import os
import re
load_dotenv()
DEFAULT_SANDBOX_PREPROCESS_THRESHOLD_BYTES = int(os.getenv("SANDBOX_PREPROCESS_THRESHOLD_BYTES", "50000"))
ENV = os.getenv("ENV", "dev")
REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX_ROOT_DIR = Path(os.getenv("STORAGE_PATH", str(REPO_ROOT / "dataroom"))).resolve()
DEFAULT_INLINE_TOOL_OUTPUT_MAX_CHARS = int(os.getenv("SANDBOX_INLINE_TOOL_OUTPUT_MAX_CHARS", "8000"))
DEFAULT_TOOL_OUTPUT_PREVIEW_CHARS = int(os.getenv("SANDBOX_TOOL_OUTPUT_PREVIEW_CHARS", "600"))
DEFAULT_RECENT_TURNS = int(os.getenv("SANDBOX_RECENT_TURNS", "1"))
USE_CUSTOM_BASE_INSTRUCTIONS_SANDBOX_TOOL = env_flag("USE_CUSTOM_BASE_INSTRUCTIONS_SANDBOX_TOOL", False)
SUB_AGENT_BASE_PROMPT_PATH = Path(__file__).parent.parent / "src/system_prompts/sub_agent_base_prompt.txt"
async def write_workspace_output(workspace_dir: Path, relative_path: str, content: str, overwrite: bool = False) -> str:
	resolved_path = resolve_outputs_path(workspace_dir=workspace_dir, relative_path=relative_path)
	if resolved_path.exists() and not overwrite:
		return
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
def ensure_sandbox_manifest(*, conversation_id: str, user_id: str | None) -> tuple[Manifest, Path]:
	workspace_dir = build_sandbox_workspace_dir(conversation_id=conversation_id, user_id=user_id)
	initialize_sandbox_workspace(workspace_dir=workspace_dir)
	manifest_src = build_manifest_local_dir_src(conversation_id=conversation_id, user_id=user_id)
	return Manifest(root="/workspace", entries={Path("."): LocalDir(src=manifest_src)}), workspace_dir


def build_sandbox_workspace_dir(*, conversation_id: str, user_id: str | None) -> Path:
	user_segment = sanitize_path_segment(user_id, fallback="anonymous")
	conversation_segment = sanitize_path_segment(conversation_id, fallback="conversation")
	return SANDBOX_ROOT_DIR / user_segment / conversation_segment


def initialize_sandbox_workspace(*, workspace_dir: Path) -> dict[str, Path]:
	paths = {
		"workspace": workspace_dir,
		"raw": workspace_dir / "raw",
		"processed": workspace_dir / "processed",
		"outputs": workspace_dir / "outputs",
		"logs": workspace_dir / "logs",
	}
	for path in paths.values():
		path.mkdir(parents=True, exist_ok=True)
	workspace_guide = workspace_dir / "WORKSPACE_GUIDE.txt"
	if not workspace_guide.exists():
		workspace_guide.write_text(
			"Sandbox workspace layout:\n"
			"Use only relative paths inside the sandbox, for example ./raw/file.pdf or processed/tool_outputs/output.txt.\n"
			"- raw/: original downloaded or copied source files\n"
			"- processed/: normalized text, extracted tables, parsed sections, and other working artifacts\n"
			"- outputs/: final compact artifacts intended for reasoning or delivery\n"
			"- logs/: optional execution notes or tool logs\n"
			"- outputs/TODO.md: current step-by-step plan\n"
			"- logs/WORKLOG.md: running notes and observations\n"
			"- outputs/SUMMARY.md: compressed state for reuse\n"
			"- outputs/ARTIFACT_INDEX.json: index of saved artifacts\n",
			encoding="utf-8",
		)
	paths["guide"] = workspace_guide
	return paths


def build_manifest_local_dir_src(*, conversation_id: str, user_id: str | None) -> Path:
	return build_sandbox_workspace_dir(conversation_id=conversation_id, user_id=user_id)


def sanitize_path_segment(value: str | None, *, fallback: str) -> str:
	if not value:
		return fallback
	sanitized = re.sub(r'[^A-Za-z0-9._-]+', '_', value).strip('._')
	return sanitized or fallback


def build_sandbox_agent(conversation_id: str, user_id: str, prompt: str, server_name: str, model: Any, 
						system_prompt:str, workspace_dir: str, mcp_server: object, reasoning: dict[str, str] | None,
						manifest: Manifest, headers: dict[str,str], extra_body:Any | None = None, add_web_search_tool: bool = True, ignore_capabilities: bool = False) -> Agent:
	model_label = resolve_model_label(model)
	# manifest, workspace_dir = ensure_sandbox_manifest(conversation_id=conversation_id, user_id=user_id)
	_ = initialize_sandbox_workspace(workspace_dir=workspace_dir)
	_ = seed_sandbox_memory_files(
        workspace_dir=workspace_dir,
        conversation_id=conversation_id,
        user_id=user_id,
        prompt=prompt,
    )
	_ = write_sandbox_run_manifest(
        workspace_dir=workspace_dir,
        conversation_id=conversation_id,
        user_id=user_id,
		model=model_label,
        server_name=server_name,
        prompt=prompt,)
	instructions = build_runtime_instructions(system_prompt=system_prompt, workspace_dir=workspace_dir)
	tools = create_workspace_tools(workspace_dir=workspace_dir)
	# mcp_tools = build_mcp_tools(headers=headers)
	agent_tools = [*tools]
	capabilities = []
	if add_web_search_tool:
		agent_tools.insert(0, WebSearchTool())
	else:
		extra_tools = create_chat_completion_safe_tools(workspace_dir=workspace_dir)
		agent_tools.extend(extra_tools)
	if not ignore_capabilities:
		capabilities = [Filesystem()]
	
	mcps = []
	if mcp_server:
		mcps.append(mcp_server)
	base_instructions = None
	if USE_CUSTOM_BASE_INSTRUCTIONS_SANDBOX_TOOL:
		with open(SUB_AGENT_BASE_PROMPT_PATH, encoding='utf8') as f:
			base_instructions = f.read()
	return SandboxAgent(
			name="Researcher",
			instructions=instructions,
			mcp_servers=mcps,
			base_instructions=base_instructions,
			model=model,
			tools=agent_tools,
			default_manifest=manifest,
			capabilities=capabilities,
			model_settings=ModelSettings(tool_choice="auto", truncation="disabled", store=False, 
								context_management=[{"type": "compaction", "compact_threshold": 800000}],
								reasoning=reasoning,
								extra_headers={"X-Conversation": conversation_id},
								extra_body=extra_body,
								verbosity="medium"),
		)


def resolve_model_label(model: Any) -> str:
	if isinstance(model, str):
		return model
	label = getattr(model, "model", None)
	if isinstance(label, str) and label.strip():
		return label
	return model.__class__.__name__


def seed_sandbox_memory_files(
	*,
	workspace_dir: Path,
	conversation_id: str,
	user_id: str | None,
	prompt: str,
) -> dict[str, Path]:
	memory_files = {
		"todo": workspace_dir / "outputs" / "TODO.md",
		"worklog": workspace_dir / "logs" / "WORKLOG.md",
		"summary": workspace_dir / "outputs" / "SUMMARY.md",
		"artifact_index": workspace_dir / "outputs" / "ARTIFACT_INDEX.json",
	}
	if not memory_files["todo"].exists():
		memory_files["todo"].write_text(
			"# TODO\n"
			"- [ ] Resolve the current request\n"
			"- [ ] Persist large source artifacts before deep analysis\n"
			"- [ ] Update SUMMARY.md with only the compact state worth carrying forward\n",
			encoding="utf-8",
		)
	if not memory_files["worklog"].exists():
		memory_files["worklog"].write_text(
			f"# WORKLOG\n\nConversation: {conversation_id}\nUser: {user_id or 'anonymous'}\n\nInitial request:\n{(prompt or '').strip()}\n",
			encoding="utf-8",
		)
	if not memory_files["summary"].exists():
		memory_files["summary"].write_text(
			"# SUMMARY\n\n"
			"Use this file for compressed carry-forward state only. Keep it short and replace raw detail with file references into raw/, processed/, and outputs/.\n",
			encoding="utf-8",
		)
	if not memory_files["artifact_index"].exists():
		memory_files["artifact_index"].write_text(
			json.dumps(
				{
					"conversation_id": conversation_id,
					"user_id": user_id,
					"artifacts": [],
				},
				indent=2,
			),
			encoding="utf-8",
		)
	return memory_files


def write_sandbox_run_manifest(
	*,
	workspace_dir: Path,
	conversation_id: str,
	user_id: str | None,
	model: str,
	server_name: str,
	prompt: str,
) -> Path:
	manifest_path = workspace_dir / "run_manifest.json"
	relative_workspace_dir = Path(".")
	manifest_payload = {
		"created_at": utc_now(),
		"environment": ENV,
		"conversation_id": conversation_id,
		"user_id": user_id,
		"model": model,
		"server_name": server_name,
		"workspace_dir": str(relative_workspace_dir),
		"raw_dir": "raw",
		"processed_dir": "processed",
		"outputs_dir": "outputs",
		"logs_dir": "logs",
		"todo_file": "outputs/TODO.md",
		"worklog_file": "logs/WORKLOG.md",
		"summary_file": "outputs/SUMMARY.md",
		"artifact_index_file": "outputs/ARTIFACT_INDEX.json",
		"prompt_preview": (prompt or "")[:500],
	}
	manifest_path.write_text(json.dumps(manifest_payload, indent=2), encoding="utf-8")
	return manifest_path


def build_runtime_instructions(
	*,
	system_prompt: str | None,
	preprocess_threshold_bytes: int = DEFAULT_SANDBOX_PREPROCESS_THRESHOLD_BYTES,
	workspace_dir: str | None
) -> str:
	# or shell tools
	sandbox_policy = f"""Sandbox preprocessing policy:
- You have sandbox filesystem and shell tools available by default.
- Use sandbox as the default execution substrate for file-centric work.
- When MCP tools return PDFs, HTML files, text files, downloadable file references, or very large document bodies, prefer to save the material into the sandbox workspace, preprocess it there, and bring back only compact artifacts needed for reasoning.
- Persist artifacts by default: store original downloaded or copied source files in ./raw, normalized or extracted working data in ./processed, and final compact deliverables in ./outputs.
- Maintain on-disk working memory inside the sandbox workspace. Keep the current plan in ./outputs/TODO.md, cumulative working notes in ./logs/WORKLOG.md, compressed state in ./outputs/SUMMARY.md, and stored artifact references in ./outputs/ARTIFACT_INDEX.json.
- Before asking for previously seen content again, use the workspace artifact tools to inspect TODO.md, SUMMARY.md, ARTIFACT_INDEX.json, or specific files under ./raw/, ./processed/, ./outputs/, and ./logs/. Use write_workspace_output to persist text deliverables under outputs/ when they should be reused or inspected later.
- When using sandbox filesystem or shell tools, always use sandbox-relative paths like ./raw/file.txt or processed/tool_outputs/file.txt. Never use host absolute paths such as /home/... or C:/... inside sandbox tool calls.
- Use sandbox preprocessing whenever the payload is likely larger than {preprocess_threshold_bytes} bytes, spans multiple sections or pages, requires cleanup, parsing, chunking, deduplication, or tabular extraction.
- Keep raw file contents out of the main context unless exact quotation, excerpt markup, or full-document inspection is necessary.
- Return compact outputs from sandbox work such as section maps, extracted excerpts, normalized text, tables, issue lists, and concise summaries.
- Sandbox preprocessing of IGZ or user-provided material is a transformation of already-authorized content and does not require separate permission.
- When a source is large enough to justify preprocessing, do not stop at transient inspection. Write the intermediate files to disk inside the sandbox workspace unless the source is too small for persistence to add value.
- Before making another large tool call or restating prior results, first consult and update the workspace memory files so the next step can build on compact persisted state rather than raw repeated context.
"""

	shell = f"""
	- For every shell command, first change directory to /{workspace_dir}.
	- Treat /{workspace_dir} as the only working directory for shell execution.
	- After changing to /{workspace_dir}, use only relative paths such as ./raw, ./processed, ./outputs, and ./logs.
	- Do not use /tmp paths unless the task explicitly requires temporary scratch space.
	- If a shell command prints or resolves to a /tmp path, change back to /{workspace_dir} before reading or writing artifacts intended for reuse.
	"""

	parts = [
		f"Today is {utc_now(only_date=True)}",
		system_prompt,
		sandbox_policy,
		shell
	]
	return "\n\n".join(part for part in parts if part)

if sys.platform != "win32":
    def build_run_config_for_agent(*, manifest: Manifest, workspace_dir: Path, ) -> RunConfig:
        return RunConfig(
            session_settings=SessionSettings(limit=10),
            trace_include_sensitive_data=True,
            sandbox=SandboxRunConfig(client=UnixLocalSandboxClient(), manifest=manifest),
			call_model_input_filter=PersistingToolOutputFilter(
				workspace_dir=workspace_dir,
				recent_turns=DEFAULT_RECENT_TURNS,
				max_output_chars=DEFAULT_INLINE_TOOL_OUTPUT_MAX_CHARS,
				preview_chars=DEFAULT_TOOL_OUTPUT_PREVIEW_CHARS,
			),
        )