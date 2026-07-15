from __future__ import annotations

import argparse
import atexit
import base64
import binascii
import copy
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import tomllib
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
CONFIG_PATH = APP_DIR / "config.json"
SETTINGS_PATH = APP_DIR / "settings.json"
PRESET_LIBRARY_PATH = APP_DIR / "preset-library.json"
SETTINGS_BACKUP_DIR = APP_DIR / "settings-backups"
LOG_DIR = Path(os.environ.get("QWEN_LAUNCHER_LOG_DIR", str(APP_DIR / "logs"))).resolve()
PID_PATH = Path(os.environ.get("QWEN_LAUNCHER_PID_PATH", str(APP_DIR / "web-launcher.pid"))).resolve()
ACTIVE_MODEL_PATH = Path(os.environ.get("QWEN_LAUNCHER_ACTIVE_MODEL_PATH", str(APP_DIR / "active-model.json"))).resolve()
MANAGED_SERVICES_PATH = Path(os.environ.get("QWEN_LAUNCHER_MANAGED_SERVICES_PATH", str(APP_DIR / "managed-services.json"))).resolve()
DEFAULT_OPENWEBUI_ROOT = Path(os.environ.get("OPENWEBUI_ROOT", str(APP_DIR.parent / "OpenWebUI"))).resolve()
POWERSHELL_EXE = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"

SAMPLING_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "repeat_penalty",
)
DEFAULT_SAMPLING = {
    "temperature": 0.8,
    "top_p": 0.95,
    "top_k": 40,
    "min_p": 0.05,
    "presence_penalty": 0.0,
    "repeat_penalty": 1.0,
}
CACHE_TYPES = ("f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1")
PERFORMANCE_KEYS = (
    "cache_type_k", "cache_type_v", "batch_size", "ubatch_size", "parallel",
    "fit", "fit_target", "flash_attention", "gpu_layers",
)
GENERATION_KEYS = ("n_predict", "reasoning", "reasoning_budget", "reasoning_preserve")
CUSTOM_OPTION_SPECS = {
    "vision": {"type": bool},
    "context": {"type": int, "minimum": 512, "maximum": 1_010_000},
    "temperature": {"type": float, "minimum": 0.0, "maximum": 5.0},
    "top_p": {"type": float, "minimum": 0.0, "maximum": 1.0},
    "top_k": {"type": int, "minimum": 0, "maximum": 1000},
    "min_p": {"type": float, "minimum": 0.0, "maximum": 1.0},
    "presence_penalty": {"type": float, "minimum": -2.0, "maximum": 2.0},
    "repeat_penalty": {"type": float, "minimum": 0.0, "maximum": 5.0},
    "n_predict": {"type": int, "minimum": -1, "maximum": 1_010_000},
    "reasoning": {"type": str, "choices": ("on", "off", "auto")},
    "reasoning_budget": {"type": int, "minimum": -1, "maximum": 1_010_000},
    "reasoning_preserve": {"type": str, "choices": ("auto", "on", "off")},
    "cache_type_k": {"type": str, "choices": CACHE_TYPES},
    "cache_type_v": {"type": str, "choices": CACHE_TYPES},
    "batch_size": {"type": int, "minimum": 1, "maximum": 131_072},
    "ubatch_size": {"type": int, "minimum": 1, "maximum": 131_072},
    "parallel": {"type": int, "minimum": 1, "maximum": 64},
    "fit": {"type": str, "choices": ("on", "off")},
    "fit_target": {"type": int, "minimum": 0, "maximum": 65_536},
    "flash_attention": {"type": str, "choices": ("on", "off", "auto")},
    "gpu_layers": {"type": "gpu_layers"},
}


def performance_defaults(server: dict) -> dict:
    return {
        "cache_type_k": server.get("cache_type_k", "f16"),
        "cache_type_v": server.get("cache_type_v", "f16"),
        "batch_size": server["batch_size"],
        "ubatch_size": server["ubatch_size"],
        "parallel": server["parallel"],
        "fit": server["fit"],
        "fit_target": server.get("fit_target", 1024),
        "flash_attention": server["flash_attention"],
        "gpu_layers": server["gpu_layers"],
    }


def generation_defaults(profile: dict) -> dict:
    return {
        "n_predict": profile.get("n_predict", -1),
        "reasoning": profile["reasoning"],
        "reasoning_budget": profile.get("reasoning_budget", -1),
        "reasoning_preserve": profile.get("reasoning_preserve", "auto"),
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def password_hash(password: str, *, salt: bytes | None = None) -> str:
    if not isinstance(password, str) or not password:
        raise ValueError("Password cannot be empty")
    salt = salt or secrets.token_bytes(16)
    n, r, p = 16_384, 8, 1
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(expected)),
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (AttributeError, TypeError, ValueError):
        return False


def instance_mutex_name(app_dir: Path = APP_DIR) -> str:
    identity = str(app_dir.resolve()).casefold().encode("utf-8")
    return f"Local\\LocalModelLaunchpad-{hashlib.sha256(identity).hexdigest()[:24]}"


def acquire_single_instance(app_dir: Path = APP_DIR) -> tuple[bool, int | None]:
    if os.name != "nt":
        return True, None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, instance_mutex_name(app_dir))
    if not handle:
        raise ctypes.WinError()
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False, None
    return True, int(handle)


def release_single_instance(handle: int | None) -> None:
    if os.name == "nt" and handle:
        ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))


def slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value[:64] or "model"


def normalize_service_url(raw_value, label: str) -> str:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError(f"{label} address is required")
    value = raw_value.strip()
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError(f"{label} address must use http or https without credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"{label} address must contain only an IP address and port")
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} address must contain a valid IP address and port") from exc
    if port is None or not 1 <= port <= 65535:
        raise ValueError(f"{label} address must include a port between 1 and 65535")
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"{parsed.scheme}://{host}:{port}"


def validate_server_executable(raw_value) -> str:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError("llama-server executable path is required")
    path = Path(raw_value.strip().strip('"')).resolve()
    if not path.is_absolute() or path.name.lower() != "llama-server.exe":
        raise ValueError("Executable must be an absolute path to llama-server.exe")
    if not path.is_file():
        raise FileNotFoundError(f"llama-server executable not found: {path}")
    return str(path)


def normalize_service_root(raw_value) -> str:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError("OpenWebUI folder is required")
    path = Path(raw_value.strip().strip('"')).expanduser()
    if not path.is_absolute():
        raise ValueError("OpenWebUI folder must be an absolute path")
    return str(path.resolve())


def load_user_settings(default_executable: str) -> dict:
    if SETTINGS_PATH.is_file():
        with SETTINGS_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("settings.json must contain a JSON object")
    else:
        data = {
            "openwebui_enabled": False,
            "openwebui_root": str(DEFAULT_OPENWEBUI_ROOT),
            "openwebui_url": "http://127.0.0.1:8181",
            "openterminal_url": "http://127.0.0.1:8765",
            "vane_enabled": False,
            "vane_url": "http://127.0.0.1:32761",
            "llama_server_executable": default_executable,
            "llama_mayhem": False,
        }
    openwebui_enabled = data.get("openwebui_enabled", False)
    if not isinstance(openwebui_enabled, bool):
        raise ValueError("openwebui_enabled must be true or false")
    vane_enabled = data.get("vane_enabled", False)
    if not isinstance(vane_enabled, bool):
        raise ValueError("vane_enabled must be true or false")
    llama_mayhem = data.get("llama_mayhem", False)
    if not isinstance(llama_mayhem, bool):
        raise ValueError("llama_mayhem must be true or false")
    return {
        "openwebui_enabled": openwebui_enabled,
        "openwebui_root": normalize_service_root(data.get("openwebui_root", str(DEFAULT_OPENWEBUI_ROOT))),
        "openwebui_url": normalize_service_url(data.get("openwebui_url", "http://127.0.0.1:8181"), "OpenWebUI"),
        "openterminal_url": normalize_service_url(data.get("openterminal_url", "http://127.0.0.1:8765"), "OpenTerminal"),
        "vane_enabled": vane_enabled,
        "vane_url": normalize_service_url(data.get("vane_url", "http://127.0.0.1:32761"), "Vane"),
        "llama_server_executable": validate_server_executable(data.get("llama_server_executable")),
        "llama_mayhem": llama_mayhem,
    }


def persist_user_settings(settings: dict) -> None:
    SETTINGS_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if SETTINGS_PATH.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy2(SETTINGS_PATH, SETTINGS_BACKUP_DIR / f"settings-{stamp}.json")
    temp_path = SETTINGS_PATH.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(settings, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, SETTINGS_PATH)


def service_definitions(config: dict) -> dict[str, dict]:
    openwebui_root = Path(config.get("openwebui_root", str(DEFAULT_OPENWEBUI_ROOT))).resolve()
    openterminal_root = openwebui_root / "OpenTerminal"
    terminal_config_path = openterminal_root / "config.toml"
    terminal_config = {}
    if terminal_config_path.is_file():
        try:
            with terminal_config_path.open("rb") as handle:
                terminal_config = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            LOGGER.warning("Unable to read optional OpenTerminal config: %s", terminal_config_path)
    terminal_port = int(terminal_config.get("port", 8765))
    openwebui_port = int(urlparse(config["openwebui_url"]).port or 8181)
    terminal_endpoint = config["openterminal_url"].rstrip("/")
    return {
        "openwebui": {
            "name": "OpenWebUI",
            "script": openwebui_root / "Start-OpenWebUI.ps1",
            "working_directory": openwebui_root,
            "port": openwebui_port,
            "health_url": config["openwebui_url"],
            "open_url": config["openwebui_url"],
            "stdout": openwebui_root / "logs" / "open-webui.out.log",
            "stderr": openwebui_root / "logs" / "open-webui.err.log",
        },
        "openterminal": {
            "name": "OpenTerminal",
            "script": openterminal_root / "Start-OpenTerminal.ps1",
            "working_directory": openterminal_root,
            "port": terminal_port,
            "health_url": f"{terminal_endpoint}/health",
            "open_url": None,
            "stdout": openterminal_root / "logs" / "open-terminal.out.log",
            "stderr": openterminal_root / "logs" / "open-terminal.err.log",
        },
        "vane": {
            "name": "Vane",
            "health_url": config["vane_url"],
            "open_url": config["vane_url"],
        },
    }


def url_is_live(url: str, timeout: float = 1.5) -> bool:
    try:
        request = Request(url, headers={"User-Agent": "Local-Model-Launchpad/1.0"})
        with urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 500
    except Exception:
        return False


def load_managed_service_state() -> dict:
    if not MANAGED_SERVICES_PATH.is_file():
        return {"version": 1, "services": {}}
    try:
        with MANAGED_SERVICES_PATH.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        if state.get("version") != 1 or not isinstance(state.get("services"), dict):
            raise ValueError("unsupported managed service state")
        return state
    except (OSError, ValueError, json.JSONDecodeError):
        LOGGER.warning("Ignoring invalid managed service state: %s", MANAGED_SERVICES_PATH)
        return {"version": 1, "services": {}}


def persist_managed_service_state(state: dict) -> None:
    temp_path = MANAGED_SERVICES_PATH.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, MANAGED_SERVICES_PATH)


def update_managed_service_record(service_id: str, record: dict | None) -> None:
    state = load_managed_service_state()
    if record is None:
        state["services"].pop(service_id, None)
    else:
        state["services"][service_id] = record
    if state["services"]:
        persist_managed_service_state(state)
    else:
        MANAGED_SERVICES_PATH.unlink(missing_ok=True)


def validated_managed_service_record(service_id: str, spec: dict) -> dict | None:
    record = load_managed_service_state()["services"].get(service_id)
    if not isinstance(record, dict):
        return None
    try:
        recorded_port = int(record.get("port", -1))
    except (TypeError, ValueError):
        return None
    if recorded_port != int(spec["port"]):
        return None
    listener = record.get("listener")
    if not isinstance(listener, dict):
        return None
    try:
        pid = int(listener["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    if pid not in listener_pids(spec["port"]) or not process_identity_matches(pid, listener):
        return None
    return record


def registered_model_directory(registry) -> Path:
    with registry.lock:
        parents = [
            str(Path(model["model_path"]).resolve().parent)
            for model in registry.data.get("models", [])
            if isinstance(model.get("model_path"), str) and model["model_path"]
        ]
    if parents:
        try:
            common = Path(os.path.commonpath(parents))
            if common.is_dir():
                return common
        except (OSError, ValueError):
            pass
    return Path.home()


def choose_gguf_file(initial_path: str, default_directory: Path, title: str) -> str | None:
    if os.name != "nt":
        raise RuntimeError("The native GGUF picker is available on Windows only")
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise RuntimeError("The Python installation does not include the Windows file picker") from exc

    initial_directory = default_directory
    initial_file = ""
    if initial_path:
        candidate = Path(initial_path).expanduser()
        if candidate.is_dir():
            initial_directory = candidate
        elif candidate.is_file():
            initial_directory = candidate.parent
            initial_file = candidate.name
        elif candidate.parent.is_dir():
            initial_directory = candidate.parent
            initial_file = candidate.name

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askopenfilename(
            parent=root,
            title=title,
            initialdir=str(initial_directory),
            initialfile=initial_file,
            filetypes=(("GGUF model files", "*.gguf"), ("All files", "*.*")),
        )
    except tk.TclError as exc:
        raise RuntimeError(f"Unable to open the Windows file picker: {exc}") from exc
    finally:
        if root is not None:
            root.destroy()

    if not selected:
        return None
    selected_path = Path(selected).resolve()
    if selected_path.suffix.casefold() != ".gguf" or not selected_path.is_file():
        raise ValueError("Choose an existing .gguf file")
    return str(selected_path)


def normalized_ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
    except ValueError:
        return None
    return address


def is_local_machine_address(value: str, connection_local_address: str = "") -> bool:
    address = normalized_ip_address(value)
    if address is None:
        return False
    if address.is_loopback:
        return True

    # A browser using one of this host's own LAN addresses produces an accepted
    # connection with the same normalized address at both endpoints. Comparing
    # the live connection is reliable even when hostname resolution temporarily
    # omits an active interface on a multi-adapter Windows host.
    local_endpoint = normalized_ip_address(connection_local_address)
    if local_endpoint is not None and address == local_endpoint:
        return True

    local_addresses = set()
    for hostname in {socket.gethostname(), socket.getfqdn()}:
        try:
            for info in socket.getaddrinfo(hostname, None):
                candidate = normalized_ip_address(info[4][0])
                if candidate is not None:
                    local_addresses.add(candidate)
        except OSError:
            continue
    return address in local_addresses


def service_status(config: dict) -> dict:
    result = {}
    llama_mayhem = bool(config.get("llama_mayhem", False))
    for service_id, spec in service_definitions(config).items():
        managed = False
        listening = False
        if "port" in spec:
            listening = bool(listener_pids(spec["port"]))
            managed = validated_managed_service_record(service_id, spec) is not None
        live = url_is_live(spec["health_url"])
        result[service_id] = {
            "id": service_id,
            "name": spec["name"],
            "live": live,
            "open_url": spec["open_url"],
            "managed": managed,
            "can_start": "script" in spec and not listening,
            "can_stop": managed or (llama_mayhem and listening and "script" in spec),
            "can_restart": managed or (llama_mayhem and listening and "script" in spec),
            "control_note": (
                "Llama Mayhem can forcibly stop any process listening on this port"
                if llama_mayhem and listening and "script" in spec
                else "Managed by this Launchpad installation"
                if managed
                else "Connected externally; stop it manually once to hand control to Launchpad"
                if live and "script" in spec
                else "Status only"
                if "script" not in spec
                else "Ready to start"
            ),
        }
    return result


def system_resources() -> dict:
    ram = None
    if os.name == "nt":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = (
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            )

        memory = MEMORYSTATUSEX()
        memory.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        kernel32 = ctypes.windll.kernel32
        kernel32.GlobalMemoryStatusEx.argtypes = (ctypes.POINTER(MEMORYSTATUSEX),)
        kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            ram = {
                "used_mib": round((memory.ullTotalPhys - memory.ullAvailPhys) / 1_048_576),
                "total_mib": round(memory.ullTotalPhys / 1_048_576),
                "percent": int(memory.dwMemoryLoad),
            }

    vram = None
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            completed = subprocess.run(
                [nvidia_smi, "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            values = []
            for line in completed.stdout.splitlines():
                parts = [part.strip() for part in line.split(",")]
                if len(parts) == 2:
                    values.append((int(parts[0]), int(parts[1])))
            if values:
                used = sum(item[0] for item in values)
                total = sum(item[1] for item in values)
                vram = {
                    "used_mib": used,
                    "total_mib": total,
                    "percent": round(used * 100 / total) if total else 0,
                    "gpu_count": len(values),
                }
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return {"ram": ram, "vram": vram, "updated_at": utc_now()}


def listener_pids(port: int) -> list[int]:
    completed = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    pattern = re.compile(rf":{port}\s+\S+\s+LISTENING\s+(\d+)\s*$", re.IGNORECASE)
    return sorted({int(match.group(1)) for line in completed.stdout.splitlines() if (match := pattern.search(line))})


def start_service(spec: dict) -> subprocess.Popen:
    if not POWERSHELL_EXE.is_file():
        raise FileNotFoundError(f"PowerShell not found: {POWERSHELL_EXE}")
    if not spec["script"].is_file():
        raise FileNotFoundError(f"Service launcher not found: {spec['script']}")
    spec["stdout"].parent.mkdir(parents=True, exist_ok=True)
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with spec["stdout"].open("ab", buffering=0) as stdout, spec["stderr"].open("ab", buffering=0) as stderr:
        return subprocess.Popen(
            [
                str(POWERSHELL_EXE),
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", str(spec["script"]),
            ],
            cwd=str(spec["working_directory"]),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creation_flags,
        )


def wait_for_managed_service(service_id: str, spec: dict, launcher: subprocess.Popen, timeout: float = 30.0) -> dict:
    launcher_identity = process_identity(launcher.pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for pid in listener_pids(spec["port"]):
            listener_identity = process_identity(pid)
            if listener_identity:
                record = {
                    "service_id": service_id,
                    "port": int(spec["port"]),
                    "script": str(spec["script"]),
                    "listener": listener_identity,
                }
                if launcher_identity:
                    record["launcher"] = launcher_identity
                update_managed_service_record(service_id, record)
                return record
        return_code = launcher.poll()
        if return_code is not None:
            raise RuntimeError(f"{spec['name']} launcher exited with code {return_code} before opening port {spec['port']}")
        time.sleep(0.25)
    raise RuntimeError(f"{spec['name']} did not open port {spec['port']} within {timeout:g} seconds")


def stop_managed_service(service_id: str, spec: dict, record: dict) -> None:
    listener = record["listener"]
    listener_pid = int(listener["pid"])
    if listener_pid not in listener_pids(spec["port"]) or not process_identity_matches(listener_pid, listener):
        update_managed_service_record(service_id, None)
        raise RuntimeError(f"Refusing to stop {spec['name']} because its recorded process identity no longer matches")
    target = listener
    launcher = record.get("launcher")
    if isinstance(launcher, dict):
        try:
            launcher_pid = int(launcher["pid"])
        except (KeyError, TypeError, ValueError):
            launcher_pid = 0
        if launcher_pid and process_identity_matches(launcher_pid, launcher):
            target = launcher
    target_pid = int(target["pid"])
    def terminate_tree(pid: int) -> None:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode not in {0, 128}:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"Unable to stop PID {pid}")
        else:
            os.kill(pid, 15)

    terminate_tree(target_pid)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and listener_pid in listener_pids(spec["port"]):
        time.sleep(0.2)
    if (
        target_pid != listener_pid
        and listener_pid in listener_pids(spec["port"])
        and process_identity_matches(listener_pid, listener)
    ):
        terminate_tree(listener_pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and listener_pid in listener_pids(spec["port"]):
            time.sleep(0.2)
    if listener_pid in listener_pids(spec["port"]):
        raise RuntimeError(f"{spec['name']} did not release port {spec['port']}")
    update_managed_service_record(service_id, None)


def stop_service_listeners(spec: dict) -> list[int]:
    pids = listener_pids(spec["port"])
    for pid in pids:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode not in {0, 128}:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"Unable to stop PID {pid}")
        else:
            os.kill(pid, 15)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and listener_pids(spec["port"]):
        time.sleep(0.2)
    if listener_pids(spec["port"]):
        raise RuntimeError(f"{spec['name']} did not release port {spec['port']}")
    return pids


def control_service(config: dict, service_id: str, action: str) -> dict:
    if action not in {"start", "stop", "restart"}:
        raise ValueError("Service action must be start, stop, or restart")
    definitions = service_definitions(config)
    if service_id not in definitions:
        raise KeyError("Unknown service")
    spec = definitions[service_id]
    llama_mayhem = bool(config.get("llama_mayhem", False))
    record = validated_managed_service_record(service_id, spec)
    listening = bool(listener_pids(spec["port"]))
    if record is None and load_managed_service_state()["services"].get(service_id) is not None:
        update_managed_service_record(service_id, None)
    if action == "start" and listening:
        return service_status(config)[service_id]
    if action in {"stop", "restart"} and listening and llama_mayhem:
        stopped_pids = stop_service_listeners(spec)
        update_managed_service_record(service_id, None)
        LOGGER.warning(
            "Llama Mayhem action=%s service=%s terminated_listener_pids=%s",
            action,
            service_id,
            stopped_pids,
        )
        if action == "stop":
            return service_status(config)[service_id]
        record = None
        listening = False
    if action in {"stop", "restart"} and listening and record is None:
        raise RuntimeError(
            f"Refusing to {action} {spec['name']} because the listener was not started by this Launchpad installation"
        )
    if action == "stop":
        if record is None:
            raise RuntimeError(f"{spec['name']} is not running as a managed service")
        stop_managed_service(service_id, spec, record)
        LOGGER.info("Service action=stop service=%s pid=%s", service_id, record["listener"]["pid"])
        return service_status(config)[service_id]
    if action == "restart" and record is not None:
        stop_managed_service(service_id, spec, record)
    launcher = start_service(spec)
    record = wait_for_managed_service(service_id, spec, launcher)
    LOGGER.info("Service action=%s service=%s launcher=%s", action, service_id, spec["script"])
    return service_status(config)[service_id]


def configure_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("local-model-launchpad")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            LOG_DIR / "web-launcher.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


LOGGER = configure_logging()


def normalize_options(value) -> dict:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("Options must be a JSON object")
    unknown = set(value).difference(CUSTOM_OPTION_SPECS)
    if unknown:
        raise ValueError(f"Unknown options: {', '.join(sorted(unknown))}")

    normalized: dict[str, bool | int | float | str] = {}
    for name, raw_value in value.items():
        if raw_value is None:
            continue
        spec = CUSTOM_OPTION_SPECS[name]
        if spec["type"] == "gpu_layers":
            if raw_value == "auto":
                normalized[name] = raw_value
            elif isinstance(raw_value, int) and not isinstance(raw_value, bool) and 0 <= raw_value <= 1000:
                normalized[name] = raw_value
            else:
                raise ValueError("gpu_layers must be auto or a whole number between 0 and 1000")
            continue
        if spec["type"] is bool:
            if not isinstance(raw_value, bool):
                raise ValueError(f"{name} must be true or false")
            normalized[name] = raw_value
            continue
        if spec["type"] is str:
            if not isinstance(raw_value, str) or raw_value not in spec["choices"]:
                raise ValueError(f"{name} must be one of: {', '.join(spec['choices'])}")
            normalized[name] = raw_value
            continue
        if isinstance(raw_value, bool):
            raise ValueError(f"{name} must be numeric")
        if spec["type"] is int:
            if not isinstance(raw_value, int):
                raise ValueError(f"{name} must be a whole number")
            converted = raw_value
        else:
            if not isinstance(raw_value, (int, float)):
                raise ValueError(f"{name} must be numeric")
            converted = float(raw_value)
            if not math.isfinite(converted):
                raise ValueError(f"{name} must be finite")
        if converted < spec["minimum"] or converted > spec["maximum"]:
            raise ValueError(f"{name} must be between {spec['minimum']} and {spec['maximum']}")
        normalized[name] = converted
    return normalized


def render_number(value: int | float) -> str:
    return str(value) if isinstance(value, int) else format(value, ".15g")


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    required = {"host", "port", "allowed_networks", "models_file", "server", "environment"}
    missing = required.difference(config)
    if missing:
        raise ValueError(f"Missing config keys: {', '.join(sorted(missing))}")

    server = config["server"]
    server_required = {
        "executable", "host", "port", "device", "gpu_layers", "fit", "flash_attention",
        "parallel", "batch_size", "ubatch_size", "timeout", "extra_args",
    }
    missing_server = server_required.difference(server)
    if missing_server:
        raise ValueError(f"Missing server config keys: {', '.join(sorted(missing_server))}")
    server["executable"] = validate_server_executable(server["executable"])
    if not isinstance(server["device"], str) or not server["device"].strip():
        raise ValueError("server.device must be 'auto', 'none', or a device reported by llama-server --list-devices")
    server["device"] = server["device"].strip()
    if not isinstance(server["extra_args"], list) or not all(isinstance(x, str) for x in server["extra_args"]):
        raise ValueError("server.extra_args must be a list of strings")
    authentication = config.get("authentication", {"enabled": False})
    if not isinstance(authentication, dict) or not isinstance(authentication.get("enabled", False), bool):
        raise ValueError("authentication.enabled must be true or false")
    if authentication.get("enabled", False):
        username = authentication.get("username")
        encoded_password = authentication.get("password_hash")
        if not isinstance(username, str) or not username.strip() or ":" in username:
            raise ValueError("authentication.username must be a non-empty name without a colon")
        if not isinstance(encoded_password, str) or not encoded_password.startswith("scrypt$"):
            raise ValueError("authentication.password_hash must be generated by setup.py")
        authentication = {
            "enabled": True,
            "username": username.strip(),
            "password_hash": encoded_password,
        }
    else:
        authentication = {"enabled": False, "username": "", "password_hash": ""}
    config["authentication"] = authentication
    user_settings = load_user_settings(server["executable"])
    server["executable"] = user_settings["llama_server_executable"]
    config["openwebui_enabled"] = user_settings["openwebui_enabled"]
    config["openwebui_root"] = user_settings["openwebui_root"]
    config["openwebui_url"] = user_settings["openwebui_url"]
    config["openterminal_url"] = user_settings["openterminal_url"]
    config["vane_enabled"] = user_settings["vane_enabled"]
    config["vane_url"] = user_settings["vane_url"]
    config["llama_mayhem"] = user_settings["llama_mayhem"]
    config["_networks"] = [ipaddress.ip_network(value, strict=False) for value in config["allowed_networks"]]
    config["_models_path"] = (APP_DIR / config["models_file"]).resolve()
    return config


class ModelCardPresetLibrary:
    def __init__(self, path: Path):
        self.path = path
        self.entries: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self.reload()

    def reload(self) -> None:
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("version") != 1 or not isinstance(data.get("entries"), list):
            raise ValueError("preset-library.json must contain version 1 and an entries list")
        entries: list[dict] = []
        ids: set[str] = set()
        for entry in data["entries"]:
            entry_id = entry.get("id")
            if not isinstance(entry_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,95}", entry_id):
                raise ValueError(f"Invalid preset library id: {entry_id!r}")
            if entry_id in ids:
                raise ValueError(f"Duplicate preset library id: {entry_id}")
            ids.add(entry_id)
            if not isinstance(entry.get("name"), str) or not entry["name"].strip():
                raise ValueError(f"{entry_id}.name is required")
            source = entry.get("source")
            if not isinstance(source, dict) or not all(isinstance(source.get(key), str) and source[key].strip() for key in ("publisher", "url", "checked_at")):
                raise ValueError(f"{entry_id}.source must contain publisher, url, and checked_at")
            parsed_source = urlparse(source["url"])
            if parsed_source.scheme != "https" or not parsed_source.netloc:
                raise ValueError(f"{entry_id}.source.url must be an HTTPS address")
            patterns = entry.get("patterns")
            if not isinstance(patterns, list) or not patterns or not all(isinstance(pattern, str) and pattern for pattern in patterns):
                raise ValueError(f"{entry_id}.patterns must be a non-empty list")
            for pattern in patterns:
                re.compile(pattern, re.IGNORECASE)
            preset_status = entry.get("preset_status", "documented")
            if preset_status not in {"documented", "reference", "no-documented-preset"}:
                raise ValueError(f"{entry_id}.preset_status is invalid")
            profiles = entry.get("profiles")
            if not isinstance(profiles, list):
                raise ValueError(f"{entry_id}.profiles must be a list")
            if not profiles and preset_status != "no-documented-preset":
                raise ValueError(f"{entry_id}.profiles may be empty only when no documented preset exists")
            if profiles and preset_status == "no-documented-preset":
                raise ValueError(f"{entry_id} cannot contain profiles when preset_status is no-documented-preset")
            profile_names: set[str] = set()
            for profile in profiles:
                for key in ("name", "mode", "sampling"):
                    if key not in profile:
                        raise ValueError(f"A profile in {entry_id} is missing {key}")
                if not isinstance(profile["name"], str) or not profile["name"].strip() or profile["name"] in profile_names:
                    raise ValueError(f"{entry_id} contains an invalid or duplicate profile name")
                profile_names.add(profile["name"])
                if not isinstance(profile["mode"], str) or not profile["mode"].strip():
                    raise ValueError(f"{entry_id}.{profile['name']}.mode is required")
                sampling = profile["sampling"]
                if not isinstance(sampling, dict) or not sampling or not set(sampling).issubset(SAMPLING_KEYS):
                    raise ValueError(f"{entry_id}.{profile['name']}.sampling must contain documented sampling fields only")
                normalize_options(sampling)
                if "reasoning" in profile and profile["reasoning"] not in {"on", "off", "auto"}:
                    raise ValueError(f"{entry_id}.{profile['name']}.reasoning must be on, off, or auto")
                generation = profile.get("generation", {})
                if not isinstance(generation, dict) or not set(generation).issubset(GENERATION_KEYS):
                    raise ValueError(f"{entry_id}.{profile['name']}.generation contains unsupported fields")
                normalize_options(generation)
            entries.append(copy.deepcopy(entry))
        self.entries = entries
        self._by_id = {entry["id"]: entry for entry in entries}

    @staticmethod
    def _haystacks(model_path: str, name: str = "", mmproj_path: str = "") -> tuple[str, str]:
        del name  # Display names are intentionally not trusted for preset matching.
        return (
            model_path.replace("\\", "/").lower(),
            mmproj_path.replace("\\", "/").lower(),
        )

    def match(self, model_path: str, name: str = "", mmproj_path: str = "") -> dict | None:
        model_haystack, projector_haystack = self._haystacks(model_path, name, mmproj_path)
        for entry in self.entries:
            if any(re.search(pattern, model_haystack, re.IGNORECASE) for pattern in entry["patterns"]):
                return copy.deepcopy(entry)
        if projector_haystack:
            for entry in self.entries:
                if any(re.search(pattern, projector_haystack, re.IGNORECASE) for pattern in entry["patterns"]):
                    return copy.deepcopy(entry)
        return None

    def match_for_id(self, preset_id: str, model_path: str, name: str = "", mmproj_path: str = "") -> dict | None:
        entry = self._by_id.get(preset_id)
        if entry is None:
            return None
        model_haystack, projector_haystack = self._haystacks(model_path, name, mmproj_path)
        if not any(
            re.search(pattern, haystack, re.IGNORECASE)
            for haystack in (model_haystack, projector_haystack)
            if haystack
            for pattern in entry["patterns"]
        ):
            return None
        return copy.deepcopy(entry)

    def public_match(self, model_path: str, name: str = "", mmproj_path: str = "") -> dict | None:
        entry = self.match(model_path, name, mmproj_path)
        if entry is None:
            return None
        return {
            "id": entry["id"],
            "name": entry["name"],
            "preset_status": entry.get("preset_status", "documented"),
            "source": entry["source"],
            "profiles": [
                {
                    "name": profile["name"],
                    "mode": profile["mode"],
                    "reasoning": profile.get("reasoning"),
                    "sampling": profile["sampling"],
                    "generation": profile.get("generation", {}),
                }
                for profile in entry["profiles"]
            ],
        }


class ModelRegistry:
    def __init__(self, path: Path, preset_library: ModelCardPresetLibrary):
        self.path = path
        self.preset_library = preset_library
        self.backup_dir = path.parent / "registry-backups"
        self.lock = threading.RLock()
        self.data: dict = {}
        self._profiles: dict[str, tuple[dict, dict]] = {}
        self.reload()

    @staticmethod
    def _validate_path(raw_value, label: str, required: bool = True) -> str | None:
        if raw_value in (None, "") and not required:
            return None
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ValueError(f"{label} is required")
        path = Path(raw_value.strip())
        if not path.is_absolute():
            raise ValueError(f"{label} must be an absolute path")
        if path.suffix.lower() != ".gguf":
            raise ValueError(f"{label} must point to a .gguf file")
        if not path.is_file():
            raise FileNotFoundError(f"{label} not found: {path}")
        return str(path)

    @staticmethod
    def _validate_extra_args(value, label: str) -> None:
        if value is None:
            return
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{label} must be a list of strings")

    def _validate_model(self, model: dict, profile_ids: set[str]) -> None:
        for key in ("id", "name", "family", "model_path", "alias", "quant", "context", "profiles"):
            if key not in model:
                raise ValueError(f"Model is missing {key}")
        if not isinstance(model["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", model["id"]):
            raise ValueError(f"Invalid model id: {model.get('id')!r}")
        self._validate_path(model["model_path"], "Model path")
        self._validate_path(model.get("mmproj_path"), "Projector path", required=False)
        normalize_options({"context": model["context"]})
        self._validate_extra_args(model.get("extra_args"), f"{model['id']}.extra_args")
        environment = model.get("environment", {})
        if not isinstance(environment, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in environment.items()):
            raise ValueError(f"{model['id']}.environment must contain string values")
        if not isinstance(model["profiles"], list) or not model["profiles"]:
            raise ValueError(f"{model['id']} must contain at least one profile")

        for profile in model["profiles"]:
            for key in ("id", "name", "mode", "vision", "reasoning", "sampling"):
                if key not in profile:
                    raise ValueError(f"A profile in {model['id']} is missing {key}")
            profile_id = profile["id"]
            if not isinstance(profile_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,95}", profile_id):
                raise ValueError(f"Invalid profile id: {profile_id!r}")
            if profile_id in profile_ids:
                raise ValueError(f"Duplicate profile id: {profile_id}")
            profile_ids.add(profile_id)
            if not isinstance(profile["vision"], bool):
                raise ValueError(f"{profile_id}.vision must be true or false")
            if profile["vision"] and not model.get("mmproj_path"):
                raise ValueError(f"{profile_id} enables vision without a projector")
            if profile["reasoning"] not in {"on", "off", "auto"}:
                raise ValueError(f"{profile_id}.reasoning must be on, off, or auto")
            normalize_options(generation_defaults(profile))
            if set(profile["sampling"]) != set(SAMPLING_KEYS):
                raise ValueError(f"{profile_id}.sampling must contain all supported sampling fields")
            normalize_options(profile["sampling"])
            if "context" in profile:
                normalize_options({"context": profile["context"]})
            if "performance" in profile:
                if not isinstance(profile["performance"], dict) or set(profile["performance"]) != set(PERFORMANCE_KEYS):
                    raise ValueError(f"{profile_id}.performance must contain all supported performance fields")
                performance = normalize_options(profile["performance"])
                if performance["ubatch_size"] > performance["batch_size"]:
                    raise ValueError(f"{profile_id}.performance ubatch_size cannot exceed batch_size")
            self._validate_extra_args(profile.get("extra_args"), f"{profile_id}.extra_args")

    def reload(self) -> None:
        with self.lock:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if data.get("version") != 1 or not isinstance(data.get("models"), list):
                raise ValueError("models.json must contain version 1 and a models list")
            model_ids: set[str] = set()
            profile_ids: set[str] = set()
            profiles: dict[str, tuple[dict, dict]] = {}
            for model in data["models"]:
                if model.get("id") in model_ids:
                    raise ValueError(f"Duplicate model id: {model.get('id')}")
                self._validate_model(model, profile_ids)
                model_ids.add(model["id"])
                for profile in model["profiles"]:
                    profiles[profile["id"]] = (model, profile)
            self.data = data
            self._profiles = profiles

    def catalog(self) -> list[dict]:
        with self.lock:
            items: list[dict] = []
            for model in self.data["models"]:
                for profile in model["profiles"]:
                    recommended = {
                        "context": profile.get("context", model["context"]),
                        **profile["sampling"],
                    }
                    projector_path = model.get("mmproj_path")
                    items.append({
                        "id": profile["id"],
                        "model_id": model["id"],
                        "family": model["family"],
                        "group": model["name"],
                        "name": f"{model['name']} — {profile['name']}",
                        "mode": profile["mode"],
                        "quant": model["quant"],
                        "vision": profile["vision"],
                        "projector": Path(projector_path).name if projector_path else None,
                        "recommended": recommended,
                        "generation": generation_defaults(profile),
                        "performance": copy.deepcopy(profile.get("performance")),
                        "customizable": True,
                        "source": model.get("source", "built-in"),
                    })
            return items

    def resolve(self, profile_id: str) -> tuple[dict, dict]:
        with self.lock:
            pair = self._profiles.get(profile_id)
            if pair is None:
                raise KeyError("Unknown profile")
            return copy.deepcopy(pair[0]), copy.deepcopy(pair[1])

    def update_profile_performance(self, profile_id: str, raw_performance) -> dict:
        if not isinstance(raw_performance, dict) or set(raw_performance) != set(PERFORMANCE_KEYS):
            raise ValueError("Performance preset must contain every supported performance field")
        performance = normalize_options(raw_performance)
        if performance["ubatch_size"] > performance["batch_size"]:
            raise ValueError("ubatch_size cannot be greater than batch_size")

        with self.lock:
            if profile_id not in self._profiles:
                raise KeyError("Unknown profile")
            updated = copy.deepcopy(self.data)
            updated_profile = None
            profile_ids: set[str] = set()
            for model in updated["models"]:
                for profile in model["profiles"]:
                    if profile["id"] == profile_id:
                        profile["performance"] = performance
                        updated_profile = profile
                self._validate_model(model, profile_ids)
            if updated_profile is None:
                raise KeyError("Unknown profile")

            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            shutil.copy2(self.path, self.backup_dir / f"models-{stamp}.json")
            temp_path = self.path.with_suffix(".json.tmp")
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(updated, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            self.reload()
            LOGGER.info("Updated performance preset: profile=%s", profile_id)
            return next(item for item in self.catalog() if item["id"] == profile_id)

    def save_profile_settings(self, profile_id: str, action: str, raw_settings, profile_name=None) -> dict:
        if action not in {"overwrite", "new"}:
            raise ValueError("action must be overwrite or new")
        if not isinstance(raw_settings, dict) or set(raw_settings) != {"vision", "context", "sampling", "generation", "performance"}:
            raise ValueError("Preset settings must include vision, context, sampling, generation, and performance")
        if not isinstance(raw_settings["vision"], bool):
            raise ValueError("vision must be true or false")
        context = normalize_options({"context": raw_settings["context"]})["context"]
        if not isinstance(raw_settings["sampling"], dict) or set(raw_settings["sampling"]) != set(SAMPLING_KEYS):
            raise ValueError("sampling must contain all supported sampling fields")
        sampling = normalize_options(raw_settings["sampling"])
        if not isinstance(raw_settings["generation"], dict) or set(raw_settings["generation"]) != set(GENERATION_KEYS):
            raise ValueError("generation must contain all supported reasoning and output fields")
        generation = normalize_options(raw_settings["generation"])
        if not isinstance(raw_settings["performance"], dict) or set(raw_settings["performance"]) != set(PERFORMANCE_KEYS):
            raise ValueError("performance must contain all supported performance fields")
        performance = normalize_options(raw_settings["performance"])
        if performance["ubatch_size"] > performance["batch_size"]:
            raise ValueError("ubatch_size cannot be greater than batch_size")
        if action == "new":
            if not isinstance(profile_name, str) or not profile_name.strip() or len(profile_name.strip()) > 60:
                raise ValueError("A new preset name is required and must be at most 60 characters")
            profile_name = profile_name.strip()

        with self.lock:
            if profile_id not in self._profiles:
                raise KeyError("Unknown profile")
            updated = copy.deepcopy(self.data)
            saved_id = profile_id
            found = False
            existing_ids = {profile["id"] for model in updated["models"] for profile in model["profiles"]}
            for model in updated["models"]:
                for index, profile in enumerate(list(model["profiles"])):
                    if profile["id"] != profile_id:
                        continue
                    found = True
                    if raw_settings["vision"] and not model.get("mmproj_path"):
                        raise ValueError("Image input cannot be enabled without a projector")
                    saved_profile = copy.deepcopy(profile)
                    if action == "new":
                        base_id = f"{profile_id[:54]}-{slugify(profile_name)}"[:88].rstrip("-")
                        saved_id = base_id
                        suffix = 2
                        while saved_id in existing_ids:
                            saved_id = f"{base_id[:89]}-{suffix}"
                            suffix += 1
                        saved_profile["id"] = saved_id
                        saved_profile["name"] = profile_name
                        saved_profile["mode"] = profile_name
                    saved_profile["vision"] = raw_settings["vision"]
                    saved_profile["context"] = context
                    saved_profile["sampling"] = {key: sampling[key] for key in SAMPLING_KEYS}
                    saved_profile["reasoning"] = generation["reasoning"]
                    saved_profile["n_predict"] = generation["n_predict"]
                    saved_profile["reasoning_budget"] = generation["reasoning_budget"]
                    saved_profile["reasoning_preserve"] = generation["reasoning_preserve"]
                    saved_profile["performance"] = {key: performance[key] for key in PERFORMANCE_KEYS}
                    if action == "new":
                        model["profiles"].append(saved_profile)
                    else:
                        model["profiles"][index] = saved_profile
                    break
                if found:
                    break
            if not found:
                raise KeyError("Unknown profile")

            profile_ids: set[str] = set()
            for model in updated["models"]:
                self._validate_model(model, profile_ids)
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            shutil.copy2(self.path, self.backup_dir / f"models-{stamp}.json")
            temp_path = self.path.with_suffix(".json.tmp")
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(updated, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            self.reload()
            LOGGER.info("Saved full preset: source=%s saved=%s action=%s", profile_id, saved_id, action)
            return next(item for item in self.catalog() if item["id"] == saved_id)

    def remove_model(self, model_id: str) -> dict:
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("A model id is required")
        with self.lock:
            removed = next((model for model in self.data["models"] if model["id"] == model_id), None)
            if removed is None:
                raise KeyError("Unknown model")
            updated = copy.deepcopy(self.data)
            updated["models"] = [model for model in updated["models"] if model["id"] != model_id]
            profile_ids: set[str] = set()
            for model in updated["models"]:
                self._validate_model(model, profile_ids)

            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            shutil.copy2(self.path, self.backup_dir / f"models-{stamp}.json")
            temp_path = self.path.with_suffix(".json.tmp")
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(updated, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            self.reload()
            LOGGER.info("Removed model from launcher registry only: id=%s name=%s", model_id, removed["name"])
            return {"id": model_id, "name": removed["name"]}

    def update_model_group(self, model_id: str, raw_group) -> dict:
        if not isinstance(raw_group, str) or not raw_group.strip() or len(raw_group.strip()) > 60:
            raise ValueError("Group is required and must be at most 60 characters")
        group = raw_group.strip()
        with self.lock:
            updated = copy.deepcopy(self.data)
            model = next((item for item in updated["models"] if item["id"] == model_id), None)
            if model is None:
                raise KeyError("Unknown model")
            model["family"] = group
            profile_ids: set[str] = set()
            for item in updated["models"]:
                self._validate_model(item, profile_ids)
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            shutil.copy2(self.path, self.backup_dir / f"models-{stamp}.json")
            temp_path = self.path.with_suffix(".json.tmp")
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(updated, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            self.reload()
            LOGGER.info("Updated model group: id=%s group=%s", model_id, group)
            return {"id": model_id, "name": model["name"], "group": group}

    def add_model(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Model definition must be a JSON object")
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError("Model name is required and must be at most 100 characters")
        family = payload.get("family", "Custom")
        if not isinstance(family, str) or not family.strip() or len(family.strip()) > 60:
            raise ValueError("Family is required and must be at most 60 characters")
        alias = payload.get("alias") or slugify(name)
        quant = payload.get("quant") or "Custom"
        profile_name = payload.get("profile_name") or "Default"
        for label, value, limit in (("Alias", alias, 100), ("Quant", quant, 60), ("Profile name", profile_name, 60)):
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
                raise ValueError(f"{label} is invalid")

        model_path = self._validate_path(payload.get("model_path"), "Model path")
        mmproj_path = self._validate_path(payload.get("mmproj_path"), "Projector path", required=False)
        vision = payload.get("vision", False)
        no_mmap = payload.get("no_mmap", False)
        if not isinstance(vision, bool) or not isinstance(no_mmap, bool):
            raise ValueError("vision and no_mmap must be true or false")
        if vision and not mmproj_path:
            raise ValueError("Vision requires a projector path")

        options = normalize_options(payload.get("defaults", {}))
        context = options.pop("context", 32768)
        sampling = dict(DEFAULT_SAMPLING)
        sampling.update(options)
        reasoning = payload.get("reasoning", "auto")
        if reasoning not in {"on", "off", "auto"}:
            raise ValueError("reasoning must be on, off, or auto")
        preset_id = payload.get("preset_id")
        if preset_id is not None and (not isinstance(preset_id, str) or not preset_id):
            raise ValueError("preset_id must be a non-empty string")
        preset = self.preset_library.match_for_id(preset_id, model_path, name.strip(), mmproj_path or "") if preset_id else None
        if preset_id and preset is None:
            raise ValueError("The selected creator preset does not match this model")

        with self.lock:
            existing_model_ids = {model["id"] for model in self.data["models"]}
            existing_profile_ids = set(self._profiles)
            base_id = slugify(name)
            model_id = base_id
            suffix = 2
            while model_id in existing_model_ids:
                model_id = f"{base_id[:58]}-{suffix}"
                suffix += 1
            used_profile_ids = set(existing_profile_ids)

            def unique_profile_id(label: str) -> str:
                profile_base = f"user-{model_id[:48]}-{slugify(label)[:35]}"[:88].rstrip("-")
                candidate = profile_base
                profile_suffix = 2
                while candidate in used_profile_ids:
                    candidate = f"{profile_base[:89]}-{profile_suffix}"
                    profile_suffix += 1
                used_profile_ids.add(candidate)
                return candidate

            profiles: list[dict] = []
            if preset and preset["profiles"]:
                for library_profile in preset["profiles"]:
                    profile_sampling = dict(sampling)
                    profile_sampling.update(normalize_options(library_profile["sampling"]))
                    profile = {
                        "id": unique_profile_id(library_profile["name"]),
                        "name": library_profile["name"],
                        "mode": library_profile["mode"],
                        "vision": vision,
                        "reasoning": library_profile.get("reasoning", reasoning),
                        "sampling": profile_sampling,
                        "preset_source": "creator-reference" if preset.get("preset_status") == "reference" else "creator-model-card",
                    }
                    profile.update(normalize_options(library_profile.get("generation", {})))
                    if no_mmap:
                        profile["no_mmap"] = True
                    profiles.append(profile)
            else:
                profile = {
                    "id": unique_profile_id("default"),
                    "name": profile_name.strip(),
                    "mode": profile_name.strip(),
                    "vision": vision,
                    "reasoning": reasoning,
                    "sampling": sampling,
                }
                if no_mmap:
                    profile["no_mmap"] = True
                profiles.append(profile)

            model = {
                "id": model_id,
                "name": name.strip(),
                "family": family.strip(),
                "model_path": model_path,
                "alias": alias.strip(),
                "quant": quant.strip(),
                "context": context,
                "source": "user",
                "profiles": profiles,
            }
            if mmproj_path:
                model["mmproj_path"] = mmproj_path
            if preset:
                model["preset_library_id"] = preset["id"]
                model["preset_source_url"] = preset["source"]["url"]
            profile_ids = set(existing_profile_ids)
            self._validate_model(model, profile_ids)

            updated = copy.deepcopy(self.data)
            updated["models"].append(model)
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            shutil.copy2(self.path, self.backup_dir / f"models-{stamp}.json")
            temp_path = self.path.with_suffix(".json.tmp")
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(updated, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            self.reload()
            first_profile_id = profiles[0]["id"]
            LOGGER.info("Added model: id=%s profiles=%s preset=%s path=%s", model_id, len(profiles), preset_id, model_path)
            return next(item for item in self.catalog() if item["id"] == first_profile_id)


def is_port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.35):
            return True
    except OSError:
        return False


def process_image_path(pid: int) -> str | None:
    if os.name != "nt":
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return None
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def same_executable(pid: int, expected_path: str) -> bool:
    actual_path = process_image_path(pid)
    if not actual_path:
        return False
    return os.path.normcase(os.path.abspath(actual_path)) == os.path.normcase(os.path.abspath(expected_path))


def llama_server_process_ids() -> list[int]:
    if os.name != "nt":
        return []

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return []
    result = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        available = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while available:
            if entry.szExeFile.casefold() == "llama-server.exe":
                result.append(int(entry.th32ProcessID))
            available = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return sorted(set(result))


def listening_ports_by_pid() -> dict[int, list[int]]:
    completed = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    result: dict[int, set[int]] = {}
    pattern = re.compile(r"^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$", re.IGNORECASE)
    for line in completed.stdout.splitlines():
        match = pattern.match(line)
        if match:
            result.setdefault(int(match.group(2)), set()).add(int(match.group(1)))
    return {pid: sorted(ports) for pid, ports in result.items()}


def process_creation_marker(pid: int) -> int | None:
    if os.name != "nt":
        try:
            return int(Path(f"/proc/{pid}").stat().st_ctime_ns)
        except OSError:
            return None

    class FILETIME(ctypes.Structure):
        _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        created = FILETIME()
        exited = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return (int(created.high) << 32) | int(created.low)
    finally:
        kernel32.CloseHandle(handle)


def process_identity(pid: int) -> dict | None:
    executable = process_image_path(pid)
    created = process_creation_marker(pid)
    if not executable or created is None:
        return None
    return {"pid": int(pid), "executable": executable, "created": created}


def process_identity_matches(pid: int, expected: dict) -> bool:
    try:
        expected_executable = str(expected["executable"])
        expected_created = int(expected["created"])
    except (KeyError, TypeError, ValueError):
        return False
    return same_executable(pid, expected_executable) and process_creation_marker(pid) == expected_created


def process_exit_code(pid: int) -> int | None:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return None
        except OSError:
            return 0
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return 0
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return 0
        return None if exit_code.value == 259 else int(exit_code.value)
    finally:
        kernel32.CloseHandle(handle)


class RecoveredProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        exit_code = process_exit_code(self.pid)
        if exit_code is not None:
            self.returncode = exit_code
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
            time.sleep(0.1)
        return int(self.returncode or 0)

    def kill(self) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(self.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.kill(self.pid, 9)


class LaunchManager:
    def __init__(self, config: dict, registry: ModelRegistry):
        self.config = config
        self.registry = registry
        self.lock = threading.RLock()
        self.process: subprocess.Popen | RecoveredProcess | None = None
        self.log_handle = None
        self.current: dict | None = None
        self.last: dict | None = None
        self._last_discovery = 0.0
        self._last_external_port_check = 0.0
        self._recover_active_session()

    def _clear_active_session(self) -> None:
        ACTIVE_MODEL_PATH.unlink(missing_ok=True)

    def _persist_active_session(self) -> None:
        if not self.current:
            self._clear_active_session()
            return
        payload = {
            "version": 1,
            "server_port": int(self.config["server"]["port"]),
            "executable": self.config["server"]["executable"],
            "current": self.current,
        }
        temp_path = ACTIVE_MODEL_PATH.with_suffix(".json.tmp")
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, ACTIVE_MODEL_PATH)
        except Exception:
            temp_path.unlink(missing_ok=True)
            LOGGER.exception("Unable to persist active model session")

    def _recover_active_session(self) -> None:
        if self.config.get("llama_mayhem", False):
            self._recover_mayhem_session()
        else:
            self._recover_managed_session()

    def _recover_managed_session(self) -> None:
        if not ACTIVE_MODEL_PATH.is_file():
            return
        try:
            with ACTIVE_MODEL_PATH.open("r", encoding="utf-8") as handle:
                saved = json.load(handle)
            current = saved.get("current")
            if not isinstance(current, dict):
                raise ValueError("missing current process state")
            pid = int(current.get("pid", 0))
            server_port = int(self.config["server"]["port"])
            expected_executable = self.config["server"]["executable"]
            saved_executable = str(saved.get("executable", ""))
            identity = {
                "executable": current.get("process_executable"),
                "created": current.get("process_started_marker"),
            }
            if (
                saved.get("version") != 1
                or int(saved.get("server_port", -1)) != server_port
                or os.path.normcase(os.path.abspath(saved_executable))
                != os.path.normcase(os.path.abspath(expected_executable))
                or not current.get("owned")
                or int(current.get("port", -1)) != server_port
                or pid not in listener_pids(server_port)
                or not process_identity_matches(pid, identity)
            ):
                raise ValueError("saved process identity no longer matches the configured listener")
            current["recovered"] = True
            self.process = RecoveredProcess(pid)
            self.current = current
            self._persist_active_session()
            LOGGER.info("Recovered managed llama-server: profile=%s pid=%s", current.get("id"), pid)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            LOGGER.warning("Discarding stale or unverifiable active-model state")
            self.process = None
            self.current = None
            self._clear_active_session()

    def _restore_mayhem_process(self, current: dict, pid: int, source: str, *, owned: bool = False) -> bool:
        profile_id = current.get("id")
        if profile_id is not None:
            try:
                self.registry.resolve(profile_id)
            except KeyError:
                return False
        restored = dict(current)
        restored.pop("status", None)
        restored["pid"] = pid
        restored["started_epoch"] = float(restored.get("started_epoch", time.time()))
        restored.setdefault(
            "started_at",
            datetime.fromtimestamp(restored["started_epoch"], timezone.utc).isoformat(),
        )
        restored.setdefault("custom_options", {})
        restored.setdefault("resolved_options", {})
        restored.setdefault("process_executable", process_image_path(pid))
        restored.setdefault("process_started_marker", process_creation_marker(pid))
        restored["owned"] = owned
        restored["recovered"] = True
        self.process = RecoveredProcess(pid)
        self.current = restored
        self._persist_active_session()
        LOGGER.warning(
            "Llama Mayhem adopted llama-server: profile=%s pid=%s source=%s owned=%s",
            profile_id,
            pid,
            source,
            owned,
        )
        return True

    def _recover_mayhem_session(self) -> None:
        server_port = int(self.config["server"]["port"])
        expected_executable = self.config["server"]["executable"]
        ports_by_pid = listening_ports_by_pid()
        llama_pids = set(llama_server_process_ids())
        verified = {
            pid
            for pid in llama_pids
            if server_port in ports_by_pid.get(pid, []) and same_executable(pid, expected_executable)
        }
        if not llama_pids:
            self._clear_active_session()
            return

        if ACTIVE_MODEL_PATH.is_file():
            try:
                with ACTIVE_MODEL_PATH.open("r", encoding="utf-8") as handle:
                    saved = json.load(handle)
                current = saved.get("current", {})
                pid = int(current.get("pid", 0))
                external = bool(current.get("external"))
                process_valid = pid in llama_pids if external else pid in llama_pids and same_executable(pid, expected_executable)
                identity = {
                    "executable": current.get("process_executable"),
                    "created": current.get("process_started_marker"),
                }
                safely_owned = bool(
                    current.get("owned")
                    and int(current.get("port", -1)) == server_port
                    and pid in listener_pids(server_port)
                    and process_identity_matches(pid, identity)
                )
                if (
                    saved.get("version") == 1
                    and int(saved.get("server_port", -1)) == server_port
                    and os.path.normcase(os.path.abspath(str(saved.get("executable", ""))))
                    == os.path.normcase(os.path.abspath(expected_executable))
                    and process_valid
                    and self._restore_mayhem_process(current, pid, "state", owned=safely_owned)
                ):
                    return
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                LOGGER.warning("Unable to restore active model session in Llama Mayhem")
            self._clear_active_session()

        launcher_log = LOG_DIR / "web-launcher.log"
        if launcher_log.is_file():
            with launcher_log.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 2_097_152), os.SEEK_SET)
                recent = handle.read().decode("utf-8", errors="replace")
            starts = re.findall(r"Started profile=([^\s]+) pid=(\d+)", recent)
            for profile_id, raw_pid in reversed(starts):
                pid = int(raw_pid)
                if pid not in verified:
                    continue
                try:
                    _, resolved, details = self.build_command(profile_id)
                except (KeyError, FileNotFoundError, ValueError):
                    continue
                model = details["model"]
                profile = details["profile"]
                candidates = sorted(
                    LOG_DIR.glob(f"model-{profile_id}-*.log"),
                    key=lambda item: item.stat().st_ctime,
                    reverse=True,
                )
                log_path = candidates[0] if candidates else None
                timestamp_match = re.search(r"-(\d{8}-\d{6})\.log$", log_path.name) if log_path else None
                started_epoch = (
                    datetime.strptime(timestamp_match.group(1), "%Y%m%d-%H%M%S").timestamp()
                    if timestamp_match
                    else log_path.stat().st_ctime
                    if log_path
                    else time.time()
                )
                current = {
                    "id": profile_id,
                    "model_id": model["id"],
                    "name": f"{model['name']} — {profile['name']}",
                    "group": model["name"],
                    "mode": profile["mode"],
                    "vision": resolved["vision"],
                    "pid": pid,
                    "port": server_port,
                    "started_at": datetime.fromtimestamp(started_epoch, timezone.utc).isoformat(),
                    "started_epoch": started_epoch,
                    "log_file": str(log_path) if log_path else None,
                    "custom_options": {},
                    "resolved_options": resolved,
                }
                if self._restore_mayhem_process(current, pid, "launch-log"):
                    return

        candidates = sorted(
            llama_pids,
            key=lambda pid: (
                server_port not in ports_by_pid.get(pid, []),
                not bool(ports_by_pid.get(pid)),
                pid,
            ),
        )
        pid = candidates[0]
        ports = ports_by_pid.get(pid, [])
        detected_port = server_port if server_port in ports else ports[0] if ports else None
        mode = f"Detected external process · port {detected_port}" if detected_port else "Detected external process · not listening"
        self._restore_mayhem_process(
            {
                "id": None,
                "model_id": None,
                "name": "External llama-server",
                "group": "External",
                "mode": mode,
                "vision": False,
                "pid": pid,
                "port": detected_port,
                "external": True,
                "process_executable": process_image_path(pid),
                "started_at": utc_now(),
                "started_epoch": time.time(),
                "log_file": None,
                "custom_options": {},
                "resolved_options": {},
            },
            pid,
            "process-scan",
        )

    def _refresh_locked(self) -> None:
        if self.process is None:
            return
        return_code = self.process.poll()
        if return_code is None:
            return
        finished = dict(self.current or {})
        finished.update(status="exited", return_code=return_code, finished_at=utc_now())
        self.last = finished
        LOGGER.info("Model process exited: profile=%s pid=%s code=%s", finished.get("id"), finished.get("pid"), return_code)
        self._close_log_locked()
        self.process = None
        self.current = None
        self._clear_active_session()

    def _refresh_external_port_locked(self) -> None:
        if not self.config.get("llama_mayhem", False) or not self.current or not self.current.get("external"):
            return
        now = time.monotonic()
        if now - self._last_external_port_check < 2.0:
            return
        self._last_external_port_check = now
        ports = listening_ports_by_pid().get(int(self.current["pid"]), [])
        server_port = int(self.config["server"]["port"])
        detected_port = server_port if server_port in ports else ports[0] if ports else None
        if detected_port == self.current.get("port"):
            return
        self.current["port"] = detected_port
        self.current["mode"] = (
            f"Detected external process · port {detected_port}"
            if detected_port
            else "Detected external process · not listening"
        )
        self._persist_active_session()

    def set_llama_mayhem(self, enabled: bool) -> None:
        with self.lock:
            self.config["llama_mayhem"] = bool(enabled)
            if not enabled and self.current and not self.current.get("owned"):
                LOGGER.info("Llama Mayhem disabled; detaching unowned llama-server pid=%s", self.current.get("pid"))
                self._close_log_locked()
                self.process = None
                self.current = None
                self._clear_active_session()
            elif enabled and self.process is None:
                self._last_discovery = 0.0


    def _close_log_locked(self) -> None:
        if self.log_handle is not None:
            try:
                self.log_handle.flush()
                self.log_handle.close()
            finally:
                self.log_handle = None

    def status(self) -> dict:
        with self.lock:
            self._refresh_locked()
            now = time.monotonic()
            if self.config.get("llama_mayhem", False) and self.process is None and now - self._last_discovery >= 2.0:
                self._last_discovery = now
                self._recover_mayhem_session()
            self._refresh_external_port_locked()
            if self.process is None:
                return {"status": "idle", "last": self.last}
            result = dict(self.current or {})
            result["status"] = "running"
            result["elapsed_seconds"] = max(0, int(time.time() - result["started_epoch"]))
            result.pop("started_epoch", None)
            return result

    def build_command(self, profile_id: str, custom_options=None) -> tuple[list[str], dict, dict]:
        model, profile = self.registry.resolve(profile_id)
        options = normalize_options(custom_options)
        vision = options.get("vision", profile["vision"])
        context = options.get("context", profile.get("context", model["context"]))
        sampling = dict(profile["sampling"])
        for key in SAMPLING_KEYS:
            if key in options:
                sampling[key] = options[key]
        generation = generation_defaults(profile)
        for key in GENERATION_KEYS:
            if key in options:
                generation[key] = options[key]

        model_path = Path(model["model_path"])
        if not model_path.is_file():
            raise FileNotFoundError(f"Model not found: {model_path}")
        if vision:
            mmproj = Path(model.get("mmproj_path", ""))
            if not mmproj.is_file():
                raise FileNotFoundError(f"Projector not found: {mmproj}")

        server = self.config["server"]
        performance = performance_defaults(server)
        performance.update(profile.get("performance", {}))
        for key in performance:
            if key in options:
                performance[key] = options[key]
        if performance["ubatch_size"] > performance["batch_size"]:
            raise ValueError("ubatch_size cannot be greater than batch_size")
        command = [server["executable"], "--model", str(model_path)]
        if vision:
            command.extend(("--mmproj", model["mmproj_path"]))
        command.extend((
            "--alias", model["alias"],
            "--host", server["host"],
            "--port", str(server["port"]),
            "-c", str(context),
        ))
        if server["device"].casefold() != "auto":
            command.extend(("--device", server["device"]))
        command.extend((
            "-ngl", str(performance["gpu_layers"]),
            "--fit", performance["fit"],
            "--fit-target", str(performance["fit_target"]),
            "--flash-attn", performance["flash_attention"],
            "--cache-type-k", performance["cache_type_k"],
            "--cache-type-v", performance["cache_type_v"],
            "--parallel", str(performance["parallel"]),
            "--batch-size", str(performance["batch_size"]),
            "--ubatch-size", str(performance["ubatch_size"]),
            "--temp", render_number(sampling["temperature"]),
            "--top-p", render_number(sampling["top_p"]),
            "--top-k", render_number(sampling["top_k"]),
            "--min-p", render_number(sampling["min_p"]),
            "--presence-penalty", render_number(sampling["presence_penalty"]),
            "--repeat-penalty", render_number(sampling["repeat_penalty"]),
            "--predict", str(generation["n_predict"]),
            "--reasoning", generation["reasoning"],
            "--reasoning-budget", str(generation["reasoning_budget"]),
            "--timeout", str(server["timeout"]),
        ))
        if generation["reasoning_preserve"] == "on":
            command.append("--reasoning-preserve")
        elif generation["reasoning_preserve"] == "off":
            command.append("--no-reasoning-preserve")
        if profile.get("no_mmap"):
            command.append("--no-mmap")
        command.extend(server.get("extra_args", []))
        command.extend(model.get("extra_args", []))
        command.extend(profile.get("extra_args", []))
        resolved = {"vision": vision, "context": context, **sampling, "generation": generation, "performance": performance}
        return command, resolved, {"model": model, "profile": profile, "custom": options}

    def launch(self, profile_id: str, custom_options=None) -> dict:
        with self.lock:
            self._refresh_locked()
            if self.process is not None:
                raise RuntimeError(f"{self.current['name']} is already running")
            server_port = int(self.config["server"]["port"])
            if is_port_listening(server_port):
                raise RuntimeError(f"Port {server_port} is already in use")

            command, resolved, details = self.build_command(profile_id, custom_options)
            model = details["model"]
            profile = details["profile"]
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            log_path = LOG_DIR / f"model-{profile_id}-{timestamp}.log"
            self.log_handle = log_path.open("ab", buffering=0)
            environment = os.environ.copy()
            environment.update(self.config.get("environment", {}))
            environment.update(model.get("environment", {}))
            working_directory = str(Path(self.config["server"]["executable"]).parent)
            display_command = subprocess.list2cmdline(command)
            argument_lines = "\n".join(
                f"  [{index:02d}] {subprocess.list2cmdline([argument])}"
                for index, argument in enumerate(command[1:], start=1)
            )
            launch_header = (
                "=== Launchpad launch details ===\n"
                f"Profile: {model['name']} - {profile['name']}\n"
                f"Working directory: {working_directory}\n"
                f"Command: {display_command}\n"
                "Arguments passed to llama-server:\n"
                f"{argument_lines}\n"
                "=== llama-server output ===\n"
            )
            self.log_handle.write(launch_header.encode("utf-8", errors="replace"))
            creation_flags = 0
            if os.name == "nt":
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            try:
                process = subprocess.Popen(
                    command,
                    cwd=working_directory,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=self.log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=creation_flags,
                )
            except Exception:
                self._close_log_locked()
                raise

            self.process = process
            self.current = {
                "id": profile_id,
                "model_id": model["id"],
                "name": f"{model['name']} — {profile['name']}",
                "group": model["name"],
                "mode": profile["mode"],
                "vision": resolved["vision"],
                "pid": process.pid,
                "process_executable": self.config["server"]["executable"],
                "process_started_marker": process_creation_marker(process.pid),
                "port": server_port,
                "started_at": utc_now(),
                "started_epoch": time.time(),
                "log_file": str(log_path),
                "custom_options": details["custom"],
                "resolved_options": resolved,
                "owned": True,
                "recovered": False,
            }
            self._persist_active_session()
            LOGGER.info("Started profile=%s pid=%s command=%r", profile_id, process.pid, command)
            return self.status()

    def stop(self) -> dict:
        with self.lock:
            self._refresh_locked()
            if self.process is None:
                raise RuntimeError("No model is running")
            process = self.process
            current = dict(self.current or {})
            llama_mayhem = bool(self.config.get("llama_mayhem", False))
            if not llama_mayhem and not current.get("owned"):
                raise RuntimeError("Refusing to stop a process that was not started by this Launchpad instance")
            if process is None or not isinstance(getattr(process, "pid", None), int):
                raise RuntimeError("Refusing to stop an invalid process record")
            expected_executable = current.get("process_executable")
            if not llama_mayhem and os.name == "nt" and (
                not isinstance(expected_executable, str)
                or not same_executable(process.pid, expected_executable)
            ):
                raise RuntimeError("Refusing to stop the process because its executable identity could not be verified")
            if not llama_mayhem and current.get("recovered") and process_creation_marker(process.pid) != current.get("process_started_marker"):
                raise RuntimeError("Refusing to stop the recovered process because its start identity no longer matches")
            LOGGER.warning(
                "Stopping profile=%s pid=%s llama_mayhem=%s owned=%s",
                current.get("id"),
                process.pid,
                llama_mayhem,
                current.get("owned"),
            )
            if os.name == "nt":
                result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                if result.returncode not in (0, 128):
                    raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Unable to stop process")
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            current.update(status="stopped", return_code=process.returncode, finished_at=utc_now())
            self.last = current
            self._close_log_locked()
            self.process = None
            self.current = None
            self._clear_active_session()
            return {"status": "idle", "last": self.last}

    def recent_log(self, line_count: int) -> dict:
        with self.lock:
            self._refresh_locked()
            source = self.current or self.last
            if not source or not source.get("log_file"):
                return {"log": "", "file": None}
            path = Path(source["log_file"])
        if not path.is_file():
            return {"log": "", "file": str(path)}
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 262_144), os.SEEK_SET)
            content = handle.read().decode("utf-8", errors="replace")
        return {"log": "\n".join(content.splitlines()[-line_count:]), "file": str(path)}


class LauncherHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, config: dict, registry: ModelRegistry, manager: LaunchManager, token: str):
        super().__init__(address, handler)
        self.config = config
        self.registry = registry
        self.manager = manager
        self.token = token
        self.settings_lock = threading.RLock()
        self.service_lock = threading.RLock()
        self.file_picker_lock = threading.Lock()


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "LocalModelLaunchpad/2.0"

    def log_message(self, fmt: str, *args) -> None:
        LOGGER.info("%s %s", self.client_address[0], fmt % args)

    def _client_allowed(self) -> bool:
        try:
            address = ipaddress.ip_address(self.client_address[0])
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
            return any(address in network for network in self.server.config["_networks"])
        except ValueError:
            return False

    def _client_is_local_machine(self) -> bool:
        try:
            connection_local_address = self.connection.getsockname()[0]
        except OSError:
            connection_local_address = ""
        return is_local_machine_address(self.client_address[0], connection_local_address)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )

    def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: int, value: dict | list) -> None:
        self._send_bytes(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _authorize(self) -> bool:
        if not self._client_allowed():
            LOGGER.warning("Rejected request from %s", self.client_address[0])
            self._error(HTTPStatus.FORBIDDEN, "This address is not permitted")
            return False
        authentication = self.server.config["authentication"]
        if not authentication["enabled"]:
            return True
        header = self.headers.get("Authorization", "")
        try:
            scheme, supplied = header.split(" ", 1)
            if scheme.casefold() != "basic":
                raise ValueError
            decoded = base64.b64decode(supplied, validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            username, password = "", ""
        if hmac.compare_digest(username, authentication["username"]) and password_matches(
            password, authentication["password_hash"]
        ):
            return True
        LOGGER.warning("Rejected unauthenticated request from %s", self.client_address[0])
        payload = json.dumps({"error": "Authentication required"}).encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self._security_headers()
        self.send_header("WWW-Authenticate", 'Basic realm="Local Model Launchpad", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        return False

    def _check_token(self) -> bool:
        supplied = self.headers.get("X-Launcher-Token", "")
        if supplied and hmac.compare_digest(supplied, self.server.token):
            return True
        self._error(HTTPStatus.FORBIDDEN, "Invalid request token")
        return False

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length < 0 or length > 32768:
            raise ValueError("Request body is too large")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def do_GET(self) -> None:
        if not self._authorize():
            return
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/api/session":
            self._json(HTTPStatus.OK, {
                "token": self.server.token,
                "allowed_networks": self.server.config["allowed_networks"],
                "model_port": self.server.config["server"]["port"],
                "openwebui_enabled": self.server.config["openwebui_enabled"],
                "openwebui_url": self.server.config["openwebui_url"],
                "openterminal_url": self.server.config["openterminal_url"],
                "vane_enabled": self.server.config["vane_enabled"],
                "vane_url": self.server.config["vane_url"],
                "performance_defaults": performance_defaults(self.server.config["server"]),
                "cache_types": CACHE_TYPES,
                "self_contained": True,
                "local_file_picker": os.name == "nt" and self._client_is_local_machine(),
            })
            return
        if route == "/api/settings":
            self._json(HTTPStatus.OK, {
                "openwebui_enabled": self.server.config["openwebui_enabled"],
                "openwebui_root": self.server.config["openwebui_root"],
                "openwebui_url": self.server.config["openwebui_url"],
                "openterminal_url": self.server.config["openterminal_url"],
                "vane_enabled": self.server.config["vane_enabled"],
                "vane_url": self.server.config["vane_url"],
                "llama_server_executable": self.server.config["server"]["executable"],
                "llama_mayhem": self.server.config["llama_mayhem"],
                "settings_file": str(SETTINGS_PATH),
            })
            return
        if route == "/api/services":
            self._json(HTTPStatus.OK, service_status(self.server.config))
            return
        if route == "/api/resources":
            self._json(HTTPStatus.OK, system_resources())
            return
        if route == "/api/preset-library/match":
            query = parse_qs(parsed.query)
            model_path = query.get("model_path", [""])[0].strip()
            name = query.get("name", [""])[0].strip()
            mmproj_path = query.get("mmproj_path", [""])[0].strip()
            if not model_path or len(model_path) > 1000 or len(mmproj_path) > 1000 or len(name) > 100:
                self._error(HTTPStatus.BAD_REQUEST, "A valid model_path is required")
                return
            self._json(HTTPStatus.OK, {"match": self.server.registry.preset_library.public_match(model_path, name, mmproj_path)})
            return
        if route == "/api/catalog":
            self._json(HTTPStatus.OK, self.server.registry.catalog())
            return
        if route == "/api/status":
            self._json(HTTPStatus.OK, self.server.manager.status())
            return
        if route == "/api/log":
            try:
                requested = int(parse_qs(parsed.query).get("lines", ["160"])[0])
            except ValueError:
                requested = 160
            self._json(HTTPStatus.OK, self.server.manager.recent_log(max(20, min(requested, 400))))
            return
        static_files = {
            "/": (STATIC_DIR / "index.html", "text/html; charset=utf-8"),
            "/settings": (STATIC_DIR / "settings.html", "text/html; charset=utf-8"),
            "/settings/": (STATIC_DIR / "settings.html", "text/html; charset=utf-8"),
            "/styles.css": (STATIC_DIR / "styles.css", "text/css; charset=utf-8"),
            "/app.js": (STATIC_DIR / "app.js", "text/javascript; charset=utf-8"),
            "/settings.js": (STATIC_DIR / "settings.js", "text/javascript; charset=utf-8"),
            "/favicon.svg": (STATIC_DIR / "favicon.svg", "image/svg+xml"),
            "/favicon-launchpad.svg": (STATIC_DIR / "favicon.svg", "image/svg+xml"),
        }
        item = static_files.get(route)
        if item is None or not item[0].is_file():
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        self._send_bytes(HTTPStatus.OK, item[0].read_bytes(), item[1])

    def do_POST(self) -> None:
        if not self._authorize() or not self._check_token():
            return
        route = urlparse(self.path).path
        try:
            body = self._read_json()
            service_match = re.fullmatch(r"/api/services/(openwebui|openterminal)/(start|stop|restart)", route)
            if service_match:
                with self.server.service_lock:
                    item = control_service(self.server.config, service_match.group(1), service_match.group(2))
                self._json(HTTPStatus.ACCEPTED, item)
                return
            if route == "/api/settings":
                openwebui_enabled = body.get("openwebui_enabled")
                if not isinstance(openwebui_enabled, bool):
                    raise ValueError("openwebui_enabled must be true or false")
                vane_enabled = body.get("vane_enabled")
                if not isinstance(vane_enabled, bool):
                    raise ValueError("vane_enabled must be true or false")
                llama_mayhem = body.get("llama_mayhem")
                if not isinstance(llama_mayhem, bool):
                    raise ValueError("llama_mayhem must be true or false")
                settings = {
                    "openwebui_enabled": openwebui_enabled,
                    "openwebui_root": normalize_service_root(body.get("openwebui_root")),
                    "openwebui_url": normalize_service_url(body.get("openwebui_url"), "OpenWebUI"),
                    "openterminal_url": normalize_service_url(body.get("openterminal_url"), "OpenTerminal"),
                    "vane_enabled": vane_enabled,
                    "vane_url": normalize_service_url(body.get("vane_url"), "Vane"),
                    "llama_server_executable": validate_server_executable(body.get("llama_server_executable")),
                    "llama_mayhem": llama_mayhem,
                }
                with self.server.settings_lock:
                    persist_user_settings(settings)
                    self.server.config["openwebui_enabled"] = settings["openwebui_enabled"]
                    self.server.config["openwebui_root"] = settings["openwebui_root"]
                    self.server.config["openwebui_url"] = settings["openwebui_url"]
                    self.server.config["openterminal_url"] = settings["openterminal_url"]
                    self.server.config["vane_enabled"] = settings["vane_enabled"]
                    self.server.config["vane_url"] = settings["vane_url"]
                    self.server.config["server"]["executable"] = settings["llama_server_executable"]
                    self.server.manager.set_llama_mayhem(settings["llama_mayhem"])
                LOGGER.info("Updated user settings: openwebui_enabled=%s openwebui_root=%s openwebui=%s openterminal=%s vane_enabled=%s vane=%s executable=%s llama_mayhem=%s", settings["openwebui_enabled"], settings["openwebui_root"], settings["openwebui_url"], settings["openterminal_url"], settings["vane_enabled"], settings["vane_url"], settings["llama_server_executable"], settings["llama_mayhem"])
                self._json(HTTPStatus.OK, {**settings, "settings_file": str(SETTINGS_PATH)})
                return
            if route == "/api/launch":
                profile_id = body.get("id")
                if not isinstance(profile_id, str):
                    raise ValueError("A profile id is required")
                self._json(HTTPStatus.ACCEPTED, self.server.manager.launch(profile_id, body.get("options")))
                return
            if route == "/api/stop":
                self._json(HTTPStatus.OK, self.server.manager.stop())
                return
            if route == "/api/file-picker":
                if not self._client_is_local_machine():
                    self._error(HTTPStatus.FORBIDDEN, "The native file picker is available only on the Launchpad computer")
                    return
                kind = body.get("kind")
                if kind not in {"model", "projector"}:
                    raise ValueError("kind must be model or projector")
                initial_path = body.get("initial_path", "")
                if not isinstance(initial_path, str) or len(initial_path) > 1000:
                    raise ValueError("initial_path is invalid")
                title = "Choose model GGUF" if kind == "model" else "Choose projector GGUF"
                with self.server.file_picker_lock:
                    path = choose_gguf_file(
                        initial_path,
                        registered_model_directory(self.server.registry),
                        title,
                    )
                self._json(HTTPStatus.OK, {"path": path})
                return
            if route == "/api/models":
                item = self.server.registry.add_model(body)
                self._json(HTTPStatus.CREATED, {"model": item, "catalog": self.server.registry.catalog()})
                return
            if route == "/api/models/remove":
                if self.server.manager.status()["status"] == "running":
                    raise RuntimeError("Stop the running model before removing launcher entries")
                item = self.server.registry.remove_model(body.get("id"))
                self._json(HTTPStatus.OK, {"model": item, "catalog": self.server.registry.catalog()})
                return
            if route == "/api/models/group":
                item = self.server.registry.update_model_group(body.get("id"), body.get("group"))
                self._json(HTTPStatus.OK, {"model": item, "catalog": self.server.registry.catalog()})
                return
            if route == "/api/profiles":
                profile_id = body.get("id")
                if not isinstance(profile_id, str):
                    raise ValueError("A profile id is required")
                item = self.server.registry.save_profile_settings(
                    profile_id,
                    body.get("action"),
                    body.get("settings"),
                    body.get("name"),
                )
                self._json(HTTPStatus.OK, {"profile": item, "catalog": self.server.registry.catalog()})
                return
            if route == "/api/profiles/performance":
                profile_id = body.get("id")
                if not isinstance(profile_id, str):
                    raise ValueError("A profile id is required")
                item = self.server.registry.update_profile_performance(profile_id, body.get("performance"))
                self._json(HTTPStatus.OK, {"profile": item, "catalog": self.server.registry.catalog()})
                return
            self._error(HTTPStatus.NOT_FOUND, "Not found")
        except KeyError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        except (ValueError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except RuntimeError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except FileNotFoundError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        except Exception as exc:
            LOGGER.exception("Request failed")
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))


if os.name == "nt":
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = (
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        )

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
        )


class WindowsTrayIcon:
    WM_TRAY = 0x8001
    WM_COMMAND = 0x0111
    WM_CLOSE = 0x0010
    WM_DESTROY = 0x0002
    WM_RBUTTONUP = 0x0205
    WM_CONTEXTMENU = 0x007B
    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    MF_STRING = 0x00000000
    MF_SEPARATOR = 0x00000800
    TPM_RIGHTBUTTON = 0x0002
    TPM_BOTTOMALIGN = 0x0020
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    LR_DEFAULTSIZE = 0x0040
    CMD_OPEN = 1001
    CMD_STOP = 1002
    CMD_QUIT = 1003

    def __init__(self, url: str, icon_path: Path, on_stop, on_quit) -> None:
        self.url = url
        self.icon_path = icon_path
        self.on_stop = on_stop
        self.on_quit = on_quit
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.hwnd = None
        self.hicon = None
        self.notify_data = None
        self._wndproc = None

    def start(self) -> None:
        if os.name != "nt":
            return
        self.thread = threading.Thread(target=self._run, name="launchpad-tray", daemon=True)
        self.thread.start()
        self.ready.wait(timeout=5)

    def stop(self) -> None:
        if os.name == "nt" and self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, self.WM_CLOSE, 0, 0)

    def _remove_icon(self) -> None:
        if self.notify_data is not None:
            ctypes.windll.shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self.notify_data))
            self.notify_data = None

    def _show_menu(self) -> None:
        user32 = ctypes.windll.user32
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            user32.AppendMenuW(menu, self.MF_STRING, self.CMD_OPEN, "Open Launchpad")
            user32.AppendMenuW(menu, self.MF_STRING, self.CMD_STOP, "Stop server")
            user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, self.MF_STRING, self.CMD_QUIT, "Quit")
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self.hwnd)
            user32.TrackPopupMenu(
                menu,
                self.TPM_RIGHTBUTTON | self.TPM_BOTTOMALIGN,
                point.x,
                point.y,
                0,
                self.hwnd,
                None,
            )
        finally:
            user32.DestroyMenu(menu)

    def _run(self) -> None:
        try:
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            kernel32 = ctypes.windll.kernel32
            kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
            user32.RegisterClassW.restype = wintypes.ATOM
            user32.CreateWindowExW.argtypes = (
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                wintypes.HMENU,
                wintypes.HINSTANCE,
                wintypes.LPVOID,
            )
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.LoadImageW.argtypes = (
                wintypes.HINSTANCE,
                wintypes.LPCWSTR,
                wintypes.UINT,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            )
            user32.LoadImageW.restype = wintypes.HANDLE
            user32.LoadIconW.argtypes = (wintypes.HINSTANCE, ctypes.c_void_p)
            user32.LoadIconW.restype = wintypes.HICON
            user32.CreatePopupMenu.restype = wintypes.HMENU
            user32.AppendMenuW.argtypes = (wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR)
            user32.AppendMenuW.restype = wintypes.BOOL
            user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
            user32.GetCursorPos.restype = wintypes.BOOL
            user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
            user32.SetForegroundWindow.restype = wintypes.BOOL
            user32.TrackPopupMenu.argtypes = (
                wintypes.HMENU,
                wintypes.UINT,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                wintypes.LPRECT,
            )
            user32.TrackPopupMenu.restype = wintypes.BOOL
            user32.DestroyMenu.argtypes = (wintypes.HMENU,)
            user32.DestroyMenu.restype = wintypes.BOOL
            user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            user32.PostMessageW.restype = wintypes.BOOL
            user32.DestroyWindow.argtypes = (wintypes.HWND,)
            user32.DestroyWindow.restype = wintypes.BOOL
            user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
            user32.GetMessageW.restype = wintypes.BOOL
            user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
            user32.TranslateMessage.restype = wintypes.BOOL
            user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
            user32.DispatchMessageW.restype = LRESULT
            user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            user32.DefWindowProcW.restype = LRESULT
            shell32.Shell_NotifyIconW.argtypes = (wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
            shell32.Shell_NotifyIconW.restype = wintypes.BOOL
            instance = kernel32.GetModuleHandleW(None)
            class_name = f"LocalModelLaunchpadTray_{os.getpid()}"

            @WNDPROC
            def window_proc(hwnd, message, wparam, lparam):
                if message == self.WM_TRAY and lparam in (self.WM_RBUTTONUP, self.WM_CONTEXTMENU):
                    self._show_menu()
                    return 0
                if message == self.WM_COMMAND:
                    command = int(wparam) & 0xFFFF
                    if command == self.CMD_OPEN:
                        webbrowser.open(self.url)
                        return 0
                    if command == self.CMD_STOP:
                        threading.Thread(
                            target=self.on_stop,
                            name="launchpad-stop-model",
                            daemon=True,
                        ).start()
                        return 0
                    if command == self.CMD_QUIT:
                        self._remove_icon()
                        threading.Thread(target=self.on_quit, name="launchpad-quit", daemon=True).start()
                        user32.DestroyWindow(hwnd)
                        return 0
                if message == self.WM_CLOSE:
                    self._remove_icon()
                    user32.DestroyWindow(hwnd)
                    return 0
                if message == self.WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(hwnd, message, wparam, lparam)

            self._wndproc = window_proc
            window_class = WNDCLASSW()
            window_class.lpfnWndProc = window_proc
            window_class.hInstance = instance
            window_class.lpszClassName = class_name
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError()
            self.hwnd = user32.CreateWindowExW(
                0, class_name, "Local Model Launchpad", 0,
                0, 0, 0, 0, None, None, instance, None,
            )
            if not self.hwnd:
                raise ctypes.WinError()
            self.hicon = user32.LoadImageW(
                None,
                str(self.icon_path),
                self.IMAGE_ICON,
                0,
                0,
                self.LR_LOADFROMFILE | self.LR_DEFAULTSIZE,
            )
            if not self.hicon:
                self.hicon = user32.LoadIconW(None, ctypes.c_void_p(32512))
            notify_data = NOTIFYICONDATAW()
            notify_data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            notify_data.hWnd = self.hwnd
            notify_data.uID = 1
            notify_data.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
            notify_data.uCallbackMessage = self.WM_TRAY
            notify_data.hIcon = self.hicon
            notify_data.szTip = "Local Model Launchpad"
            self.notify_data = notify_data
            if not shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(notify_data)):
                raise ctypes.WinError()
            LOGGER.info("Windows tray icon started")
            self.ready.set()
            message = wintypes.MSG()
            while True:
                message_result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if message_result == -1:
                    raise ctypes.WinError()
                if message_result == 0:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except Exception:
            LOGGER.exception("Unable to start Windows tray icon")
            self.ready.set()
        finally:
            self._remove_icon()
            self.hwnd = None


def stop_manager_if_running(manager: LaunchManager) -> None:
    try:
        if manager.status().get("status") == "running":
            manager.stop()
    except Exception:
        LOGGER.exception("Unable to stop active model during launcher shutdown")


def stop_model_from_tray(manager: LaunchManager) -> dict | None:
    try:
        result = manager.stop()
        LOGGER.info("Model server stopped from Windows tray")
        return result
    except RuntimeError as error:
        if str(error) == "No model is running":
            LOGGER.info("Tray stop requested with no model server running")
            return {"status": "idle"}
        LOGGER.exception("Unable to stop model server from Windows tray")
    except Exception:
        LOGGER.exception("Unable to stop model server from Windows tray")
    return None


def run() -> None:
    parser = argparse.ArgumentParser(description="Self-contained local model launchpad")
    parser.add_argument("--host", help="Override the configured bind address")
    parser.add_argument("--port", type=int, help="Override the configured HTTP port")
    parser.add_argument("--check", action="store_true", help="Validate configuration and exit")
    args = parser.parse_args()
    config = load_config()
    preset_library = ModelCardPresetLibrary(PRESET_LIBRARY_PATH)
    registry = ModelRegistry(config["_models_path"], preset_library)
    if args.check:
        print(json.dumps({
            "ok": True,
            "models": len(registry.data["models"]),
            "profiles": len(registry.catalog()),
            "preset_library_entries": len(preset_library.entries),
            "networks": config["allowed_networks"],
        }))
        return
    host = args.host or config["host"]
    port = args.port if args.port is not None else int(config["port"])
    acquired, mutex_handle = acquire_single_instance()
    if not acquired:
        LOGGER.info("This Launchpad installation is already running; opening the existing instance")
        webbrowser.open(f"http://127.0.0.1:{port}/")
        return
    atexit.register(release_single_instance, mutex_handle)
    manager = LaunchManager(config, registry)
    server = LauncherHTTPServer((host, port), RequestHandler, config, registry, manager, secrets.token_urlsafe(32))
    tray = None
    if os.name == "nt":
        def stop_from_tray() -> None:
            LOGGER.info("Stop-model requested from Windows tray")
            stop_model_from_tray(manager)

        def quit_from_tray() -> None:
            LOGGER.info("Quit requested from Windows tray")
            stop_manager_if_running(manager)
            server.shutdown()

        tray = WindowsTrayIcon(
            f"http://127.0.0.1:{port}/",
            STATIC_DIR / "launchpad.ico",
            stop_from_tray,
            quit_from_tray,
        )
        tray.start()
    PID_PATH.write_text(str(os.getpid()), encoding="ascii")
    atexit.register(lambda: PID_PATH.unlink(missing_ok=True))
    LOGGER.info("Self-contained Launchpad starting on %s:%s profiles=%s", host, port, len(registry.catalog()))
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        if tray is not None:
            tray.stop()
        stop_manager_if_running(manager)
        server.server_close()
        PID_PATH.unlink(missing_ok=True)
        LOGGER.info("Launchpad stopped")


if __name__ == "__main__":
    run()
