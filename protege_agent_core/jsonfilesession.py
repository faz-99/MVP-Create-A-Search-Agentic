from pathlib import Path
from typing import List
from agents.items import TResponseInputItem
from agents.memory.session import SessionABC
from .s3_service import S3Sync
from dotenv import load_dotenv
import asyncio
import json
import os
load_dotenv()
DEFAULT_INLINE_TOOL_OUTPUT_MAX_CHARS = int(os.getenv("INLINE_TOOL_OUTPUT_MAX_CHARS", "1000"))
DEFAULT_TOOL_OUTPUT_PREVIEW_CHARS = int(os.getenv("TOOL_OUTPUT_PREVIEW_CHARS", "200"))
STORAGE_PATH= Path(os.getenv("STORAGE_PATH", ""))
AGENT_TOOL_OUTPUT_ROOT_DIR = STORAGE_PATH / Path(os.getenv("AGENT_TOOL_OUTPUT_DIR", "./"))
def test_summarize():
    return os.getenv("TEST_SESSION_SUMMARY", "false").lower() == "true"

class JsonFileSession(SessionABC):
    def __init__(self, session_id: str, base_dir: str = "sessions", retry:int = 0, ignore_copy: bool = False):
        self.session_id = session_id
        self.base_dir = (STORAGE_PATH / Path(base_dir)).expanduser().resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = STORAGE_PATH / Path(base_dir) / f"{session_id}.json"
        self.s3_sync = S3Sync()

    async def create_copy(self):
        await asyncio.to_thread(self._create_copy)

    async def get_items(self, limit: int | None = None) -> List[TResponseInputItem]:
        items = self._read_items()
        if limit is None or len(items) <= limit or limit > 10:
            return items

        return self._get_last_complete_turns(items, limit)

    async def add_items(self, items: List[TResponseInputItem]) -> None:
        existing = self._read_items()
        existing.extend(items)
        await asyncio.to_thread(self._write_items, existing)

    async def pop_item(self) -> TResponseInputItem | None:
        items = self._read_items()
        if not items:
            return None
        last = items.pop()
        await asyncio.to_thread(self._write_items, items)
        return last

    async def clear_session(self) -> None:
        await asyncio.to_thread(self._write_items, [])

    def _read_items(self) -> List[TResponseInputItem]:
        if not self.file_path.exists():
            return []
        return json.loads(self.file_path.read_text(encoding="utf-8"))

    def _write_items(self, items: List[TResponseInputItem], file_path: Path = None) -> None:
        final_path = self.file_path
        if file_path is not None:
            final_path = file_path

        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_text(
            json.dumps(items, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    
    def _get_last_complete_turns(
        self, items: List[TResponseInputItem], max_turns: int
    ) -> List[TResponseInputItem]:
        if not items or max_turns <= 0:
            return []

        turns = self._split_into_turns(items)
        kept_turns = turns[-max_turns:]
        kept_items = [item for turn in kept_turns for item in turn]

        return self._expand_to_include_missing_tool_calls(items, kept_items)

    def _split_into_turns(
        self, items: List[TResponseInputItem]
    ) -> List[List[TResponseInputItem]]:
        turns: List[List[TResponseInputItem]] = []
        current_turn: List[TResponseInputItem] = []
                
        for item in items:
            if item.get("role") == "user" and current_turn:
                turns.append(current_turn)
                current_turn = [item]
            else:
                current_turn.append(item)

        if current_turn:
            turns.append(current_turn)

        return turns

    def _expand_to_include_missing_tool_calls(
        self,
        all_items: List[TResponseInputItem],
        kept_items: List[TResponseInputItem],
    ) -> List[TResponseInputItem]:
        if not kept_items:
            return kept_items

        start_index = len(all_items) - len(kept_items)

        required_call_ids = {
            item["call_id"]
            for item in kept_items
            if item.get("type") == "function_call_output" and item.get("call_id")
        }
        present_call_ids = {
            item["call_id"]
            for item in kept_items
            if item.get("type") == "function_call" and item.get("call_id")
        }

        missing_call_ids = required_call_ids - present_call_ids

        while missing_call_ids and start_index > 0:
            start_index -= 1
            item = all_items[start_index]

            if item.get("type") == "function_call" and item.get("call_id") in missing_call_ids:
                present_call_ids.add(item["call_id"])
                missing_call_ids = required_call_ids - present_call_ids

        return all_items[start_index:]
    
    async def complete(self):
        await asyncio.to_thread(self.s3_sync._upload_file_to_s3, self.file_path, self.file_path.relative_to(STORAGE_PATH))

    def _create_copy(self):
        items = self._read_items()
        turn = 1
        for i in items:
            if i.get("role") == "user":
                turn = turn + 1

        if turn > 1:
            copy_path = self.file_path.with_name(f"{self.file_path.stem}_{turn}{self.file_path.suffix}")
            self._write_items(items=items, file_path=copy_path)
        
    def recreate_from(self, turn: int):
        if turn == 1:
            if self.file_path.exists():
                self.file_path.unlink()
        else:
            copy_path = self.file_path.with_name(f"{self.file_path.stem}_{turn}{self.file_path.suffix}")
            if copy_path.exists():
                items = json.loads(copy_path.read_text(encoding="utf-8"))
                self._write_items(items=items)

        tmp_turn = turn + 1
        while True:
            tmp_path = self.file_path.with_name(f"{self.file_path.stem}_{tmp_turn}{self.file_path.suffix}")
            if not tmp_path.exists():
                break
            tmp_path.unlink()
            tmp_turn = tmp_turn + 1

    def remove_last_user_message(self):
        items = self._read_items()
        for i in range(len(items) - 1, -1, -1):
            if items[i].get("role") == "user":
                items.remove(items[i])
                break
        self._write_items(items=items)

    def _compact(self):
        try:
            if not test_summarize():
                return
            items = self._read_items()
            
            for k in items:
                t = k.get("type", None)
                if t is not None:
                    t = k["type"]
                    
                    if t == "function_call_output":
                        output = k.get("output", [])
                        if not isinstance(output, str):
                            call_id = k.get("call_id")
                            for o in output:
                                text = o.get("text", None)
                                if text is not None:
                                    text_size = len(text)
                                    if text_size > DEFAULT_INLINE_TOOL_OUTPUT_MAX_CHARS:
                                        self.save_tool_output(text=text, call_id=call_id)
                                        text= f"[Stored large tool output call_id: {call_id} produced {text_size} chars. Use recover_tool_output(call_id=\"{call_id}\", session_id=\"{self.session_id}\") for details]. Preview {text[:DEFAULT_TOOL_OUTPUT_PREVIEW_CHARS]}"
                                        o["text"] = text
            self._write_items(items=items)
        except Exception as e:
            print(e)

    def save_tool_output(self, call_id: str, text: str):
        output_tool = AGENT_TOOL_OUTPUT_ROOT_DIR / "output" / self.session_id 
        output_tool.mkdir(parents=True, exist_ok=True)
        with open(output_tool / f"{call_id}.txt", 'w', encoding='utf8') as f:
            f.write(text)
        