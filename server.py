from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import secrets
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - startup message is clearer than an import traceback
    AESGCM = None


APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
QODER_ROOT = Path(os.environ.get("QODER_ROOT", Path.home() / ".qoder"))
STORE_ROOT = Path(os.environ.get("QODER_COCKPIT_STORE", QODER_ROOT / "cockpit-local"))
DATABASE_FILE = STORE_ROOT / "cockpit.db"
BACKUPS_ROOT = STORE_ROOT / "backups"
SECRET_KEYS = (
    "secret://aicoding.auth.userInfo",
    "secret://aicoding.auth.userPlan",
    "secret://aicoding.auth.creditUsage",
)
SESSION_STATE_PREFIXES = (
    "lingma.chat.localHistory.",
    "aicoding-chat-",
    "chat.chatMode.session.",
)
SESSION_STATE_KEYS = frozenset(
    {
        "aicoding.quest.focusedViewState",
        "aicoding.quest.currentOpenTask.ownerAccountId",
        "aicoding.quest.sessionGroups.layout",
        "aicoding.questArchivedTaskList",
        "aicoding.questDeletedTaskTombstones",
        "aicoding.questSelectedWorkspacePath",
        "aicoding.questTaskListCollapseState",
        "aicoding.questTaskListOrganizeState",
        "aicoding.questTaskListSnapshot",
        "aicoding.questWorkspaceHistory",
        "aicoding.questAgentsWindowRecoveryIgnoredFolders",
        "chat.ChatSessionStore.index",
        "chat.workspaceTransfer",
        "quest-current-open-task-id",
        "quest-unread-task-list",
        "workbench.auxiliarybar.pinnedPanels",
        "workbench.auxiliarybar.placeholderPanels",
    }
)
OWNER_BOUND_JSON_KEYS = frozenset(
    {
        "aicoding.quest.sessionGroups.layout",
        "aicoding.questTaskListSnapshot",
    }
)
OAUTH_LOGIN_BASE = "https://qoder.com/device/selectAccounts"
OAUTH_OPENAPI_BASE = "https://openapi.qoder.sh"
OAUTH_CLIENT_ID = "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb"
OAUTH_CHALLENGE_METHOD = "S256"
oauth_lock = threading.Lock()
oauth_state: dict[str, dict[str, Any]] = {}
quota_refresh_stop = threading.Event()
quota_refresh_lock = threading.RLock()
database_lock = threading.RLock()
QUOTA_REFRESH_INTERVAL_SECONDS = 300


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def oauth_urlsafe(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def oauth_json_request(url: str, headers: dict[str, str] | None = None, query: dict[str, str] | None = None) -> dict[str, Any] | None:
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status == 404:
                return None
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"Qoder OAuth 请求失败：HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(f"Qoder OAuth 请求失败：HTTP {exc.code}") from exc


def oauth_machine_id() -> str | None:
    path = Path(os.environ.get("APPDATA", "")) / "Qoder" / "SharedClientCache" / "cache" / "id"
    try:
        value = path.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def oauth_status_headers(token: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    cache = Path(os.environ.get("APPDATA", "")) / "Qoder" / "SharedClientCache" / "cache" / "machine_token.json"
    try:
        machine = json_load(cache, {})
        if machine.get("token"):
            headers["Cosy-MachineToken"] = str(machine["token"])
        if machine.get("type"):
            headers["Cosy-MachineType"] = str(machine["type"])
    except Exception:
        pass
    headers["Cosy-MachineOS"] = "x86_64_windows"
    headers["Cosy-ClientType"] = "0"
    return headers


def oauth_expire_timestamp(value: Any) -> str | None:
    """把 device token 的 ISO/秒级过期时间统一为 Qoder 使用的毫秒字符串。"""
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.isdigit():
        number = int(text)
        return str(number if number > 1_000_000_000_000 else number * 1000)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return str(int(parsed.timestamp() * 1000))
    except ValueError:
        return text


def oauth_build_login_url(nonce: str, challenge: str) -> str:
    query = {
        "nonce": nonce,
        "challenge": challenge,
        "challenge_method": OAUTH_CHALLENGE_METHOD,
        "client_id": OAUTH_CLIENT_ID,
    }
    machine_id = oauth_machine_id()
    if machine_id:
        query["machine_id"] = machine_id
    return f"{OAUTH_LOGIN_BASE}?{urllib.parse.urlencode(query)}"


def oauth_fetch_account(state: dict[str, Any], token_data: dict[str, Any]) -> dict[str, Any]:
    token = str(token_data.get("token") or "").strip()
    if not token:
        raise RuntimeError("Qoder 授权响应缺少 token。")
    # 授权完成后立即读取用户、套餐和额度，保存为可复用的本地账号快照。
    headers = oauth_status_headers(token)
    user_info = oauth_json_request(f"{OAUTH_OPENAPI_BASE}/api/v1/userinfo", headers) or {}
    user_status = oauth_json_request(f"{OAUTH_OPENAPI_BASE}/api/v3/user/status", headers) or {}
    if user_status.get("whitelistStatus") in {"NoIpPermission", "AppDisable", "LoginExpire", "NotAllow", "NOT_ALLOW"}:
        raise RuntimeError(f"Qoder 账号当前不可用：{user_status.get('whitelistStatus')}")
    info = {
        "id": token_data.get("user_id") or user_info.get("id") or user_status.get("id"),
        "token": token,
        "refreshToken": token_data.get("refresh_token"),
        "expireTime": oauth_expire_timestamp(token_data.get("expires_at")),
        "refreshTokenExpireTime": oauth_expire_timestamp(token_data.get("refresh_token_expires_at")),
        **user_info,
    }
    info.update({key: value for key, value in user_status.items() if value is not None})
    # Qoder 客户端同时读取 token 与 securityOauthToken；两者必须指向同一个 access token。
    info["token"] = token
    info["securityOauthToken"] = token
    plan = oauth_json_request(f"{OAUTH_OPENAPI_BASE}/api/v2/user/plan", headers) or {}
    usage = oauth_json_request(f"{OAUTH_OPENAPI_BASE}/api/v2/quota/usage", headers) or {}
    return merge_account(normalize_account({SECRET_KEYS[0]: info, SECRET_KEYS[1]: plan, SECRET_KEYS[2]: usage}))


def refresh_account_quota(account: dict[str, Any]) -> dict[str, Any]:
    """使用已授权 access token 更新单个账号的状态、套餐和额度快照。"""
    values = account.get("values") if isinstance(account.get("values"), dict) else {}
    info = dict(values.get(SECRET_KEYS[0]) or {})
    token = str(info.get("token") or info.get("securityOauthToken") or "").strip()
    if not token:
        raise RuntimeError("账号缺少 access token，请重新授权。")
    headers = oauth_status_headers(token)
    status = oauth_json_request(f"{OAUTH_OPENAPI_BASE}/api/v3/user/status", headers) or {}
    if status.get("whitelistStatus") in {"NoIpPermission", "AppDisable", "LoginExpire", "NotAllow", "NOT_ALLOW"}:
        raise RuntimeError(f"账号当前不可用：{status.get('whitelistStatus')}")
    plan = oauth_json_request(f"{OAUTH_OPENAPI_BASE}/api/v2/user/plan", headers) or {}
    usage = oauth_json_request(f"{OAUTH_OPENAPI_BASE}/api/v2/quota/usage", headers) or {}
    info.update({key: value for key, value in status.items() if value is not None})
    info["token"] = token
    info["securityOauthToken"] = token
    values = {**values, SECRET_KEYS[0]: info, SECRET_KEYS[1]: plan, SECRET_KEYS[2]: usage}
    refreshed = normalize_account(values, existing_id=account.get("id"))
    refreshed["created_at"] = account.get("created_at", refreshed["updated_at"])
    refreshed["quota_refreshed_at"] = now_iso()
    refreshed["quota_error"] = None
    return refreshed


def refresh_all_quotas(account_id: str | None = None) -> dict[str, Any]:
    """批量刷新账号额度；单个账号失败不会阻塞其它账号更新。"""
    with quota_refresh_lock:
        accounts = load_accounts()
        refreshed_accounts: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        attempted = 0
        # 每个账号独立刷新并保留失败信息，避免某个过期 token 影响其它账号的额度同步。
        for account in accounts:
            if account_id and account.get("id") != account_id:
                refreshed_accounts.append(account)
                continue
            attempted += 1
            try:
                refreshed_accounts.append(refresh_account_quota(account))
            except Exception as exc:
                failed = dict(account)
                failed["quota_error"] = str(exc)
                failed["quota_refreshed_at"] = now_iso()
                refreshed_accounts.append(failed)
                errors.append({"account_id": str(account.get("id", "")), "error": str(exc)})
        save_accounts(refreshed_accounts)
        return {"updated": attempted - len(errors), "errors": errors, "refreshed_at": now_iso()}


def quota_refresh_loop() -> None:
    """后台定时刷新额度，页面无需保持打开也能让本地账号快照保持新鲜。"""
    quota_refresh_stop.wait(8)
    while not quota_refresh_stop.is_set():
        try:
            refresh_all_quotas()
        except Exception:
            pass
        quota_refresh_stop.wait(QUOTA_REFRESH_INTERVAL_SECONDS)


def oauth_worker(login_id: str) -> None:
    with oauth_lock:
        state = oauth_state.get(login_id)
    if not state:
        return
    try:
        deadline = time.time() + 600
        # Device Authorization 没有回调地址，本地服务按短间隔轮询直到用户完成授权或超时。
        while time.time() < deadline:
            with oauth_lock:
                if oauth_state.get(login_id, {}).get("cancelled"):
                    raise RuntimeError("Qoder 授权已取消。")
            payload = oauth_json_request(
                f"{OAUTH_OPENAPI_BASE}/api/v1/deviceToken/poll",
                query={"nonce": state["nonce"], "verifier": state["verifier"], "challenge_method": OAUTH_CHALLENGE_METHOD},
            )
            if payload and payload.get("token"):
                account = oauth_fetch_account(state, payload)
                with oauth_lock:
                    oauth_state[login_id].update({"status": "completed", "account": public_account(account)})
                return
            time.sleep(1)
        raise RuntimeError("Qoder 授权已超时，请重新发起。")
    except Exception as exc:
        with oauth_lock:
            if login_id in oauth_state:
                oauth_state[login_id].update({"status": "error", "error": str(exc)})


def json_load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def json_save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def qoder_state_db() -> Path | None:
    candidates = [
        QODER_ROOT / "app" / "User" / "globalStorage" / "state.vscdb",
        Path(os.environ.get("APPDATA", "")) / "Qoder" / "User" / "globalStorage" / "state.vscdb",
        Path(os.environ.get("APPDATA", "")) / "Qoder" / "globalStorage" / "state.vscdb",
    ]
    return next((path for path in candidates if path.is_file()), None)


def project_root() -> Path:
    return QODER_ROOT / "projects"


def qoder_user_root() -> Path:
    """返回包含 globalStorage 与 workspaceStorage 的 Qoder 用户数据目录。"""
    state_db = qoder_state_db()
    if state_db is not None:
        return state_db.parent.parent
    return Path(os.environ.get("APPDATA", "")) / "Qoder" / "User"


def workspace_storage_root() -> Path:
    return qoder_user_root() / "workspaceStorage"


def qoder_executable() -> Path | None:
    configured = os.environ.get("QODER_APP_PATH")
    candidates = [
        Path(configured) if configured else None,
        Path(os.environ.get("ProgramFiles", "")) / "Qoder IDE" / "Qoder.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Qoder IDE" / "Qoder IDE.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Qoder IDE" / "resources" / "app" / "resources" / "bin" / "x86_64_windows" / "Qoder.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Qoder" / "Qoder.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Qoder" / "Qoder.exe",
    ]
    return next((path for path in candidates if path and path.is_file()), None)


def is_qoder_running() -> bool:
    if os.name == "nt":
        # Qoder IDE.exe 是桌面主进程，Qoder.exe 是其本地会话服务；任一存活都说明会话库仍可能被占用。
        for image_name in ("Qoder IDE.exe", "Qoder.exe"):
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            if image_name.lower() in result.stdout.lower():
                return True
        return False
    result = subprocess.run(["pgrep", "-f", "[Q]oder"], capture_output=True, text=True, check=False)
    return bool(result.stdout.strip())


def stop_qoder(timeout: float = 20.0) -> bool:
    """请求 Qoder 正常退出，确保 state.vscdb 和会话文件不被进程继续占用。"""
    if not is_qoder_running():
        return False
    if os.name == "nt":
        # 先向桌面窗口发送正常关闭消息，让 Qoder 落盘 SQLite/WAL；超时后再结束完整 IDE 进程树。
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process -Name 'Qoder IDE' -ErrorAction SilentlyContinue | "
                "Where-Object { $_.MainWindowHandle -ne 0 } | "
                "ForEach-Object { [void]$_.CloseMainWindow() }",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        graceful_deadline = time.time() + min(8.0, timeout)
        while time.time() < graceful_deadline and is_qoder_running():
            time.sleep(0.25)
        if is_qoder_running():
            for image_name in ("Qoder IDE.exe", "Qoder.exe"):
                subprocess.run(
                    ["taskkill", "/IM", image_name, "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
    else:
        subprocess.run(["pkill", "-TERM", "-f", "[Q]oder"], capture_output=True, check=False)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_qoder_running():
            return True
        time.sleep(0.25)
    raise RuntimeError("Qoder 未能在规定时间内退出，请先手动关闭后重试。")


def launch_qoder() -> Path:
    executable = qoder_executable()
    if executable is None:
        raise RuntimeError("找不到 Qoder.exe。请设置 QODER_APP_PATH 环境变量后重试。")
    subprocess.Popen([str(executable), "--reuse-window"], close_fds=True)
    return executable


def require_qoder_stopped(allow_running: bool = False) -> None:
    if not allow_running and is_qoder_running():
        raise RuntimeError("Qoder 正在运行。请先关闭 Qoder，再执行账号注入或会话同步。")


def db_connect(path: Path, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        return sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True)
    return sqlite3.connect(path)


def dpapi_unprotect(blob: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Qoder 凭据解密目前只实现了 Windows DPAPI。")

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    source = (ctypes.c_byte * len(blob)).from_buffer_copy(blob)
    input_blob = DataBlob(len(blob), source)
    output_blob = DataBlob()
    crypt_unprotect = ctypes.windll.crypt32.CryptUnprotectData
    crypt_unprotect.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_wchar_p, ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.POINTER(DataBlob)]
    crypt_unprotect.restype = ctypes.wintypes.BOOL
    if not crypt_unprotect(ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def windows_master_key() -> bytes:
    local_state = Path(os.environ.get("APPDATA", "")) / "Qoder" / "Local State"
    document = json_load(local_state, {})
    encoded = document.get("os_crypt", {}).get("encrypted_key")
    if not encoded:
        raise RuntimeError(f"未找到 Qoder Local State 加密密钥：{local_state}")
    encrypted = __import__("base64").b64decode(encoded)
    if encrypted.startswith(b"DPAPI"):
        encrypted = encrypted[5:]
    return dpapi_unprotect(encrypted)


def decode_secret_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        envelope = json.loads(value)
        if envelope.get("type") != "Buffer" or not isinstance(envelope.get("data"), list):
            return json.loads(value)
        encrypted = bytes(envelope["data"])
        if not encrypted.startswith(b"v10"):
            return value
        if AESGCM is None:
            raise RuntimeError("缺少 cryptography 依赖，无法解密 Qoder state.vscdb。请执行 pip install cryptography。")
        plaintext = AESGCM(windows_master_key()).decrypt(encrypted[3:15], encrypted[15:], None)
        return json.loads(plaintext.decode("utf-8"))
    except (json.JSONDecodeError, ValueError, KeyError):
        return value


def encode_secret_value(value: Any) -> str:
    if AESGCM is None:
        raise RuntimeError("缺少 cryptography 依赖，无法写入 Qoder state.vscdb。请执行 pip install cryptography。")
    nonce = os.urandom(12)
    encrypted = AESGCM(windows_master_key()).encrypt(nonce, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), None)
    envelope = {"type": "Buffer", "data": list(b"v10" + nonce + encrypted)}
    return json.dumps(envelope, separators=(",", ":"))


def read_state_values(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    db: sqlite3.Connection | None = None
    try:
        db = db_connect(path, True)
        rows = db.execute("SELECT key, value FROM ItemTable WHERE key IN (?, ?, ?)", SECRET_KEYS).fetchall()
        values: dict[str, Any] = {}
        for key, value in rows:
            values[key] = decode_secret_value(value)
        return values
    except sqlite3.Error:
        return {}
    finally:
        if db is not None:
            db.close()


def write_state_values(path: Path, values: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = db_connect(path)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY ON CONFLICT REPLACE, value BLOB)")
        for key in SECRET_KEYS:
            if key in values:
                encoded = encode_secret_value(values[key])
                db.execute("INSERT OR REPLACE INTO ItemTable(key, value) VALUES (?, ?)", (key, encoded))
        db.commit()
    finally:
        db.close()


def qoder_account_owner(values: dict[str, Any]) -> str:
    """生成 Qoder 用于隔离不同账号 Quest 状态的内部 owner 标识。"""
    user_info = values.get("secret://aicoding.auth.userInfo")
    if not isinstance(user_info, dict):
        raise RuntimeError("目标账号缺少 Qoder 用户信息，无法继承会话。")
    user_id = str(user_info.get("id") or "").strip()
    org_id = str(user_info.get("orgId") or user_info.get("organization_id") or "").strip()
    if not user_id:
        raise RuntimeError("目标账号缺少用户 ID，无法继承会话。")
    return hashlib.sha1(f"{user_id}\0{org_id}".encode("utf-8")).hexdigest()


def qoder_account_identity(values: dict[str, Any]) -> dict[str, str]:
    """提取 Qoder 本地会话库用于隔离账号的用户、组织和显示名称。"""
    user_info = values.get("secret://aicoding.auth.userInfo")
    if not isinstance(user_info, dict):
        raise RuntimeError("账号缺少 Qoder 用户信息，无法同步本地历史会话。")
    user_id = str(user_info.get("id") or user_info.get("userId") or user_info.get("user_id") or "").strip()
    org_id = str(user_info.get("orgId") or user_info.get("organization_id") or "").strip()
    display_name = str(user_info.get("displayName") or user_info.get("name") or user_info.get("nickname") or "").strip()
    if not user_id:
        raise RuntimeError("账号缺少用户 ID，无法同步本地历史会话。")
    return {"user_id": user_id, "org_id": org_id, "display_name": display_name}


def is_session_state_key(key: str) -> bool:
    return key in SESSION_STATE_KEYS or key.startswith(SESSION_STATE_PREFIXES)


def read_session_state_rows(path: Path) -> dict[str, Any]:
    db = db_connect(path, True)
    try:
        rows = db.execute("SELECT key, value FROM ItemTable").fetchall()
        return {key: value for key, value in rows if is_session_state_key(key)}
    finally:
        db.close()


def json_storage_value(value: Any, update: dict[str, Any]) -> Any:
    """修改 Qoder JSON 状态值，同时保留 SQLite 中原来的 TEXT/BLOB 类型。"""
    is_bytes = isinstance(value, bytes)
    text = value.decode("utf-8") if is_bytes else str(value)
    document = json.loads(text)
    if not isinstance(document, dict):
        raise RuntimeError("Qoder 会话状态格式异常，无法修改账号归属。")
    document.update(update)
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    return encoded.encode("utf-8") if is_bytes else encoded


def rebind_session_state_rows(rows: dict[str, Any], target_values: dict[str, Any]) -> dict[str, Any]:
    owner = qoder_account_owner(target_values)
    rebound = dict(rows)
    # Qoder 只加载 ownerAccountId 与当前登录账号一致的 Quest 快照和标签页布局。
    for key in OWNER_BOUND_JSON_KEYS:
        if key in rebound:
            rebound[key] = json_storage_value(rebound[key], {"ownerAccountId": owner})
    if "aicoding.quest.currentOpenTask.ownerAccountId" in rebound:
        current = rebound["aicoding.quest.currentOpenTask.ownerAccountId"]
        rebound["aicoding.quest.currentOpenTask.ownerAccountId"] = owner.encode("utf-8") if isinstance(current, bytes) else owner
    return rebound


def write_session_state_rows(path: Path, rows: dict[str, Any], target_values: dict[str, Any]) -> int:
    rebound = rebind_session_state_rows(rows, target_values)
    db = db_connect(path)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY ON CONFLICT REPLACE, value BLOB)")
        existing = [key for (key,) in db.execute("SELECT key FROM ItemTable").fetchall() if is_session_state_key(key)]
        # 继承时以来源账号快照为准，避免目标账号残留的索引指向不存在的会话文件。
        db.executemany("DELETE FROM ItemTable WHERE key = ?", ((key,) for key in existing))
        db.executemany("INSERT OR REPLACE INTO ItemTable(key, value) VALUES (?, ?)", rebound.items())
        db.commit()
        return len(rebound)
    finally:
        db.close()


def snapshot_session_state(account_id: str, state_db: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = STORE_ROOT / "sessions" / safe_name(account_id) / "_state" / f"{stamp}.vscdb"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(state_db, destination)
    return destination


def state_account_id(path: Path) -> str | None:
    values = read_state_values(path)
    if not values:
        return None
    return normalize_account(values).get("id")


def latest_session_state(account_id: str) -> Path | None:
    account_root = STORE_ROOT / "sessions" / safe_name(account_id) / "_state"
    candidates = list(account_root.glob("*.vscdb")) if account_root.exists() else []
    backup_root = BACKUPS_ROOT / "state"
    candidates.extend(backup_root.glob("*.vscdb") if backup_root.exists() else [])
    # 旧版本只做了全库安全备份；读取其中的账号身份后，也可作为首次修复的来源快照。
    matching = []
    for candidate in candidates:
        try:
            if state_account_id(candidate) == account_id:
                matching.append(candidate)
        except (OSError, sqlite3.Error, RuntimeError):
            continue
    return max(matching, key=lambda item: item.stat().st_mtime_ns) if matching else None


def qoder_session_db() -> Path | None:
    """定位 Qoder/Cosy 保存真实 Chat 与 Quest 索引的本地 SQLite 数据库。"""
    configured = os.environ.get("QODER_SESSION_DB")
    candidates = [
        Path(configured) if configured else None,
        Path(os.environ.get("APPDATA", "")) / "Qoder" / "SharedClientCache" / "cache" / "db" / "local.db",
        QODER_ROOT / "SharedClientCache" / "cache" / "db" / "local.db",
    ]
    return next((path for path in candidates if path and path.is_file()), None)


def backup_session_database(path: Path) -> Path:
    """用 SQLite 在线备份生成包含 WAL 内容的完整会话库副本。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = BACKUPS_ROOT / "session-db" / stamp / path.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_db = db_connect(path, True)
    backup_db = sqlite3.connect(destination)
    try:
        source_db.backup(backup_db, pages=16384)
    finally:
        backup_db.close()
        source_db.close()
    return destination


def sqlite_table_columns(db: sqlite3.Connection, table: str) -> set[str] | None:
    """读取 Qoder 某张表的字段；未安装对应功能时返回 None。"""
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if not exists:
        return None
    return {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}


def validate_local_session_database(path: Path) -> None:
    """切号前确认基础 Chat 会话表可迁移，避免写入账号后才发现数据库不兼容。"""
    db = db_connect(path, True)
    try:
        columns = sqlite_table_columns(db, "chat_session")
        missing = {"session_id", "user_id"} - (columns or set())
        if missing:
            names = "、".join(sorted(missing))
            raise RuntimeError(f"Qoder local.db 缺少 chat_session 必需字段：{names}")
    finally:
        db.close()


def rebind_local_sessions(path: Path, source_values: dict[str, Any], target_values: dict[str, Any]) -> dict[str, Any]:
    """把来源账号的本地 Chat 会话及已安装扩展表归属迁移到目标账号。"""
    source = qoder_account_identity(source_values)
    target = qoder_account_identity(target_values)
    if source["user_id"] == target["user_id"]:
        return {"session_count": 0, "backup": None, "tables": {}, "skipped_tables": {}}

    read_db = db_connect(path, True)
    try:
        chat_columns = sqlite_table_columns(read_db, "chat_session")
        if not chat_columns or not {"session_id", "user_id"}.issubset(chat_columns):
            raise RuntimeError("Qoder local.db 的 chat_session 表不兼容，无法同步 IDE 历史会话。")
        session_ids = [row[0] for row in read_db.execute("SELECT session_id FROM chat_session WHERE user_id = ?", (source["user_id"],)).fetchall()]
    finally:
        read_db.close()
    if not session_ids:
        return {"session_count": 0, "backup": None, "tables": {}, "skipped_tables": {}}

    # local.db 保存完整会话正文和索引，写入任何归属字段前都先生成可独立恢复的全库备份。
    backup = backup_session_database(path)
    db = sqlite3.connect(path, timeout=30)
    counts: dict[str, int] = {}
    skipped_tables: dict[str, str] = {}
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TEMP TABLE cockpit_moved_sessions (session_id TEXT PRIMARY KEY)")
        db.executemany("INSERT INTO cockpit_moved_sessions(session_id) VALUES (?)", ((item,) for item in session_ids))

        # 基础 Qoder IDE 只保证 user_id；显示名和组织字段存在时再同步，兼容不同版本表结构。
        chat_columns = sqlite_table_columns(db, "chat_session") or set()
        assignments = ["user_id = ?"]
        parameters = [target["user_id"]]
        for column, value in (("user_name", target["display_name"]), ("org_id", target["org_id"])):
            if column in chat_columns:
                assignments.append(f"{column} = ?")
                parameters.append(value)
        parameters.append(source["user_id"])
        cursor = db.execute(
            f"UPDATE chat_session SET {', '.join(assignments)} WHERE user_id = ?",
            parameters,
        )
        counts["chat_session"] = cursor.rowcount

        # Quest/Agent 扩展表不是 Qoder IDE 必装组件；仅迁移当前版本实际存在且字段完整的表。
        for table in ("local_execution", "agent_memory"):
            columns = sqlite_table_columns(db, table)
            required = {"session_id", "user_id"}
            if columns is None:
                skipped_tables[table] = "未安装对应扩展组件"
                continue
            if not required.issubset(columns):
                skipped_tables[table] = "缺少 session_id 或 user_id 字段"
                continue
            cursor = db.execute(
                f"UPDATE {table} SET user_id = ? WHERE user_id = ? AND session_id IN (SELECT session_id FROM cockpit_moved_sessions)",
                (target["user_id"], source["user_id"]),
            )
            counts[table] = cursor.rowcount

        # task 同步元数据使用 userId::orgId 作为账号键，只迁移本批 Quest，避免覆盖目标账号的全局同步游标。
        sync_columns = sqlite_table_columns(db, "sync_metadata")
        if sync_columns is None:
            skipped_tables["sync_metadata"] = "未安装任务同步组件"
        elif not {"user_id", "entity_type", "entity_id"}.issubset(sync_columns):
            skipped_tables["sync_metadata"] = "缺少任务同步必需字段"
        else:
            source_sync_id = f"{source['user_id']}::{source['org_id']}" if source["org_id"] else source["user_id"]
            target_sync_id = f"{target['user_id']}::{target['org_id']}" if target["org_id"] else target["user_id"]
            cursor = db.execute(
                "UPDATE sync_metadata SET user_id = ? WHERE entity_type = 'task' AND user_id IN (?, ?) "
                "AND entity_id IN (SELECT session_id FROM cockpit_moved_sessions)",
                (target_sync_id, source["user_id"], source_sync_id),
            )
            counts["sync_metadata"] = cursor.rowcount
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {
        "session_count": len(session_ids),
        "backup": str(backup),
        "tables": counts,
        "skipped_tables": skipped_tables,
    }


def extract_email(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("email", "mail", "userEmail", "loginEmail"):
            if isinstance(value.get(key), str) and "@" in value[key]:
                return value[key].lower()
        for child in value.values():
            found = extract_email(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = extract_email(child)
            if found:
                return found
    if isinstance(value, str) and "@" in value:
        return value.lower()
    return None


def extract_text(value: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), (str, int, float)):
                return str(value[key])
        for child in value.values():
            found = extract_text(child, keys)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = extract_text(child, keys)
            if found:
                return found
    return None


def normalize_account(values: dict[str, Any], existing_id: str | None = None) -> dict[str, Any]:
    info = values.get(SECRET_KEYS[0], {})
    plan = values.get(SECRET_KEYS[1], {})
    usage = values.get(SECRET_KEYS[2], {})
    email = extract_email(info) or extract_email(plan) or "unknown@qoder.local"
    user_id = extract_text(info, ("userId", "user_id", "id"))
    account_id = existing_id or (f"qoder_uid_{user_id}" if user_id else f"qoder_email_{email.replace('@', '_at_')}")
    credits_used = extract_text(usage, ("used", "creditsUsed", "usage"))
    credits_total = extract_text(usage, ("total", "creditsTotal", "limit"))
    credits_remaining = extract_text(usage, ("remaining", "creditsRemaining", "balance"))
    try:
        total_number = float(credits_total) if credits_total is not None else 0
        remaining_number = float(credits_remaining) if credits_remaining is not None else 0
        credits_remaining_percent = round(max(0, min(100, remaining_number / total_number * 100)), 1) if total_number > 0 else None
    except (TypeError, ValueError):
        credits_remaining_percent = None
    return {
        "id": account_id,
        "email": email,
        "user_id": user_id,
        "display_name": extract_text(info, ("displayName", "name", "nickname")),
        "plan": extract_text(plan, ("plan_tier_name", "planType", "plan", "name")),
        "credits_used": credits_used,
        "credits_total": credits_total,
        "credits_remaining": credits_remaining,
        "credits_remaining_percent": credits_remaining_percent,
        "updated_at": now_iso(),
        "values": values,
    }


def init_database() -> None:
    STORE_ROOT.mkdir(parents=True, exist_ok=True)
    with database_lock:
        with sqlite3.connect(DATABASE_FILE) as db:
            db.execute("CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, email TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)")
            db.commit()


def load_accounts() -> list[dict[str, Any]]:
    with database_lock:
        init_database()
        with sqlite3.connect(DATABASE_FILE) as db:
            rows = db.execute("SELECT payload FROM accounts ORDER BY updated_at DESC").fetchall()
        accounts = [json.loads(row[0]) for row in rows]
        changed = False
        # 旧授权快照在读取时补齐 Qoder 需要的双 token 字段和毫秒级过期时间。
        for account in accounts:
            values = account.get("values")
            info = values.get(SECRET_KEYS[0]) if isinstance(values, dict) else None
            if not isinstance(info, dict):
                continue
            token = info.get("token") or info.get("securityOauthToken")
            if token and not info.get("securityOauthToken"):
                info["securityOauthToken"] = token
                changed = True
            for key in ("expireTime", "refreshTokenExpireTime"):
                normalized = oauth_expire_timestamp(info.get(key))
                if normalized and normalized != str(info.get(key)):
                    info[key] = normalized
                    changed = True
        if changed:
            save_accounts(accounts)
        return accounts


def save_accounts(accounts: list[dict[str, Any]]) -> None:
    with database_lock:
        init_database()
        with sqlite3.connect(DATABASE_FILE) as db:
            db.execute("DELETE FROM accounts")
            db.executemany(
                "INSERT INTO accounts(id, email, payload, updated_at) VALUES (?, ?, ?, ?)",
                [(item["id"], item.get("email", ""), json.dumps(item, ensure_ascii=False), item.get("updated_at", now_iso())) for item in accounts],
            )
            db.commit()


def merge_account(account: dict[str, Any]) -> dict[str, Any]:
    with quota_refresh_lock:
        accounts = load_accounts()
        accounts = [item for item in accounts if item.get("id") != account["id"]]
        accounts.insert(0, account)
        save_accounts(accounts)
        return account


def safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    return cleaned[:100] or "project"


def project_key(path: Path) -> str:
    digest = hashlib.sha256(str(path).lower().encode("utf-8")).hexdigest()[:12]
    return f"{safe_name(path.name)}_{digest}"


def copy_contents(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        destination = target / child.name
        if child.is_dir():
            shutil.copytree(child, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(child, destination)


def copy_session_contents(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    # 项目目录只同步会话正文与执行记录；Quest 列表索引由 state.vscdb 的账号快照单独处理。
    transcript = source / "transcript"
    if transcript.is_dir():
        shutil.copytree(transcript, target / "transcript", dirs_exist_ok=True)
    for entry in source.glob("*.session.execution"):
        destination = target / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, destination)


def remove_session_contents(project: Path) -> None:
    transcript = project / "transcript"
    if transcript.exists():
        shutil.rmtree(transcript)
    for entry in project.glob("*.session.execution"):
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def snapshot_project(account_id: str, project: Path) -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = STORE_ROOT / "sessions" / safe_name(account_id) / project_key(project) / stamp
    copy_session_contents(project, destination)
    metadata = {"account_id": account_id, "project": str(project), "captured_at": now_iso(), "snapshot": stamp}
    json_save(destination / "metadata.json", metadata)
    return metadata


def latest_snapshot(account_id: str, project: Path) -> Path | None:
    root = STORE_ROOT / "sessions" / safe_name(account_id) / project_key(project)
    snapshots = sorted((item for item in root.iterdir() if item.is_dir()), reverse=True) if root.exists() else []
    return snapshots[0] if snapshots else None


def restore_project(account_id: str, project: Path) -> dict[str, Any]:
    source = latest_snapshot(account_id, project)
    if source is None:
        raise RuntimeError(f"账号 {account_id} 没有项目 {project.name} 的会话快照。")
    backup = BACKUPS_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") / project_key(project)
    copy_session_contents(project, backup)
    staging = STORE_ROOT / "staging" / uuid4().hex
    copy_session_contents(source, staging)
    metadata = staging / "metadata.json"
    if metadata.exists():
        metadata.unlink()
    remove_session_contents(project)
    copy_session_contents(staging, project)
    shutil.rmtree(staging, ignore_errors=True)
    return {"source": str(source), "backup": str(backup)}


def project_records() -> list[dict[str, Any]]:
    root = project_root()
    root.mkdir(parents=True, exist_ok=True)
    records = []
    for project in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name.lower()):
        transcript = project / "transcript"
        execution_count = len(list(project.glob("*.session.execution")))
        transcript_count = len(list(transcript.glob("*.jsonl"))) if transcript.exists() else 0
        records.append({"name": project.name, "path": str(project), "transcript_count": transcript_count, "execution_count": execution_count})
    return records


def validate_workspace_id(workspace_id: str) -> str:
    """限制工作区 ID 为 Qoder 实际使用的目录/键名字符，阻断路径穿越。"""
    value = workspace_id.strip()
    if not value or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in value):
        raise RuntimeError("工作区 ID 非法。")
    return value


def chat_history_key(workspace_id: str) -> str:
    return f"lingma.chat.localHistory.{validate_workspace_id(workspace_id)}"


def decode_workspace_path(document: dict[str, Any]) -> str:
    """把 workspace.json 中的 file URI 转为适合展示的本机路径。"""
    uri = str(document.get("folder") or document.get("workspace") or "")
    if not uri:
        return ""
    parsed = urllib.parse.urlparse(uri)
    path = urllib.parse.unquote(parsed.path if parsed.scheme == "file" else uri)
    if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path.replace("/", os.sep)


def normalized_workspace_path(value: str) -> str:
    return str(value or "").replace("\\", "/").rstrip("/").lower()


def workspace_path_for_id(workspace_id: str) -> str:
    directory = workspace_storage_root() / validate_workspace_id(workspace_id)
    document = json_load(directory / "workspace.json", {})
    return decode_workspace_path(document)


def read_item_json(db_path: Path, key: str, default: Any) -> Any:
    if not db_path.is_file():
        return default
    db = db_connect(db_path, True)
    try:
        try:
            row = db.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return default
    finally:
        db.close()
    if row is None:
        return default
    value = row[0].decode("utf-8") if isinstance(row[0], bytes) else str(row[0])
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def write_item_json(db: sqlite3.Connection, key: str, value: Any) -> None:
    db.execute(
        "INSERT OR REPLACE INTO ItemTable(key, value) VALUES (?, ?)",
        (key, json.dumps(value, ensure_ascii=False, separators=(",", ":"))),
    )


def workspace_state_db(workspace_id: str) -> Path:
    root = workspace_storage_root().resolve()
    path = (root / validate_workspace_id(workspace_id) / "state.vscdb").resolve()
    if root not in path.parents:
        raise RuntimeError("工作区路径越界。")
    return path


def chat_workspace_records() -> list[dict[str, Any]]:
    """合并工作区目录与全局历史键，保留仅剩历史记录的工作区。"""
    records: dict[str, dict[str, Any]] = {}
    root = workspace_storage_root()
    if root.is_dir():
        for directory in root.iterdir():
            if not directory.is_dir():
                continue
            workspace_id = directory.name
            path = decode_workspace_path(json_load(directory / "workspace.json", {}))
            name = Path(path).stem if path else workspace_id
            records[workspace_id] = {"id": workspace_id, "name": name, "path": path}

    state_db = qoder_state_db()
    if state_db is not None:
        db = db_connect(state_db, True)
        try:
            rows = db.execute(
                "SELECT key FROM ItemTable WHERE key LIKE 'lingma.chat.localHistory.%'"
            ).fetchall()
        finally:
            db.close()
        prefix = "lingma.chat.localHistory."
        for (key,) in rows:
            workspace_id = str(key).removeprefix(prefix)
            records.setdefault(workspace_id, {"id": workspace_id, "name": workspace_id, "path": ""})

    for record in records.values():
        record["chat_count"] = len(chat_records(record["id"]))
    return sorted(records.values(), key=lambda item: (str(item["name"]).lower(), item["id"]))


def chat_sort_timestamp(value: Any) -> float:
    """把 IDE/Quest 可能使用的数字或 ISO 时间统一转换为毫秒时间戳。"""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        try:
            number = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp() * 1000
            except ValueError:
                return 0.0
    # Qoder 历史通常使用毫秒，部分 Quest 数据可能使用秒；排序前统一单位避免两类记录错位。
    return number * 1000 if abs(number) < 100_000_000_000 else number


def chat_records(workspace_id: str) -> list[dict[str, Any]]:
    state_db = qoder_state_db()
    if state_db is None:
        return []
    value = read_item_json(state_db, chat_history_key(workspace_id), [])
    records = value if isinstance(value, list) else []
    workspace_path = normalized_workspace_path(workspace_path_for_id(workspace_id))
    quest_snapshot = read_item_json(state_db, "aicoding.questTaskListSnapshot", {})
    if isinstance(quest_snapshot, dict):
        for folder_path, folder in (quest_snapshot.get("folders") or {}).items():
            if not isinstance(folder, dict) or not workspace_path or normalized_workspace_path(folder_path) != workspace_path:
                continue
            for task in folder.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                task_id = str(task.get("id") or "").strip()
                if not task_id:
                    continue
                records.append({
                    "id": task_id,
                    "title": task.get("title") or task.get("name") or task.get("query") or "无标题 Quest",
                    "context": [],
                    "timestamp": task.get("updatedAtTimestamp") or task.get("updatedAt") or task.get("createTime") or 0,
                    "sessionId": task.get("executionSessionId") or task.get("designSessionId") or "",
                    "kind": "quest",
                    "status": task.get("status") or "",
                    "quest_type": task.get("questType") or "",
                })
    for record in records:
        record.setdefault("kind", "chat")
    # IDE 历史先读、Quest 后追加只是数据合并顺序，展示顺序必须按最近活动时间统一倒序。
    return sorted(records, key=lambda item: chat_sort_timestamp(item.get("timestamp")), reverse=True)


def chat_backup_document(workspace_id: str) -> dict[str, Any]:
    workspace_id = validate_workspace_id(workspace_id)
    workspace = next((item for item in chat_workspace_records() if item["id"] == workspace_id), None)
    state_db = workspace_state_db(workspace_id)
    records = chat_records(workspace_id)
    return {
        "format": "qoder-cockpit-chat-backup-v1",
        "backup_time": now_iso(),
        "workspace_id": workspace_id,
        "workspace_path": workspace.get("path", "") if workspace else "",
        "chat_history": [item for item in records if item.get("kind") != "quest"],
        "quest_tasks": [item for item in records if item.get("kind") == "quest"],
        "chat_views": read_item_json(state_db, "aicoding.chat.views", []),
        "chat_tabs": read_item_json(state_db, "aicoding.chat.tabs", {"activeTabId": "", "tabs": []}),
    }


def save_chat_backup(workspace_id: str) -> Path:
    document = chat_backup_document(workspace_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = BACKUPS_ROOT / "chat" / f"{safe_name(workspace_id)}-{stamp}.json"
    json_save(destination, document)
    return destination


def save_all_chat_backups() -> Path:
    documents = [chat_backup_document(item["id"]) for item in chat_workspace_records()]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = BACKUPS_ROOT / "chat" / f"all-workspaces-{stamp}.json"
    json_save(destination, documents)
    return destination


def chat_backup_records() -> list[dict[str, Any]]:
    root = BACKUPS_ROOT / "chat"
    records = []
    # 备份目录中的单工作区和全量文件统一转换为列表项，供前端按时间查看与选择恢复范围。
    for path in root.glob("*.json") if root.is_dir() else []:
        value = json_load(path, None)
        documents = value if isinstance(value, list) else [value]
        documents = [item for item in documents if isinstance(item, dict)]
        if not documents:
            continue
        records.append(
            {
                "name": path.name,
                "backup_time": documents[0].get("backup_time", ""),
                "workspace": documents[0].get("workspace_path") or documents[0].get("workspace_id", ""),
                "workspace_id": documents[0].get("workspace_id", "") if len(documents) == 1 else "",
                "workspace_ids": [str(item.get("workspace_id", "")) for item in documents if item.get("workspace_id")],
                "workspace_count": len(documents),
                "size": path.stat().st_size,
            }
        )
    return sorted(records, key=lambda item: item["backup_time"], reverse=True)


def chat_backup_path(name: str) -> Path:
    root = (BACKUPS_ROOT / "chat").resolve()
    path = (root / Path(name).name).resolve()
    if path.parent != root or not path.is_file():
        raise RuntimeError("备份文件不存在。")
    return path


def rebuild_chat_views(workspace_id: str, prune: bool = False) -> dict[str, int]:
    """以历史记录为准补齐显示索引；删除场景同时清理已失效条目。"""
    histories = chat_records(workspace_id)
    latest: dict[str, dict[str, Any]] = {}
    for chat in histories:
        session_id = str(chat.get("sessionId") or chat.get("session_id") or "")
        if session_id and chat_sort_timestamp(chat.get("timestamp")) >= chat_sort_timestamp(latest.get(session_id, {}).get("timestamp")):
            latest[session_id] = chat
    state_path = workspace_state_db(workspace_id)
    if not state_path.is_file():
        return {"views_added": 0, "tabs_added": 0}

    views = read_item_json(state_path, "aicoding.chat.views", [])
    tabs_document = read_item_json(state_path, "aicoding.chat.tabs", {"activeTabId": "", "tabs": []})
    views = views if isinstance(views, list) else []
    tabs_document = tabs_document if isinstance(tabs_document, dict) else {"activeTabId": "", "tabs": []}
    tabs = tabs_document.get("tabs") if isinstance(tabs_document.get("tabs"), list) else []
    if prune:
        views = [item for item in views if str(item.get("sessionId", "")) in latest]
        tabs = [item for item in tabs if str(item.get("sessionId", "")) in latest]
    view_ids = {str(item.get("sessionId", "")) for item in views}
    tab_ids = {str(item.get("sessionId", "")) for item in tabs}
    added_views = added_tabs = 0
    for session_id, chat in latest.items():
        title = str(chat.get("title") or f"会话 {session_id[:8]}")
        if session_id not in view_ids:
            views.append({"active": False, "id": session_id, "location": "auxiliaryBar", "sessionId": session_id, "sessionType": "assistant", "targetRemoteAuthority": None, "title": title, "viewId": str(uuid4())})
            added_views += 1
        if session_id not in tab_ids:
            tabs.append({"sessionId": session_id, "tabId": str(uuid4()), "targetRemoteAuthority": None, "title": title})
            added_tabs += 1
    tabs_document["tabs"] = tabs
    db = db_connect(state_path)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS ItemTable(key TEXT PRIMARY KEY ON CONFLICT REPLACE, value BLOB)")
        write_item_json(db, "aicoding.chat.views", views)
        write_item_json(db, "aicoding.chat.tabs", tabs_document)
        db.commit()
    finally:
        db.close()
    return {"views_added": added_views, "tabs_added": added_tabs}


def restore_chat_backup(name: str, target_workspace_id: str | None = None) -> dict[str, Any]:
    value = json_load(chat_backup_path(name), None)
    documents = value if isinstance(value, list) else [value]
    if not all(isinstance(item, dict) for item in documents):
        raise RuntimeError("备份文件格式无效。")
    if target_workspace_id:
        # 指定目标时必须从备份中精确匹配工作区，禁止把全量备份的首个工作区误写到当前工作区。
        matched = [item for item in documents if str(item.get("workspace_id", "")) == target_workspace_id]
        if not matched:
            if len(documents) == 1 and not documents[0].get("workspace_id"):
                matched = documents
            else:
                raise RuntimeError("备份中找不到指定工作区，未执行恢复。")
        documents = matched

    restored = 0
    restored_workspaces: list[tuple[str, dict[str, Any]]] = []
    state_path = qoder_state_db()
    if state_path is None:
        raise RuntimeError("找不到 Qoder state.vscdb。")
    # 恢复会覆盖目标工作区，先生成可从“备份”视图再次选择的完整会话快照。
    restore_targets = {
        validate_workspace_id(target_workspace_id or str(document.get("workspace_id", "")))
        for document in documents
    }
    for workspace_id in restore_targets:
        save_chat_backup(workspace_id)
    backup_session_database(state_path)
    db = db_connect(state_path)
    try:
        # 全局历史作为一次事务写入，避免批量恢复只完成一部分。
        db.execute("BEGIN IMMEDIATE")
        snapshot = read_item_json(state_path, "aicoding.questTaskListSnapshot", {})
        snapshot_changed = False
        for document in documents:
            workspace_id = validate_workspace_id(target_workspace_id or str(document.get("workspace_id", "")))
            history = document.get("chat_history", [])
            if not isinstance(history, list):
                raise RuntimeError("备份中的对话记录格式无效。")
            write_item_json(db, chat_history_key(workspace_id), history)
            quest_tasks = document.get("quest_tasks", [])
            if isinstance(snapshot, dict) and isinstance(snapshot.get("folders"), dict) and isinstance(quest_tasks, list):
                target_path = normalized_workspace_path(workspace_path_for_id(workspace_id) or document.get("workspace_path", ""))
                folder = next((item for path, item in snapshot["folders"].items() if normalized_workspace_path(path) == target_path), None)
                if isinstance(folder, dict) and isinstance(folder.get("tasks"), list):
                    # 备份代表目标工作区的完整快照：先清空现有 Quest，再按备份记录重建，避免旧任务残留。
                    folder["tasks"] = [
                        {"id": str(quest.get("id", "")), "title": quest.get("title", ""), "name": quest.get("title", ""), "status": quest.get("status", ""), "updatedAtTimestamp": quest.get("timestamp", 0), "createTime": quest.get("timestamp", 0), "executionSessionId": quest.get("sessionId", ""), "questType": quest.get("quest_type", "agent")}
                        for quest in quest_tasks
                        if str(quest.get("id", ""))
                    ]
                    snapshot_changed = True
            restored_workspaces.append((workspace_id, document))
            restored += len(history)
        if snapshot_changed:
            write_item_json(db, "aicoding.questTaskListSnapshot", snapshot)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    for workspace_id, document in restored_workspaces:
        # workspaceStorage 中的 views/tabs 决定 IDE 是否展示会话，恢复快照时与全局历史一起写回。
        workspace_db = workspace_state_db(workspace_id)
        if workspace_db.is_file() and ("chat_views" in document or "chat_tabs" in document):
            view_db = db_connect(workspace_db)
            try:
                view_db.execute("BEGIN IMMEDIATE")
                if "chat_views" in document:
                    write_item_json(view_db, "aicoding.chat.views", document.get("chat_views", []))
                if "chat_tabs" in document:
                    write_item_json(view_db, "aicoding.chat.tabs", document.get("chat_tabs", {"activeTabId": "", "tabs": []}))
                view_db.commit()
            finally:
                view_db.close()
        rebuild_chat_views(workspace_id, prune=True)
    return {"workspace_count": len(documents), "chat_count": restored}


def delete_chats(workspace_id: str, chat_ids: list[str]) -> dict[str, int]:
    ids = {str(item) for item in chat_ids if str(item)}
    if not ids:
        raise RuntimeError("没有选择要删除的对话。")
    save_chat_backup(workspace_id)
    histories = chat_records(workspace_id)
    # IDE 记录写回历史键，Quest 记录只清理当前工作区对应的 folder，避免同 ID 任务跨工作区被误删。
    remaining = [chat for chat in histories if str(chat.get("id", "")) not in ids]
    chat_remaining = [chat for chat in remaining if chat.get("kind") != "quest"]
    quest_ids = {str(chat.get("id", "")) for chat in histories if chat.get("kind") == "quest" and str(chat.get("id", "")) in ids}
    state_path = qoder_state_db()
    if state_path is None:
        raise RuntimeError("找不到 Qoder state.vscdb。")
    db = db_connect(state_path)
    try:
        write_item_json(db, chat_history_key(workspace_id), chat_remaining)
        if quest_ids:
            snapshot = read_item_json(state_path, "aicoding.questTaskListSnapshot", {})
            if isinstance(snapshot, dict):
                workspace_path = normalized_workspace_path(workspace_path_for_id(workspace_id))
                for folder_path, folder in (snapshot.get("folders") or {}).items():
                    if workspace_path and normalized_workspace_path(folder_path) != workspace_path:
                        continue
                    if isinstance(folder, dict) and isinstance(folder.get("tasks"), list):
                        folder["tasks"] = [task for task in folder["tasks"] if str(task.get("id", "")) not in quest_ids]
                write_item_json(db, "aicoding.questTaskListSnapshot", snapshot)
        db.commit()
    finally:
        db.close()
    rebuild_chat_views(workspace_id, prune=True)
    return {"deleted": len(histories) - len(remaining), "remaining": len(remaining)}


def merge_chat_workspaces(source_id: str, target_id: str) -> dict[str, Any]:
    source_id, target_id = validate_workspace_id(source_id), validate_workspace_id(target_id)
    if source_id == target_id:
        raise RuntimeError("来源与目标工作区不能相同。")
    source_backup, target_backup = save_chat_backup(source_id), save_chat_backup(target_id)
    source_all, target_all = chat_records(source_id), chat_records(target_id)
    source = [item for item in source_all if item.get("kind") != "quest"]
    target = [item for item in target_all if item.get("kind") != "quest"]
    target_ids = {str(item.get("id", "")) for item in target}
    additions = [item for item in source if str(item.get("id", "")) not in target_ids]
    source_quests = [item for item in source_all if item.get("kind") == "quest"]
    state_path = qoder_state_db()
    if state_path is None:
        raise RuntimeError("找不到 Qoder state.vscdb。")
    quest_snapshot = read_item_json(state_path, "aicoding.questTaskListSnapshot", {})
    source_folder = target_folder = None
    if source_quests:
        folders = quest_snapshot.get("folders") if isinstance(quest_snapshot, dict) else None
        source_path = normalized_workspace_path(workspace_path_for_id(source_id))
        target_path = normalized_workspace_path(workspace_path_for_id(target_id))
        source_folder = next((folder for path, folder in (folders or {}).items() if normalized_workspace_path(path) == source_path), None)
        target_folder = next((folder for path, folder in (folders or {}).items() if normalized_workspace_path(path) == target_path), None)
        if not (isinstance(source_folder, dict) and isinstance(source_folder.get("tasks"), list) and isinstance(target_folder, dict) and isinstance(target_folder.get("tasks"), list)):
            raise RuntimeError("源或目标工作区缺少 Quest 任务索引，未执行合并。")

    db = db_connect(state_path)
    try:
        # IDE 历史与 Quest 索引必须同一事务提交，避免出现“对话已合并但 Quest 仍留在源工作区”。
        db.execute("BEGIN IMMEDIATE")
        write_item_json(db, chat_history_key(target_id), target + additions)
        write_item_json(db, chat_history_key(source_id), [])
        moved_quests = 0
        if source_quests:
            target_quest_ids = {str(task.get("id", "")) for task in target_folder["tasks"]}
            source_quest_ids = {str(item.get("id", "")) for item in source_quests}
            for task in list(source_folder["tasks"]):
                task_id = str(task.get("id", ""))
                if task_id in source_quest_ids:
                    if task_id not in target_quest_ids:
                        target_folder["tasks"].append(task)
                        target_quest_ids.add(task_id)
                    source_folder["tasks"].remove(task)
                    moved_quests += 1
            write_item_json(db, "aicoding.questTaskListSnapshot", quest_snapshot)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    rebuild_chat_views(source_id, prune=True)
    rebuild_chat_views(target_id, prune=True)
    return {"merged": len(additions) + moved_quests, "skipped": len(source) - len(additions), "target_total": len(target) + len(additions) + moved_quests, "source_backup": str(source_backup), "target_backup": str(target_backup)}


def delete_chat_workspace(workspace_id: str) -> dict[str, Any]:
    workspace_id = validate_workspace_id(workspace_id)
    backup = save_chat_backup(workspace_id)
    # 同时按任务 ID 清理全局 Quest 快照；workspaceStorage 目录删除本身不会触碰这个全局索引。
    quest_ids = {str(item.get("id", "")) for item in chat_records(workspace_id) if item.get("kind") == "quest"}
    state_path = qoder_state_db()
    if state_path is None:
        raise RuntimeError("找不到 Qoder state.vscdb。")
    db = db_connect(state_path)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM ItemTable WHERE key = ?", (chat_history_key(workspace_id),))
        snapshot = read_item_json(state_path, "aicoding.questTaskListSnapshot", {})
        if quest_ids and isinstance(snapshot, dict) and isinstance(snapshot.get("folders"), dict):
            workspace_path = normalized_workspace_path(workspace_path_for_id(workspace_id))
            for folder_path, folder in snapshot["folders"].items():
                if workspace_path and normalized_workspace_path(folder_path) != workspace_path:
                    continue
                if isinstance(folder, dict) and isinstance(folder.get("tasks"), list):
                    folder["tasks"] = [task for task in folder["tasks"] if str(task.get("id", "")) not in quest_ids]
            write_item_json(db, "aicoding.questTaskListSnapshot", snapshot)
        db.commit()
    finally:
        db.close()
    root = workspace_storage_root().resolve()
    directory = (root / workspace_id).resolve()
    deleted = False
    if root in directory.parents and directory.is_dir():
        shutil.rmtree(directory)
        deleted = True
    return {"backup": str(backup), "directory_deleted": deleted}


def export_chats_markdown() -> bytes:
    lines = ["# Qoder 对话记录", "", f"导出时间：{now_iso()}", ""]
    # 导出沿用会话列表的统一时间解析，保证 Quest 的秒级或 ISO 时间不会阻塞整份 Markdown 下载。
    for workspace in chat_workspace_records():
        lines.extend([f"## {workspace['name']}", "", f"工作区：`{workspace['path'] or workspace['id']}`", ""])
        for chat in chat_records(workspace["id"]):
            timestamp = chat_sort_timestamp(chat.get("timestamp"))
            when = datetime.fromtimestamp(timestamp / 1000, timezone.utc).astimezone().isoformat(timespec="minutes") if timestamp else "未知"
            lines.extend([f"### {chat.get('title') or '无标题'}", "", f"- 时间：{when}", f"- 会话 ID：`{chat.get('sessionId') or chat.get('session_id') or ''}`", f"- 上下文数量：{len(chat.get('context') or [])}", ""])
    return "\n".join(lines).encode("utf-8")


def public_account(account: dict[str, Any]) -> dict[str, Any]:
    copy = dict(account)
    copy.pop("values", None)
    return copy


class Handler(BaseHTTPRequestHandler):
    server_version = "QoderCockpit/0.1"

    def send_json(self, value: Any, status: int = 200) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_download(self, content: bytes, filename: str, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/config":
                self.send_json({"qoder_root": str(QODER_ROOT), "state_db": str(qoder_state_db() or ""), "qoder_running": is_qoder_running()})
            elif parsed.path == "/api/accounts":
                current = normalize_account(read_state_values(qoder_state_db())) if qoder_state_db() else None
                self.send_json({"accounts": [public_account(item) for item in load_accounts()], "current": public_account(current) if current else None})
            elif parsed.path == "/api/projects":
                self.send_json({"projects": project_records()})
            elif parsed.path == "/api/chat/workspaces":
                self.send_json({"workspaces": chat_workspace_records()})
            elif parsed.path == "/api/chat/list":
                workspace_id = urllib.parse.parse_qs(parsed.query).get("workspace_id", [""])[0]
                self.send_json({"chats": chat_records(workspace_id)})
            elif parsed.path == "/api/chat/backups":
                self.send_json({"backups": chat_backup_records()})
            elif parsed.path == "/api/chat/export":
                self.send_download(export_chats_markdown(), "qoder-chats.md", "text/markdown; charset=utf-8")
            elif parsed.path == "/api/health":
                self.send_json({"ok": True, "qoder_running": is_qoder_running()})
            elif parsed.path == "/api/oauth/status":
                login_id = urllib.parse.parse_qs(parsed.query).get("login_id", [""])[0]
                with oauth_lock:
                    pending = dict(oauth_state.get(login_id, {}))
                self.send_json({key: pending[key] for key in ("login_id", "status", "verification_uri", "expires_at", "account", "error") if key in pending})
            elif parsed.path.startswith("/api/snapshots"):
                query = urllib.parse.parse_qs(parsed.query)
                account_id = query.get("account_id", [""])[0]
                project = Path(query.get("project", [""])[0])
                source = latest_snapshot(account_id, project) if account_id and project else None
                self.send_json({"latest": str(source) if source else None})
            elif parsed.path == "/" or parsed.path == "/index.html":
                content = (STATIC_ROOT / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            elif parsed.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            elif parsed.path.startswith("/static/"):
                target = (STATIC_ROOT / parsed.path.removeprefix("/static/")).resolve()
                if STATIC_ROOT.resolve() not in target.parents:
                    raise RuntimeError("非法静态资源路径")
                content = target.read_bytes()
                # 为本地字体返回正确 MIME，避免浏览器因 nosniff 或字体格式校验拒绝加载。
                content_types = {
                    ".css": "text/css",
                    ".js": "text/javascript",
                    ".woff2": "font/woff2",
                    ".woff": "font/woff",
                    ".ttf": "font/ttf",
                }
                content_type = content_types.get(target.suffix.lower(), "application/octet-stream")
                self.send_response(200)
                if content_type.startswith("text/"):
                    content_type = f"{content_type}; charset=utf-8"
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self) -> None:
        try:
            payload = self.read_json()
            if self.path == "/api/accounts/import-local":
                values = read_state_values(qoder_state_db())
                if not values:
                    raise RuntimeError("没有读取到 Qoder 当前账号数据，请确认 Qoder 已登录并已关闭后重试。")
                account = merge_account(normalize_account(values))
                self.send_json({"account": public_account(account)})
            elif self.path == "/api/accounts/refresh":
                account_id = str(payload.get("account_id", "")).strip() or None
                self.send_json(refresh_all_quotas(account_id))
            elif self.path == "/api/oauth/start":
                verifier = oauth_urlsafe(secrets.token_bytes(32))
                challenge = oauth_urlsafe(hashlib.sha256(verifier.encode("ascii")).digest())
                login_id = str(uuid4())
                nonce = str(uuid4())
                verification_uri = oauth_build_login_url(nonce, challenge)
                with oauth_lock:
                    oauth_state[login_id] = {"login_id": login_id, "status": "pending", "nonce": nonce, "verifier": verifier, "verification_uri": verification_uri, "expires_at": time.time() + 600}
                threading.Thread(target=oauth_worker, args=(login_id,), daemon=True).start()
                self.send_json({"login_id": login_id, "verification_uri": verification_uri, "expires_in": 600, "interval_seconds": 1})
            elif self.path == "/api/oauth/cancel":
                login_id = str(payload.get("login_id", ""))
                with oauth_lock:
                    if login_id in oauth_state:
                        oauth_state[login_id]["cancelled"] = True
                        oauth_state[login_id]["status"] = "cancelled"
                self.send_json({"cancelled": login_id})
            elif self.path == "/api/accounts/delete":
                account_id = str(payload.get("account_id", ""))
                with quota_refresh_lock:
                    save_accounts([item for item in load_accounts() if item.get("id") != account_id])
                self.send_json({"deleted": account_id})
            elif self.path == "/api/switch":
                target_id = str(payload.get("target_account_id", ""))
                source_id = str(payload.get("source_account_id", ""))
                if not any(item.get("id") == target_id for item in load_accounts()):
                    raise RuntimeError("目标账号不存在，请先导入账号。")
                projects = [Path(item["path"]) for item in project_records()]
                saved = []
                state_db = qoder_state_db()
                if state_db is None:
                    raise RuntimeError("找不到 Qoder state.vscdb。")
                target = next(item for item in load_accounts() if item.get("id") == target_id)
                source_account = next((item for item in load_accounts() if item.get("id") == source_id), None)
                if source_id and payload.get("inherit_sessions", True) and source_account is None:
                    raise RuntimeError("来源账号不存在，无法继承历史会话。")
                session_db: Path | None = None
                if source_id and payload.get("inherit_sessions", True):
                    session_db = qoder_session_db()
                    if session_db is None:
                        raise RuntimeError("找不到 Qoder 本地会话数据库 local.db，无法同步 IDE 历史会话。")
                    validate_local_session_database(session_db)
                # 基础会话库预检通过后才关闭 Qoder，避免不兼容版本仅因检查失败就中断当前工作状态。
                was_running = stop_qoder()
                backup_db = BACKUPS_ROOT / "state" / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.vscdb"
                backup_db.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(state_db, backup_db)
                active_account_id = state_account_id(state_db)
                source_state_db = backup_db if source_id and source_id == active_account_id else latest_session_state(source_id)
                if source_id and source_id == active_account_id and payload.get("save_previous", True):
                    # 项目文件和全局 Quest 索引必须在同一登录态下成套保存，才能恢复完整会话列表。
                    for project in projects:
                        saved.append(snapshot_project(source_id, project))
                    source_state_db = snapshot_session_state(source_id, state_db)
                restored = []
                restored_state_rows = 0
                local_sessions: dict[str, Any] | None = None
                try:
                    write_state_values(state_db, target["values"])
                    if source_id and payload.get("inherit_sessions", True):
                        if source_state_db is None:
                            raise RuntimeError("来源账号没有可用的会话状态快照，请先登录该账号并保存一次会话。")
                        source_state_rows = read_session_state_rows(source_state_db)
                        restored_state_rows = write_session_state_rows(state_db, source_state_rows, target["values"])
                        for project in projects:
                            restored.append({"project": str(project), **restore_project(source_id, project)})
                        # 基础 Chat 表必迁移，Quest/Agent 扩展表按当前 Qoder 安装能力选择性迁移。
                        local_sessions = rebind_local_sessions(session_db, source_account["values"], target["values"])
                except Exception as exc:
                    # 迁移事务失败时恢复完整账号状态，避免 Qoder 留在“目标账号凭据 + 来源账号会话”的半完成状态。
                    shutil.copy2(backup_db, state_db)
                    restart_error = None
                    if was_running:
                        try:
                            launch_qoder()
                        except Exception as restart_exc:
                            restart_error = str(restart_exc)
                    message = f"切换失败，已恢复原账号状态：{exc}"
                    if restart_error:
                        message += f"；Qoder 自动重启失败：{restart_error}"
                    raise RuntimeError(message) from exc
                launched = launch_qoder()
                self.send_json(
                    {
                        "target": public_account(target),
                        "saved": saved,
                        "restored": restored,
                        "restored_state_rows": restored_state_rows,
                        "local_sessions": local_sessions,
                        "state_backup": str(backup_db),
                        "qoder_started": was_running or bool(launched),
                    }
                )
            elif self.path == "/api/qoder/start":
                if is_qoder_running():
                    self.send_json({"started": False, "message": "Qoder 已在运行。"})
                    return
                executable = launch_qoder()
                self.send_json({"started": True, "path": str(executable)})
            elif self.path == "/api/session/save":
                require_qoder_stopped(bool(payload.get("allow_running")))
                account_id = str(payload.get("account_id", ""))
                project = Path(str(payload.get("project_path", ""))).resolve()
                if not project.is_dir():
                    raise RuntimeError("项目目录不存在。")
                self.send_json(snapshot_project(account_id, project))
            elif self.path == "/api/session/restore":
                require_qoder_stopped(bool(payload.get("allow_running")))
                project = Path(str(payload.get("project_path", ""))).resolve()
                self.send_json(restore_project(str(payload.get("account_id", "")), project))
            elif self.path == "/api/chat/backup":
                was_running = stop_qoder()
                try:
                    path = save_all_chat_backups() if payload.get("all") else save_chat_backup(str(payload.get("workspace_id", "")))
                finally:
                    if was_running:
                        launch_qoder()
                self.send_json({"path": str(path), "name": path.name})
            elif self.path == "/api/chat/restore":
                was_running = stop_qoder()
                try:
                    result = restore_chat_backup(str(payload.get("name", "")), str(payload.get("target_workspace_id", "")).strip() or None)
                finally:
                    if was_running:
                        launch_qoder()
                self.send_json(result)
            elif self.path == "/api/chat/delete":
                was_running = stop_qoder()
                try:
                    result = delete_chats(str(payload.get("workspace_id", "")), payload.get("chat_ids", []))
                finally:
                    if was_running:
                        launch_qoder()
                self.send_json(result)
            elif self.path == "/api/chat/repair":
                was_running = stop_qoder()
                try:
                    result = rebuild_chat_views(str(payload.get("workspace_id", "")), prune=False)
                finally:
                    if was_running:
                        launch_qoder()
                self.send_json(result)
            elif self.path == "/api/chat/merge":
                was_running = stop_qoder()
                try:
                    result = merge_chat_workspaces(str(payload.get("source_workspace_id", "")), str(payload.get("target_workspace_id", "")))
                finally:
                    if was_running:
                        launch_qoder()
                self.send_json(result)
            elif self.path == "/api/chat/workspace/delete":
                was_running = stop_qoder()
                try:
                    result = delete_chat_workspace(str(payload.get("workspace_id", "")))
                finally:
                    if was_running:
                        launch_qoder()
                self.send_json(result)
            elif self.path == "/api/chat/backup/delete":
                path = chat_backup_path(str(payload.get("name", "")))
                path.unlink()
                self.send_json({"deleted": path.name})
            else:
                self.send_error(404)
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, 409)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"[{now_iso()}] {format % args}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Qoder 本地账号、额度和会话管理器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    init_database()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    quota_thread = threading.Thread(target=quota_refresh_loop, name="qoder-quota-refresh", daemon=True)
    quota_thread.start()
    url = f"http://{args.host}:{args.port}"
    print(f"Qoder Cockpit running at {url}")
    if args.host in {"127.0.0.1", "localhost"}:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        quota_refresh_stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
