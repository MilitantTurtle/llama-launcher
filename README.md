# Local Model Launchpad

[![Tests](https://github.com/MilitantTurtle/llama-launcher/actions/workflows/tests.yml/badge.svg)](https://github.com/MilitantTurtle/llama-launcher/actions/workflows/tests.yml)

A Windows-first web launchpad for running local GGUF models with `llama-server.exe`. It includes a source-linked model preset library, model/profile management, live status and logs, and a Windows tray menu.

Configuration, model paths, credentials, logs, and other runtime state stay local and are excluded from version control.

## Licence

Local Model Launchpad is available under the [PolyForm Noncommercial License 1.0.0](LICENSE) (`PolyForm-Noncommercial-1.0.0`). You may use it, fork it, change it, and redistribute it for non-commercial purposes. Commercial use is not permitted.

Because the licence restricts commercial use, this project is source-available rather than OSI-approved open-source software.

## Screenshots

### Running dashboard

Monitor memory use, connected local services, and the active llama-server process from one place.

![Launchpad dashboard with a local model running](docs/images/launchpad-running.jpg)

### Model profiles and launch controls

Choose a task-specific preset, then review or edit its sampling, context, reasoning, and performance settings before launch.

![Expanded Qwen model card showing coding preset and launch settings](docs/images/launchpad-presets.jpg)

## Quick start

Requirements:

- Windows 10 or 11
- Python 3.11 or newer on `PATH`
- A recent llama.cpp build containing `llama-server.exe`

Right-click `start-launchpad.ps1`, choose **Run with PowerShell**, and complete the first-run window. Choose the folder containing `llama-server.exe`; setup asks that binary for its available compute devices and lets you use automatic selection, a reported accelerator, or CPU-only mode. You can also enter an optional username and password for LAN access.

The first run creates `config.json` and an empty `models.json`. Both are deliberately ignored by Git. The browser interface is available at `http://127.0.0.1:8766/` on the host machine.

You can also configure it without the window:

```powershell
python setup.py --llama-server "C:\llama.cpp\build\bin" --device auto
```

Or copy `config.example.json` to `config.json` and edit it manually. The path must point to an existing `llama-server.exe`.

## Adding models

Open **Add model** and choose an existing GGUF. When Launchpad is opened on its host Windows computer, the Browse buttons open a native file picker; remote LAN clients can still paste paths but cannot open a dialog on the host.

Choosing a model GGUF fills a readable model name and inferred family from the filename while preserving later manual edits. If the filename matches the bundled preset library, the form labels each profile value as **From preset**, **Your value**, **Fallback**, or **Ignored** so it is clear what importing creator profiles will replace. Turning preset import off marks the full profile as manual.

## LAN access and security

By default, both Launchpad and llama-server bind to `0.0.0.0`. Windows Firewall still controls whether another device can connect. Launchpad accepts loopback and RFC 1918 private addresses by default; edit `allowed_networks` to make that narrower.

The optional username/password gate uses HTTP Basic authentication with a salted scrypt hash in `config.json`; the password itself is not stored. Basic authentication over plain HTTP does **not** encrypt traffic or credentials. Treat it as a casual trusted-LAN gate. For untrusted networks, put the app behind HTTPS, a VPN, or an authenticated reverse proxy. Do not expose its ports directly to the internet.

To generate a replacement password hash:

```powershell
python setup.py --hash-password "your new password"
```

Place the printed value in `authentication.password_hash`, set a username, and set `authentication.enabled` to `true`.

## Configuration

- `config.json`: bind addresses, allowed networks, authentication, llama-server executable and launch defaults.
- `models.json`: generated local models and launch profiles. It starts empty and is ignored by Git so model paths remain local.
- `preset-library.json`: source-linked recommended profiles matched from model filenames.
- `settings.json`: optional OpenWebUI, OpenTerminal, LibreChat, Vane, and Llama Mayhem preferences. Integrations and unsafe process control default off; the file is created only when settings are saved.

Set `OPENWEBUI_ROOT` or `LIBRECHAT_ROOT` if you want the optional local service controls to use installations outside their default sibling folders. Set `LAUNCHPAD_PYTHON` if `python.exe` is not on `PATH`.

`server.device` accepts `auto`, `none` for CPU-only, or a device identifier reported by `llama-server.exe --list-devices`, such as `CUDA0` or `Vulkan0`. Automatic mode omits the `--device` argument and lets llama.cpp select from the backends compiled into that binary.

## Optional service controls

The settings page accepts the OpenWebUI installation folder containing `Start-OpenWebUI.ps1` and the LibreChat folder containing `Start-LibreChat.ps1`. OpenTerminal is expected in OpenWebUI's `OpenTerminal` subfolder. Once Launchpad starts one of these services, its Start, Stop, and Restart controls remain available across Launchpad restarts. Stopping LibreChat leaves its MongoDB Windows service running; LibreChat's startup script remains responsible for ensuring that dependency is available.

Launchpad records the exact launcher and listening-process IDs, executable paths, ports, and Windows process-creation identities in the ignored local file `managed-services.json`. It only stops a service while all of that identity still matches. A service that was already running externally remains visible as connected but unmanaged; stop it manually once, then use Launchpad's Start button to place it under managed control. Vane remains a status link because it may be remote.

## Llama Mayhem

**Llama Mayhem** is an opt-in unsafe process-control mode in Settings. It matches the more aggressive behavior some private installations use: Launchpad discovers and adopts external `llama-server.exe` processes, allows adopted processes to be stopped without ownership or creation-identity checks, and allows OpenWebUI, OpenTerminal, or LibreChat controls to force-stop any process listening on their configured ports.

The option is off by default and strict ownership remains the normal public behavior. Enable it only when the configured ports and every detected `llama-server.exe` are under your control. Turning it off detaches an adopted unowned model process without terminating it. Launching a model still refuses an occupied model-server port rather than automatically killing the listener.

## Duplicate-launch safety

Each installation owns a Windows named mutex derived from its absolute folder. Starting the same installation twice opens the existing interface and exits; it does not kill processes by executable name or by port. Separate copies in separate folders remain independent.

Launchpad persists the exact PID, executable path, listening port, and Windows process-creation identity for a `llama-server.exe` it starts. A restarted Launchpad can reclaim and stop that same verified process. Stale or mismatched records are discarded, and if a different process owns the configured model port, launching is refused without terminating that process.

## Development checks

```powershell
python -m unittest discover -s tests -v
python app.py --check
```

`app.py --check` requires a valid local `config.json` and an existing `llama-server.exe`.

The same compile and test checks run on `windows-latest` for every GitHub push and pull request.

## Wiring OpenWebUI and LibreChat service control

Launchpad can show any reachable OpenWebUI or LibreChat instance as connected, but Start, Stop, and Restart require a small PowerShell launcher inside each installation folder. Managed service control is intended for native Windows processes that stay attached to that script. Docker installations should normally be managed with Docker and used as status/Open links only; see [Docker installations](#docker-installations).

### Expected layout

By default, Launchpad looks for sibling folders:

```text
C:\Apps\
├── llama-launcher\
├── OpenWebUI\
│   └── Start-OpenWebUI.ps1
└── LibreChat\
    └── Start-LibreChat.ps1
```

The folders do not have to be siblings. Open **Launcher Settings**, enable **Show services button**, and set the absolute installation folders and addresses. The address fields accept an IP address and port, such as `127.0.0.1:8181` or `192.168.1.50:3080`; they do not accept a hostname, credentials, or an additional URL path.

The configured address controls three things: the health check, the Open link, and the TCP port whose listening process Launchpad records. It must therefore match the port opened by the service. Configure OpenWebUI to use port `8181` and LibreChat to use port `3080`, or change the corresponding address in Settings.

Environment variables can set the initial folder defaults before `settings.json` exists:

```powershell
$env:OPENWEBUI_ROOT = 'D:\Apps\OpenWebUI'
$env:LIBRECHAT_ROOT = 'D:\Apps\LibreChat'
.\start-launchpad.ps1
```

Saving Launcher Settings writes the selected folders to `settings.json`, after which those saved values take precedence.

### `Start-OpenWebUI.ps1` template

This template expects OpenWebUI to be installed in a `.venv` inside its installation folder. Adjust `$OpenWebUI` if your executable is elsewhere.

```powershell
$ErrorActionPreference = 'Stop'

$Root = $PSScriptRoot
$OpenWebUI = Join-Path $Root '.venv\Scripts\open-webui.exe'

if (-not (Test-Path -LiteralPath $OpenWebUI -PathType Leaf)) {
    throw "OpenWebUI executable not found: $OpenWebUI"
}

Set-Location -LiteralPath $Root
$env:DATA_DIR = Join-Path $Root 'data'

& $OpenWebUI serve --host 0.0.0.0 --port 8181
exit $LASTEXITCODE
```

Save it as `Start-OpenWebUI.ps1` in the folder selected as **OpenWebUI installation folder**. Keep the final command in the foreground: do not use `Start-Process` without `-Wait`.

### `Start-LibreChat.ps1` template

This template expects a native LibreChat checkout with `package.json`, dependencies already installed, and `npm.cmd` available on `PATH`. LibreChat's `.env` must point to a working MongoDB instance and configure the server to use the port entered in Launcher Settings on an interface reachable through the configured IP address.

```powershell
$ErrorActionPreference = 'Stop'

$Root = $PSScriptRoot
$Package = Join-Path $Root 'package.json'

if (-not (Test-Path -LiteralPath $Package -PathType Leaf)) {
    throw "LibreChat package.json not found: $Package"
}

$Npm = (Get-Command npm.cmd -ErrorAction Stop).Source
Set-Location -LiteralPath $Root

& $Npm run backend
exit $LASTEXITCODE
```

Save it as `Start-LibreChat.ps1` in the folder selected as **LibreChat installation folder**. If Node.js is portable rather than on `PATH`, replace the `Get-Command` line with an absolute or root-relative path to that runtime's `npm.cmd`.

This script does not start MongoDB. Run MongoDB as a separate Windows service or extend the script to verify/start your own dependency before `npm run backend`. Launchpad intentionally stops only LibreChat's application process tree; it does not stop a separately managed MongoDB service.

### First managed start

1. Test each service directly once and confirm its URL works, then stop it normally.
2. Open Launcher Settings, enable **Show services button**, enter both installation folders and addresses, and save.
3. On the main page, press **Start** for the service.
4. Wait for **Connected · managed**. Launchpad records the exact launcher and listening-process identity in `managed-services.json`.
5. Stop and Restart are now available and remain available after Launchpad itself restarts, provided the recorded Windows process identity still matches.

If a service is already running when Launchpad first sees it, it appears as **Connected · external**. Strict mode will not stop a process it did not start. Stop that service manually once, refresh Launchpad, and press Start to hand future control to Launchpad.

Llama Mayhem can force-stop an external listener, but it removes this ownership protection. Do not enable it merely to avoid the one-time handoff.

### OpenTerminal

OpenTerminal is optional and remains inside the OpenWebUI dropdown. To manage it, place it at `OpenWebUI\OpenTerminal`, with `Start-OpenTerminal.ps1` and `config.toml` in that folder. Launchpad reads its listening port from `config.toml` (default `8765`) and checks `/health` on the OpenTerminal address configured in Settings. If OpenTerminal is not installed, its row remains disconnected and its Start action reports that the launcher script is missing.

### Logs and troubleshooting

Launchpad creates the log folders when needed and redirects launcher output to:

```text
OpenWebUI\logs\open-webui.out.log
OpenWebUI\logs\open-webui.err.log
LibreChat\logs\librechat-launchpad.out.log
LibreChat\logs\librechat-launchpad.err.log
```

Common states and errors:

- **Disconnected / Ready to start:** the health URL is unavailable and no process owns the configured port.
- **Connected · external:** the URL is live, but this Launchpad installation did not start the listener.
- **Service launcher not found:** the expected `.ps1` file is missing from the configured installation folder.
- **Launcher exited before opening port:** the script failed, a dependency is unavailable, or the command/port does not match Settings. Read the service's `.err.log`.
- **Recorded process identity no longer matches:** the service was replaced or restarted outside Launchpad. Stop it manually and use Start again.

Stop a managed service before changing its installation folder or port. Do not copy or hand-edit `managed-services.json`; stale records are discarded automatically when their process identity no longer matches.

### Docker installations

The current process-ownership model does not run `docker compose down` during Stop. Killing a foreground `docker compose up` client may leave its containers running, and force-stopping a Docker-owned port with Llama Mayhem can affect Docker itself. For Docker-based OpenWebUI or LibreChat:

- leave the corresponding `Start-*.ps1` absent;
- configure the service URL to get Connected status and the Open link;
- manage Start, Stop, and Restart with Docker Compose or your container manager;
- do not use Llama Mayhem against Docker-published service ports.
