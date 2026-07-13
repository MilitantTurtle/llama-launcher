from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
MODELS_PATH = APP_DIR / "models.json"
AUTOMATIC_DEVICE = "auto"
CPU_DEVICE = "none"


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if not password:
        raise ValueError("Password cannot be empty")
    salt = salt or secrets.token_bytes(16)
    n, r, p = 16_384, 8, 1
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def resolve_llama_server(value: str | Path) -> Path:
    path = Path(str(value).strip().strip('"')).expanduser().resolve()
    if path.is_dir():
        path = path / "llama-server.exe"
    if path.name.casefold() != "llama-server.exe" or not path.is_file():
        raise FileNotFoundError("Choose the folder containing llama-server.exe")
    return path


def parse_device_list(output: str) -> list[tuple[str, str]]:
    devices: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in output.splitlines():
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_.-]*\d+):\s*(\S.*)$", line)
        if not match or match.group(1).casefold() in seen:
            continue
        device_id, description = match.group(1), match.group(2).strip()
        seen.add(device_id.casefold())
        devices.append((device_id, description))
    return devices


def detect_devices(executable: Path, timeout: float = 15.0) -> list[tuple[str, str]]:
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [str(executable), "--list-devices"],
            cwd=str(executable.parent),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_device_list(f"{completed.stdout}\n{completed.stderr}")


def build_config(
    executable: Path,
    username: str = "",
    password: str = "",
    device: str = AUTOMATIC_DEVICE,
) -> dict:
    username = username.strip()
    device = device.strip() or AUTOMATIC_DEVICE
    if bool(username) != bool(password):
        raise ValueError("Enter both a username and password, or leave both blank")
    if ":" in username:
        raise ValueError("Username cannot contain a colon")
    authentication = {"enabled": False, "username": "", "password_hash": ""}
    if username:
        authentication = {
            "enabled": True,
            "username": username,
            "password_hash": hash_password(password),
        }
    return {
        "host": "0.0.0.0",
        "port": 8766,
        "allowed_networks": [
            "127.0.0.0/8",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
        ],
        "models_file": "models.json",
        "authentication": authentication,
        "server": {
            "executable": str(executable),
            "host": "0.0.0.0",
            "port": 8000,
            "device": device,
            "gpu_layers": 0 if device.casefold() == CPU_DEVICE else "auto",
            "fit": "on",
            "flash_attention": "on",
            "parallel": 1,
            "batch_size": 512,
            "ubatch_size": 256,
            "timeout": 7200,
            "extra_args": [],
        },
        "environment": {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        },
    }


def write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def save_setup(
    server_location: str,
    username: str = "",
    password: str = "",
    device: str = AUTOMATIC_DEVICE,
) -> Path:
    executable = resolve_llama_server(server_location)
    write_json_atomic(CONFIG_PATH, build_config(executable, username, password, device))
    if not MODELS_PATH.exists():
        write_json_atomic(MODELS_PATH, {"version": 1, "models": []})
    return executable


def graphical_setup() -> bool:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError:
        return False

    root = tk.Tk()
    root.title("Local Model Launchpad - First Run")
    root.resizable(False, False)
    folder = tk.StringVar()
    automatic_label = "Automatic (recommended)"
    cpu_label = "CPU only (--device none)"
    device_choice = tk.StringVar(value=automatic_label)
    device_values = {automatic_label: AUTOMATIC_DEVICE, cpu_label: CPU_DEVICE}
    username = tk.StringVar()
    password = tk.StringVar()
    status = tk.StringVar(value="Choose the folder that contains llama-server.exe.")
    completed = {"value": False}

    frame = ttk.Frame(root, padding=18)
    frame.grid(sticky="nsew")
    ttk.Label(frame, text="llama.cpp server folder").grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Entry(frame, textvariable=folder, width=58).grid(row=1, column=0, padx=(0, 8), pady=(4, 10))

    def refresh_devices() -> None:
        try:
            executable = resolve_llama_server(folder.get())
        except (OSError, ValueError) as error:
            messagebox.showerror("Unable to inspect devices", str(error), parent=root)
            return
        status.set("Asking llama-server which compute devices are available...")
        root.update_idletasks()
        detected = detect_devices(executable)
        device_values.clear()
        device_values[automatic_label] = AUTOMATIC_DEVICE
        for device_id, description in detected:
            device_values[f"{device_id} - {description}"] = device_id
        device_values[cpu_label] = CPU_DEVICE
        device_box.configure(values=list(device_values))
        device_choice.set(automatic_label)
        status.set(
            f"Detected {len(detected)} accelerator device{'s' if len(detected) != 1 else ''}. "
            "Automatic is the safest default."
            if detected
            else "No accelerator was reported. Automatic remains available; CPU-only is the explicit fallback."
        )

    def browse() -> None:
        selected = filedialog.askdirectory(title="Choose the folder containing llama-server.exe")
        if selected:
            folder.set(selected)
            refresh_devices()

    ttk.Button(frame, text="Browse...", command=browse).grid(row=1, column=1, pady=(4, 10))
    ttk.Button(frame, text="Detect", command=refresh_devices).grid(row=1, column=2, pady=(4, 10), padx=(6, 0))
    ttk.Label(frame, text="Compute device").grid(row=2, column=0, columnspan=3, sticky="w")
    device_box = ttk.Combobox(frame, textvariable=device_choice, state="readonly", width=76)
    device_box.configure(values=list(device_values))
    device_box.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 14))
    ttk.Separator(frame).grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 14))
    ttk.Label(frame, text="Optional LAN login (leave both blank for no login)").grid(
        row=5, column=0, columnspan=3, sticky="w"
    )
    ttk.Label(frame, text="Username").grid(row=6, column=0, sticky="w", pady=(8, 2))
    ttk.Entry(frame, textvariable=username, width=32).grid(row=7, column=0, columnspan=3, sticky="w")
    ttk.Label(frame, text="Password").grid(row=8, column=0, sticky="w", pady=(8, 2))
    ttk.Entry(frame, textvariable=password, show="*", width=32).grid(row=9, column=0, columnspan=3, sticky="w")
    ttk.Label(frame, textvariable=status, foreground="#555555", wraplength=480).grid(
        row=10, column=0, columnspan=3, sticky="w", pady=(14, 10)
    )

    def save() -> None:
        try:
            selected_device = device_values.get(device_choice.get(), AUTOMATIC_DEVICE)
            executable = save_setup(folder.get(), username.get(), password.get(), selected_device)
        except (OSError, ValueError) as error:
            messagebox.showerror("Setup could not be saved", str(error), parent=root)
            return
        completed["value"] = True
        messagebox.showinfo(
            "Setup complete",
            f"Launchpad is configured to use:\n{executable}\n\nDevice: {selected_device}\n"
            "You can edit config.json at any time.",
            parent=root,
        )
        root.destroy()

    ttk.Button(frame, text="Save and continue", command=save).grid(row=11, column=0, columnspan=3, sticky="e")
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return completed["value"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Create Local Model Launchpad config.json")
    parser.add_argument("--llama-server", help="Folder containing llama-server.exe, or the executable itself")
    parser.add_argument("--username", default="", help="Optional HTTP Basic username")
    parser.add_argument("--password", default="", help="Optional HTTP Basic password")
    parser.add_argument("--device", default=AUTOMATIC_DEVICE, help="llama.cpp device id, 'auto', or 'none' for CPU")
    parser.add_argument("--hash-password", metavar="PASSWORD", help="Print a config-compatible password hash and exit")
    args = parser.parse_args()
    if args.hash_password is not None:
        print(hash_password(args.hash_password))
        return 0
    if args.llama_server:
        try:
            executable = save_setup(args.llama_server, args.username, args.password, args.device)
        except (OSError, ValueError) as error:
            print(f"Setup failed: {error}", file=sys.stderr)
            return 1
        print(f"Configured llama-server: {executable}")
        return 0
    try:
        if graphical_setup():
            return 0
    except Exception as error:
        print(f"Graphical setup failed: {error}", file=sys.stderr)
    print("Setup was cancelled. Run setup.py again, or copy config.example.json to config.json and edit it.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
