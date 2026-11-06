import boto3
import json
import os
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from dotenv import load_dotenv
from agents import OpenAIResponsesModel
from openai import AsyncOpenAI
from protege_agent_core.api.request import ChatRequest
import asyncio
import httpx
import logging
load_dotenv()
logger = logging.getLogger(__name__)
DEFAULT_PROVIDER = "openai"
DEFAULT_OPENAI_MODEL = "gpt-5.4-2026-03-05"
DEFAULT_VERTEX_MODEL = "gemini-3-flash-preview"
SECRETS_FILE = "secrets.json"
USER_PREFERENCES_API = os.getenv("USER_PREFERENCES_API", "")
VAULT_URL = os.getenv("VAULT_URL", "")
keys = { }
_http_client = httpx.AsyncClient(timeout=300, verify=False, http2=True)
STORAGE_PATH = os.getenv("STORAGE_PATH", "")
def syncSecrets()  -> dict:
    global keys
    if len(keys.keys()) > 0:
        return keys

    filecache = SECRETS_FILE
    file = Path(filecache)
    keepFile = os.getenv("KEEP_FILE", "false")

    if not file.exists():
        secretName = os.getenv("MODELS_SECRETS")
        secrets = getSecret(secretName=secretName)
        keys["openai"] = {}
        keys["gemini"] = {}
        keys["claude"] = {}
        default_model_claude = os.getenv("DEFAULT_CLAUDE_MODEL", "")
        for k in secrets.keys():
            if "open_ai" in k:
                keys["openai"][k] = secrets[k]
            elif "gemini" in k:
                keys["gemini"][k] = secrets[k]
            elif "claude" in k and default_model_claude:
                keys["claude"][k] = secrets[k]
        with open(filecache, 'w', encoding="utf8") as f:
            f.write(json.dumps(keys))
    else:
        with open(filecache, 'r', encoding='utf8') as f:
            keys = json.loads(f.read())
        
    if keepFile not in ("1", "true"):
        file.unlink()
    return keys

def get_available_models():
    global keys
    result = {}
    result["DefaultGemini"] = os.getenv("DEFAULT_GEMINI_MODEL", "gemini_3_flash")
    result["DefaultOpenai"] = os.getenv("DEFAULT_OPENAI_MODEL","open_ai_gpt_5_4_2026_03_05")
    defuault_claude_model = os.getenv("DEFAULT_CLAUDE_MODEL", "")
    default_fast_moel = os.getenv("DEFAULT_FAST_MODEL", "")
    result["DefaultClaude"] = defuault_claude_model
    result["DefaultFastModel"] = default_fast_moel
    for k in keys.keys():
        result[k] = []
        for v in keys[k].keys():
            if k != "claude" or (k == "claude" and defuault_claude_model != "" and ("bedrock" in str(v).lower() or "claude" in str(v).lower())):
                result[k].append(v)
    return result

def get_queue_connection_string() -> str:
    sn = os.getenv("SECRET_NAME_RABBIT", "")
    snKey = os.getenv("SNKEY_RABBIT", "connStr")
    if sn == "":
        return "host=localhost"
    secret = get_secret_value(sn)
    secret_value = json.loads(secret["SecretString"])
    return secret_value[snKey]
    

def get_model_config(provider: str, modelKey: str = "") -> dict[str, str]:
    global keys
    secrets = keys[provider]
    
    filecache = f"{provider}_{SECRETS_FILE}"
    file = Path(filecache)
    if secrets is None and file.exists():
        with open(filecache, 'r', encoding='utf8') as f:
            secrets = json.loads(f.read())
    
    if provider == "gemini":
        
        secretValue = json.loads(secrets[modelKey])[0]
        googleCredentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        credentials = secretValue["ModelJsonKey"].replace("\n", "\\n")
        
        project = json.loads(credentials)["project_id"]
        os.environ["GEMINI_MODEL"] = secretValue["Model"]
        os.environ["VERTEX_PROJECT_ID"] = project
        
        with open(googleCredentials, 'w', encoding='utf-8') as f:
            f.write(credentials)
        return {
            "model": secretValue["Model"],
            "vertexprojectid": project,
            "requesturi": secretValue["RequestUri"],
            "project": project
        }
    elif provider == "openai":
        if secrets is not None:
            values = json.loads(secrets[modelKey])[0]
            
            return {
                "model": values["Model"],
                "apikey": values["ApiKey"],
                "baseurl": values["RequestUri"].split("v1", 1)[0] + "/v1"
            }
        
        secretName =os.getenv("SECRET_NAME_OPEN_AI")
        values = getSecret(secretName=secretName, modelKey="open_ai_gpt_5_4_2026_03_05")[0]
        return {
            "model": values["Model"],
            "apikey": values["ApiKey"],
            "baseurl": values["RequestUri"].split("v1", 1)[0] + "/v1"
        }
    elif provider == "claude":
        if secrets is not None:
            values = json.loads(secrets[modelKey])[0]
            
            return {
                "model": values["Model"],
                "apiKey": values["ApiKey"]
            }

def update_artifact_index(*, workspace_dir: Path, artifact_entry: dict[str, Any]) -> bool:
	artifact_index_path = workspace_dir / "outputs" / "ARTIFACT_INDEX.json"
	if artifact_index_path.exists():
		artifact_index = json.loads(artifact_index_path.read_text(encoding="utf-8"))
	else:
		artifact_index = {"artifacts": []}
	artifacts = artifact_index.setdefault("artifacts", [])
	entry_key = artifact_entry.get("entry_key")
	if entry_key and any(existing.get("entry_key") == entry_key for existing in artifacts):
		return False
	artifacts.append(artifact_entry)
	artifact_index_path.write_text(json.dumps(artifact_index, default=str, indent=2), encoding="utf-8")
	return True

def getSecret(secretName: str, modelKey: str = ""):
    response = get_secret_value(secret_name=secretName)
    if "SecretString" in response:
        secret = response["SecretString"]
        r = json.loads(secret)
        if modelKey == "":
            return r
        
        secret = json.loads(r[modelKey])
    else:
        import base64
        secret = base64.b64decode(response["SecretBinary"])
        
    return secret

def get_secret_value(secret_name:str):
    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
        region_name="us-east-1"
    )

    return client.get_secret_value(SecretId=secret_name)

def safe_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True)


def default_model_for_provider(provider: str) -> str:
    if provider == "vertex":
        return DEFAULT_VERTEX_MODEL
    return DEFAULT_OPENAI_MODEL


def is_vertex_model_name(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized.startswith("google/") or normalized.startswith("gemini")


def is_openai_model_name(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized.startswith("gpt-") or normalized.startswith("o")


def canonicalize_vertex_model_name(model: str) -> str:
    candidate = model.strip()
    if candidate.lower().startswith("google/"):
        return candidate
    if candidate.lower().startswith("gemini"):
        return f"google/{candidate}"
    return candidate

def resolve_model_name(provider: str, requested_model: Optional[str]) -> str:
    candidate = (requested_model or "").strip()
    if provider == "vertex":
        if candidate and is_vertex_model_name(candidate):
            return canonicalize_vertex_model_name(candidate)

        gemini_model = (os.getenv("GEMINI_MODEL") or "").strip()
        if gemini_model:
            return canonicalize_vertex_model_name(gemini_model)

        env_model = (os.getenv("OPENAI_MODEL") or "").strip()
        if env_model and is_vertex_model_name(env_model):
            return canonicalize_vertex_model_name(env_model)

        return DEFAULT_VERTEX_MODEL

    if candidate and not is_vertex_model_name(candidate):
        return candidate

    env_model = (os.getenv("OPENAI_MODEL") or "").strip()
    if env_model and not is_vertex_model_name(env_model):
        return env_model

    return DEFAULT_OPENAI_MODEL

def build_vertex_base_url(modelConfig: dict[str, str]) -> str:
    base_url = os.getenv("VERTEX_BASE_URL")
    if base_url:
        return base_url

    project_id = modelConfig["vertexprojectid"] # os.getenv("VERTEX_PROJECT_ID")
    if not project_id:
        raise RuntimeError(
            "VERTEX_PROJECT_ID is not set. Set it or provide VERTEX_BASE_URL for Vertex AI."
        )

    location = (os.getenv("VERTEX_LOCATION") or "").strip() or "global"
    return (
        "https://aiplatform.googleapis.com/v1/projects/"
        f"{project_id}/locations/{location}/endpoints/openapi"
    )

def build_vertex_token_provider() -> tuple[str, Optional[Callable[[], str]]]:
    access_token = os.getenv("VERTEX_ACCESS_TOKEN")
    if access_token:
        return access_token, None

    try:
        from google.auth import default
        import google.auth.transport.requests
    except ImportError as exc:
        raise RuntimeError(
            "google-auth is required for Vertex AI when VERTEX_ACCESS_TOKEN is not set."
        ) from exc

    credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    request = google.auth.transport.requests.Request()
    
    def refresh_token() -> str:
        credentials.refresh(request)
        return credentials.token

    return refresh_token(), refresh_token

def resolve_secret(explicit_value: Optional[str], env_name: str) -> Optional[str]:
    if explicit_value is not None:
        value = explicit_value.strip()
        return value or None

    value = os.getenv(env_name)
    if value is None:
        return None

    value = value.strip()
    return value or None

def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_csv(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]


def env_int(name: str, default: Optional[int] = None) -> Optional[int]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)

def utc_now(only_date: bool = False) -> str:
    if only_date:
        return datetime.now(timezone.utc).date().isoformat()
    return datetime.now(timezone.utc).isoformat()
	
def append_worklog_entry(*, workspace_dir: Path, message: str) -> None:
    worklog_path = workspace_dir / "logs" / "WORKLOG.md"
    if worklog_path.exists():
        existing_text = worklog_path.read_text(encoding="utf-8")
        if message in existing_text:
            return
    else:
        worklog_path.mkdir(parents=True, exist_ok=True)

    with worklog_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n- {utc_now()}: {message}")

def parse_json(data_string):
    try:
        parsed_data = json.loads(data_string)
        if isinstance(parsed_data, (dict, list)):
            return json.dumps(parsed_data, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        return None
    return None

def convert_from_json(data_string):
    try:
        result = json.loads(data_string)
        return result
    except:
        return None

def is_stop_requested(key: Path):
    return key.exists()

def delete_stop_request(key: Path):
    if key.exists():
        key.unlink()

def create_stop_request(key: Path):
    key.parent.mkdir(parents=True, exist_ok=True)
    if not key.exists():
        key.touch()

def enqueue_dismiss(key: Path):
    key.parent.mkdir(parents=True, exist_ok=True)
    if not key.exists():
        key.touch()

def remove_dismiss(key: Path):
    if key.exists():
        key.unlink()

def list_pending_dismiss(user_id: str):
    folder = Path(STORAGE_PATH) / "dismiss" / user_id
    result = []
    if folder.exists():
        files = folder.glob("**/*.dismiss")
        for f in files:
            result.append(f.parent.name)

    return result

def _request_file(key: str, folder:str = "stops", name: str = ".stop") -> Path:
    return Path(STORAGE_PATH) / folder / key / name

def generate_stop_key(user_id: str, id: str) -> Path:
    return _request_file(f"{user_id}/{id}")

def generate_dismiss_key(user_id: str, id: str) -> Path:
    return _request_file(f"{user_id}/{id}", folder="dismiss", name=".dismiss")

async def http_post(url: str, headers: dict[str, str] = None, body: Any = None) -> httpx.Response:
    response = await _http_client.post(url=url, json=body, headers=headers)
    response.raise_for_status()
    return response

async def http_get(url: str, headers: dict[str, str] = None) -> httpx.Response:
    try:
        response = await _http_client.get(url=url, headers=headers)
        response.raise_for_status()
        return response
    except Exception as e:
        logger.exception(e)
        raise

async def http_put(url: str, headers: dict[str,str] = None, body: Any = None) -> httpx.Response:
    try:
        response = await _http_client.put(url=url, headers=headers,json=body)
        response.raise_for_status()
        return response
    except Exception as e:
        logger.exception(e)
        raise

def hash_value(value:str) -> str:
    return hashlib.sha256(value.encode('utf8')).hexdigest()

async def close_http_client() -> None:
    await _http_client.aclose()

_openai_clients: dict[tuple[str, str | None], AsyncOpenAI] = {}


async def close_openai_clients() -> None:
    clients = list(_openai_clients.values())
    _openai_clients.clear()

    results = await asyncio.gather(
        *(client.close() for client in clients),
        return_exceptions=True,
    )

    errors = [result for result in results if isinstance(result, Exception)]
    if errors:
        raise errors[0]


def get_openai_model(api_key: str, base_url: str | None, model_name: str):
    key = (api_key, base_url)

    client = _openai_clients.get(key)
    if client is None:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        _openai_clients[key] = client

    return OpenAIResponsesModel(
        model=model_name,
        openai_client=client,
    )

MCP_TIMEOUT = env_int("MCP_TIMEOUT", 60)
MAX_TURNS = env_int("MAX_TURNS", 50)
WARN_BEFORE = env_int("WARN_BEFORE", 5)
MCP_URL = os.getenv("MCP_AUTH_URL")
ENV = os.getenv("ENV", "dev")

MCP_SERVER =os.getenv("MCP_SERVER", "")
async def get_user_preferences(igz_token: str) -> dict[str, dict]:
    if USER_PREFERENCES_API == "" or igz_token == "":
        return {}
    
    url = f"{USER_PREFERENCES_API}/api/Protege/GetUserPreferences"
    response = await http_get(url, headers={"Authorization": f"Bearer {igz_token}", "ContentType": "application/json"})
    response.raise_for_status()
    return response.json()

async def upload_conversation_to_vault(request: ChatRequest, content:str):
    if VAULT_URL:
        url = f"{VAULT_URL}/api/Protege/save"
        body = {
            "trackingId": request.ConversationId,
            "docName": f"conversation_{request.ConversationId}",
            "contentType": "text",
            "content": content
        }

        if ("" or request.AuthCookie).strip() == "":
            response = await http_get(f"{MCP_URL}/api/mcpauth/token", headers={"lna": request.LexisToken})
            request.AuthCookie = response.text.replace("\"", "")
        headers = {"Authorization": f"Bearer {request.AuthCookie}", "ContentType": "application/json"}
        try:
            response = await http_post(url=url, headers=headers, body=body)
        except Exception as e:
            logger.error(f"Error uploading to vault {request.ConversationId} {str(e)}")
            raise

SYSTEM_PROMPT = None
def read_system_prompt():
    global SYSTEM_PROMPT
    if SYSTEM_PROMPT:
        return SYSTEM_PROMPT
    
    path = "src/system_prompts/system_prompt.txt"
    with open(Path(path), 'r', encoding='utf8') as f:
        system_prompt = f.read()
    SYSTEM_PROMPT = system_prompt
    return SYSTEM_PROMPT