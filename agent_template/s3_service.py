import boto3
import json
import os
import shutil
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
load_dotenv()

class S3Sync:
    _s3_client = None

    def delete_file(self, file_path: Path, s3_key: Path) -> None:
        if file_path.exists():
            file_path.unlink()
        if not self._is_s3_storage_enabled():
            return
        
        self._delete_s3_object(str(s3_key))

    def read_json(self, file_path: Path, default: Any, root_dir: Path) -> Any:
        if not  root_dir.exists():
            return default
        return self._read_json(file_path, default)

    def write_json(self, file_path: Path, content: Any, root_dir: Path, ignore_upload: bool = False) -> None:
        if not root_dir.exists():
            root_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(file_path, content, root_dir=root_dir, ignore_upload=ignore_upload)

    def _read_json(self, file_path: Path, default: Any) -> Any:
        if file_path is None or not file_path.exists():
            return default
        return json.loads(file_path.read_text(encoding="utf-8"))

    def _write_json(self, file_path: Path, content: Any, root_dir: Path, ignore_upload: bool = False) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
        if not ignore_upload:
            self._upload_file_to_s3(file_path, destination_key=file_path.relative_to(root_dir))

    def _build_s3_client(self) -> Any:
        if self._s3_client is not None:
            return self._s3_client
        
        client_kwargs: dict[str, Any] = {}
        region_name = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
        if region_name:
            client_kwargs["region_name"] = region_name.strip()
        S3Sync._s3_client = boto3.client("s3", **client_kwargs)

        return S3Sync._s3_client

    def _is_s3_storage_enabled(self) -> bool:
        return os.getenv("ENABLE_S3_STORAGE", "false").strip().lower() in {"1", "true", "yes", "on"}

    def _s3_bucket_name(self) -> str:
        bucket_name = os.getenv("S3_BUCKET", "").strip()
        if not bucket_name:
            raise RuntimeError("S3_BUCKET is required when S3 storage is enabled.")
        return bucket_name

    def _s3_prefix(self) -> str:
        return os.getenv("S3_PREFIX", "agent_openai_sdk").strip().strip("/")

    def _to_s3_key(self, relative_path: str) -> str:
        prefix = self._s3_prefix()
        path = relative_path.replace("\\", "/")
        return f"{prefix}/{path}" if prefix else path
    
    def _from_s3_key(self, key: str) -> str | None:
        prefix = self._s3_prefix()
        if prefix:
            prefix_with_separator = f"{prefix}/"
            if key == prefix:
                return None
            if not key.startswith(prefix_with_separator):
                return None
            return key.removeprefix(prefix_with_separator)
        return key


    def _iter_local_files(self, root_dir: Path) -> list[Path]:
        if not root_dir.exists():
            return []
        return [path for path in root_dir.rglob("*") if path.is_file()]
    
    
    def find_file(self, root_dir: Path, id: str) -> list[Path]:
        if not root_dir.exists():
            return []
        for path in root_dir.glob("*"):
            if path.is_dir():
                for f in path.glob("*"):
                    if f.is_file() and f.name == f"{id}.json":
                        return path / f.name
            if path.is_file() and path.name == f"{id}.json":
                return path.parent / path.name
    
    def _upload_file_to_s3(self, file_path: Path, destination_key: Path) -> None:
        if not self._is_s3_storage_enabled():
            return
        client = self._build_s3_client()

        client.upload_file(
            str(file_path),
            self._s3_bucket_name(),
            self._to_s3_key(str(destination_key))
        )
    
    def _delete_s3_object(self, relative_path: str) -> None:
        if not self._is_s3_storage_enabled():
            return
        try:
            self._build_s3_client().delete_object(Bucket=self._s3_bucket_name(), Key=self._to_s3_key(relative_path))
        except Exception as e:
            print(e)
    def copy_file(self, source: Path, destination: Path) -> None:
        if source.exists():
            shutil.copyfile(src=source, dst=destination)

    def sync_data_dir_from_s3(self, data_dir: Path = None, s3_dir: Path = None) -> None:
        try:
            if not self._is_s3_storage_enabled():
                return
            if data_dir is None:
                data_dir = self._folder
            resolved_data_dir = data_dir.resolve()
            if not resolved_data_dir.exists():
                resolved_data_dir.mkdir(parents=True, exist_ok=True)
            client = self._build_s3_client()
            bucket_name = self._s3_bucket_name()
            paginator = client.get_paginator("list_objects_v2")
            folder_prefix = self._to_s3_key(s3_dir.as_posix().strip("/"))

            for page in paginator.paginate(Bucket=bucket_name, Prefix=folder_prefix):
                for item in page.get("Contents", []):
                    relative_path = self._from_s3_key(str(item.get("Key", "")))
                    if not relative_path:
                        continue
                    
                    final_path = data_dir / relative_path
                    r = final_path.parent.resolve()
                    if not r.exists():
                        r.mkdir(parents=True, exist_ok=True)
                    
                    if not final_path.is_file() or final_path.stat().st_size != int(item.get("Size", 0)):
                        client.download_file(bucket_name, item["Key"], final_path)

        except Exception as e:
            pass
