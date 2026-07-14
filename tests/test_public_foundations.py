from __future__ import annotations

import base64
import ipaddress
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
import setup as first_run


class PasswordTests(unittest.TestCase):
    def test_setup_hash_is_accepted_by_application(self) -> None:
        encoded = first_run.hash_password("correct horse battery staple", salt=b"0123456789abcdef")
        self.assertTrue(app.password_matches("correct horse battery staple", encoded))
        self.assertFalse(app.password_matches("wrong", encoded))
        self.assertNotIn("correct horse", encoded)


class SetupTests(unittest.TestCase):
    def test_device_list_parser_accepts_llama_cpp_output(self) -> None:
        output = """Available devices:
          CUDA0: NVIDIA GeForce RTX 3060 Ti (8191 MiB)
          Vulkan0: AMD Radeon Graphics (4096 MiB)
        """
        self.assertEqual(
            first_run.parse_device_list(output),
            [
                ("CUDA0", "NVIDIA GeForce RTX 3060 Ti (8191 MiB)"),
                ("Vulkan0", "AMD Radeon Graphics (4096 MiB)"),
            ],
        )

    def test_first_run_writes_portable_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server_dir = root / "llama-bin"
            server_dir.mkdir()
            executable = server_dir / "llama-server.exe"
            executable.write_bytes(b"")
            config_path = root / "config.json"
            models_path = root / "models.json"
            with mock.patch.object(first_run, "CONFIG_PATH", config_path), mock.patch.object(
                first_run, "MODELS_PATH", models_path
            ):
                first_run.save_setup(str(server_dir))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            models = json.loads(models_path.read_text(encoding="utf-8"))
            self.assertEqual(config["host"], "0.0.0.0")
            self.assertEqual(config["server"]["host"], "0.0.0.0")
            self.assertEqual(
                Path(config["server"]["executable"]).resolve(), executable.resolve()
            )
            self.assertIn("192.168.0.0/16", config["allowed_networks"])
            self.assertFalse(config["authentication"]["enabled"])
            self.assertEqual(config["server"]["device"], "auto")
            self.assertNotIn("CUDA_VISIBLE_DEVICES", config["environment"])
            self.assertEqual(models, {"version": 1, "models": []})

    def test_cpu_selection_disables_gpu_layers(self) -> None:
        config = first_run.build_config(Path("llama-server.exe"), device="none")
        self.assertEqual(config["server"]["device"], "none")
        self.assertEqual(config["server"]["gpu_layers"], 0)

    def test_login_requires_both_fields(self) -> None:
        with self.assertRaises(ValueError):
            first_run.build_config(Path("llama-server.exe"), username="someone", password="")


class SafetyTests(unittest.TestCase):
    @staticmethod
    def service_config(root: Path) -> dict:
        return {
            "openwebui_root": str(root),
            "openwebui_url": "http://127.0.0.1:8181",
            "openterminal_url": "http://127.0.0.1:8765",
            "vane_url": "http://127.0.0.1:32761",
        }

    def test_mutex_identity_is_stable_and_install_specific(self) -> None:
        first = app.instance_mutex_name(Path("C:/Launchpad/Public"))
        same = app.instance_mutex_name(Path("c:/launchpad/public"))
        other = app.instance_mutex_name(Path("C:/Launchpad/Other"))
        self.assertEqual(first, same)
        self.assertNotEqual(first, other)
        self.assertTrue(first.startswith("Local\\LocalModelLaunchpad-"))

    def test_existing_settings_default_llama_mayhem_off(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "llama-server.exe"
            executable.write_bytes(b"")
            settings_path = root / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "openwebui_enabled": False,
                        "openwebui_root": str(root),
                        "openwebui_url": "http://127.0.0.1:8181",
                        "openterminal_url": "http://127.0.0.1:8765",
                        "vane_enabled": False,
                        "vane_url": "http://127.0.0.1:32761",
                        "llama_server_executable": str(executable),
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(app, "SETTINGS_PATH", settings_path):
                settings = app.load_user_settings(str(executable))
            self.assertFalse(settings["llama_mayhem"])

    def test_display_name_cannot_spoof_a_preset_match(self) -> None:
        library = app.ModelCardPresetLibrary(app.PRESET_LIBRARY_PATH)
        self.assertIsNone(library.match("C:/models/unrelated.gguf", name="Qwen3.5 9B"))
        self.assertEqual(library.match("C:/models/Qwen3.5-9B-Q4_K_M.gguf")["id"], "qwen35-9b")

    def test_agentcpm_explore_uses_an_explicit_base_model_reference(self) -> None:
        library = app.ModelCardPresetLibrary(app.PRESET_LIBRARY_PATH)
        match = library.match("C:/models/AgentCPM-Explore.Q4_K_M.gguf")
        self.assertEqual(match["id"], "agentcpm-explore")
        self.assertEqual(match["preset_status"], "reference")

    def test_native_file_picker_is_restricted_to_local_machine_addresses(self) -> None:
        self.assertTrue(app.is_local_machine_address("127.0.0.1"))
        self.assertTrue(app.is_local_machine_address("::1"))
        self.assertTrue(app.is_local_machine_address("::ffff:127.0.0.1"))
        self.assertFalse(app.is_local_machine_address("203.0.113.10"))

    def test_native_file_picker_uses_the_accepted_connection_endpoints(self) -> None:
        with mock.patch.object(app.socket, "getaddrinfo", side_effect=OSError("unavailable")):
            self.assertTrue(
                app.is_local_machine_address("192.168.1.160", "192.168.1.160")
            )
            self.assertTrue(
                app.is_local_machine_address("::ffff:192.168.1.160", "192.168.1.160")
            )
            self.assertFalse(
                app.is_local_machine_address("192.168.1.181", "192.168.1.160")
            )

    def test_request_handler_passes_its_local_connection_endpoint(self) -> None:
        handler = object.__new__(app.RequestHandler)
        handler.client_address = ("192.168.1.160", 50000)
        handler.connection = mock.Mock()
        handler.connection.getsockname.return_value = ("192.168.1.160", 8766)
        with mock.patch.object(app.socket, "getaddrinfo", side_effect=OSError("unavailable")):
            self.assertTrue(handler._client_is_local_machine())

    def test_registered_model_directory_uses_common_existing_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "publisher-a" / "model-a.gguf"
            second = root / "publisher-b" / "model-b.gguf"
            first.parent.mkdir()
            second.parent.mkdir()
            registry = mock.Mock()
            registry.lock = threading.RLock()
            registry.data = {
                "models": [
                    {"model_path": str(first)},
                    {"model_path": str(second)},
                ]
            }
            self.assertEqual(app.registered_model_directory(registry), root.resolve())

    def test_restart_of_unowned_optional_service_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "managed-services.json"
            with mock.patch.object(app, "MANAGED_SERVICES_PATH", state_path), mock.patch.object(
                app, "listener_pids", return_value=[4321]
            ), mock.patch.object(app.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "not started by this Launchpad"):
                    app.control_service(self.service_config(root), "openwebui", "restart")
            run.assert_not_called()

    def test_llama_mayhem_allows_stopping_an_unowned_optional_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {**self.service_config(root), "llama_mayhem": True}
            expected = {"id": "openwebui", "live": False}
            with mock.patch.object(app, "validated_managed_service_record", return_value=None), mock.patch.object(
                app, "load_managed_service_state", return_value={"version": 1, "services": {}}
            ), mock.patch.object(app, "listener_pids", return_value=[4321]), mock.patch.object(
                app, "stop_service_listeners", return_value=[4321]
            ) as stop, mock.patch.object(app, "update_managed_service_record") as update, mock.patch.object(
                app, "service_status", return_value={"openwebui": expected}
            ):
                result = app.control_service(config, "openwebui", "stop")
            self.assertEqual(result, expected)
            stop.assert_called_once()
            update.assert_called_once_with("openwebui", None)

    def test_llama_mayhem_force_stops_every_service_listener(self) -> None:
        spec = {"name": "OpenWebUI", "port": 8181}
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(app, "listener_pids", side_effect=[[4321, 5432], [], []]), mock.patch.object(
            app.subprocess, "run", return_value=completed
        ) as run:
            stopped = app.stop_service_listeners(spec)
        self.assertEqual(stopped, [4321, 5432])
        self.assertEqual(
            [call.args[0][:3] for call in run.call_args_list],
            [["taskkill", "/PID", "4321"], ["taskkill", "/PID", "5432"]],
        )

    def test_process_identity_requires_matching_executable_and_start_marker(self) -> None:
        expected = {"executable": "C:/service.exe", "created": 987654}
        with mock.patch.object(app, "same_executable", return_value=True), mock.patch.object(
            app, "process_creation_marker", return_value=987654
        ):
            self.assertTrue(app.process_identity_matches(4321, expected))
        with mock.patch.object(app, "same_executable", return_value=True), mock.patch.object(
            app, "process_creation_marker", return_value=987655
        ):
            self.assertFalse(app.process_identity_matches(4321, expected))

    def test_managed_service_stop_targets_only_recorded_process_tree(self) -> None:
        spec = {"name": "OpenWebUI", "port": 8181}
        record = {
            "listener": {"pid": 4321, "executable": "C:/service.exe", "created": 987654}
        }
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(app, "listener_pids", side_effect=[[4321], [], []]), mock.patch.object(
            app, "process_identity_matches", return_value=True
        ), mock.patch.object(app.subprocess, "run", return_value=completed) as run, mock.patch.object(
            app, "update_managed_service_record"
        ) as update:
            app.stop_managed_service("openwebui", spec, record)
        self.assertEqual(run.call_args.args[0][:3], ["taskkill", "/PID", "4321"])
        update.assert_called_once_with("openwebui", None)

    def test_stale_model_state_is_discarded_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "active-model.json"
            state_path.write_text('{"current":{"pid":1234}}', encoding="utf-8")
            with mock.patch.object(app, "ACTIVE_MODEL_PATH", state_path):
                manager = app.LaunchManager({"server": {"port": 8000, "executable": "llama-server.exe"}}, object())
            self.assertIsNone(manager.process)
            self.assertFalse(state_path.exists())

    def test_verified_model_state_is_recovered_after_launcher_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "active-model.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "server_port": 8000,
                        "executable": "C:/llama-server.exe",
                        "current": {
                            "id": "model-default",
                            "pid": 4321,
                            "port": 8000,
                            "process_executable": "C:/llama-server.exe",
                            "process_started_marker": 987654,
                            "owned": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            config = {"server": {"port": 8000, "executable": "C:/llama-server.exe"}}
            with mock.patch.object(app, "ACTIVE_MODEL_PATH", state_path), mock.patch.object(
                app, "listener_pids", return_value=[4321]
            ), mock.patch.object(app, "process_identity_matches", return_value=True):
                manager = app.LaunchManager(config, object())
            self.assertIsInstance(manager.process, app.RecoveredProcess)
            self.assertEqual(manager.process.pid, 4321)
            self.assertTrue(manager.current["recovered"])

    def test_llama_mayhem_discovers_and_adopts_an_external_llama_server(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "active-model.json"
            config = {
                "llama_mayhem": True,
                "server": {"port": 8000, "executable": "C:/llama-server.exe"},
            }
            with mock.patch.object(app, "ACTIVE_MODEL_PATH", state_path), mock.patch.object(
                app, "LOG_DIR", root
            ), mock.patch.object(app, "llama_server_process_ids", return_value=[4321]), mock.patch.object(
                app, "listening_ports_by_pid", return_value={4321: [8000]}
            ), mock.patch.object(app, "same_executable", return_value=False), mock.patch.object(
                app, "process_image_path", return_value="D:/other/llama-server.exe"
            ), mock.patch.object(app, "process_creation_marker", return_value=987654):
                manager = app.LaunchManager(config, object())
            self.assertIsInstance(manager.process, app.RecoveredProcess)
            self.assertEqual(manager.process.pid, 4321)
            self.assertTrue(manager.current["external"])
            self.assertFalse(manager.current["owned"])
            self.assertEqual(manager.current["port"], 8000)

    def test_stop_refuses_an_unowned_process_without_taskkill(self) -> None:
        manager = app.LaunchManager.__new__(app.LaunchManager)
        manager.lock = threading.RLock()
        manager.process = mock.Mock()
        manager.process.poll.return_value = None
        manager.current = {"pid": 1234, "owned": False}
        manager.last = None
        manager.log_handle = None
        manager.config = {"server": {"executable": "C:/llama-server.exe"}}
        with mock.patch.object(app.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "not started by this Launchpad"):
                manager.stop()
        run.assert_not_called()

    def test_llama_mayhem_stops_an_unowned_model_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = app.LaunchManager.__new__(app.LaunchManager)
            manager.lock = threading.RLock()
            manager.process = mock.Mock()
            manager.process.pid = 1234
            manager.process.poll.return_value = None
            manager.process.wait.return_value = 0
            manager.process.returncode = 0
            manager.current = {"pid": 1234, "owned": False, "id": None}
            manager.last = None
            manager.log_handle = None
            manager.config = {
                "llama_mayhem": True,
                "server": {"executable": "C:/llama-server.exe"},
            }
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(app, "ACTIVE_MODEL_PATH", Path(temporary) / "active-model.json"), mock.patch.object(
                app.subprocess, "run", return_value=completed
            ) as run:
                result = manager.stop()
            self.assertEqual(result["status"], "idle")
            self.assertEqual(run.call_args.args[0][:3], ["taskkill", "/PID", "1234"])
            self.assertIsNone(manager.process)

    def test_disabling_llama_mayhem_detaches_without_stopping_an_unowned_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = app.LaunchManager.__new__(app.LaunchManager)
            manager.lock = threading.RLock()
            manager.process = mock.Mock()
            manager.current = {"pid": 1234, "owned": False}
            manager.last = None
            manager.log_handle = None
            manager.config = {"llama_mayhem": True}
            manager._last_discovery = 1.0
            with mock.patch.object(app, "ACTIVE_MODEL_PATH", Path(temporary) / "active-model.json"):
                manager.set_llama_mayhem(False)
            self.assertFalse(manager.config["llama_mayhem"])
            self.assertIsNone(manager.process)
            self.assertIsNone(manager.current)

    def test_auto_device_omits_device_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model_path = Path(temporary) / "model.gguf"
            model_path.write_bytes(b"")
            model = {
                "id": "model",
                "name": "Model",
                "alias": "model",
                "model_path": str(model_path),
                "context": 2048,
            }
            profile = {
                "id": "model-default",
                "name": "Default",
                "mode": "Instruct",
                "vision": False,
                "reasoning": "auto",
                "sampling": dict(app.DEFAULT_SAMPLING),
            }
            registry = mock.Mock()
            registry.resolve.return_value = (model, profile)
            manager = app.LaunchManager.__new__(app.LaunchManager)
            manager.registry = registry
            manager.config = first_run.build_config(Path("llama-server.exe"), device="auto")
            command, _, _ = manager.build_command("model-default")
            self.assertNotIn("--device", command)


class AuthenticationHTTPTests(unittest.TestCase):
    def test_http_basic_gate_challenges_then_accepts_valid_credentials(self) -> None:
        encoded = app.password_hash("lan-password", salt=b"0123456789abcdef")
        config = {
            "_networks": [ipaddress.ip_network("127.0.0.0/8")],
            "authentication": {"enabled": True, "username": "lan-user", "password_hash": encoded},
        }
        server = app.LauncherHTTPServer(("127.0.0.1", 0), app.RequestHandler, config, None, None, "token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        try:
            with self.assertRaises(HTTPError) as denied:
                urlopen(url, timeout=3)
            self.assertEqual(denied.exception.code, 401)
            credentials = base64.b64encode(b"lan-user:lan-password").decode("ascii")
            request = Request(url, headers={"Authorization": f"Basic {credentials}"})
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"Local Model Launchpad", response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_file_picker_route_requires_token_and_local_client(self) -> None:
        config = {
            "_networks": [ipaddress.ip_network("127.0.0.0/8")],
            "authentication": {"enabled": False},
        }
        registry = mock.Mock()
        registry.lock = threading.RLock()
        registry.data = {"models": []}
        server = app.LauncherHTTPServer(("127.0.0.1", 0), app.RequestHandler, config, registry, None, "token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/api/file-picker"
        body = json.dumps({"kind": "model", "initial_path": ""}).encode("utf-8")

        def picker_request(token: str = "") -> Request:
            headers = {"Content-Type": "application/json"}
            if token:
                headers["X-Launcher-Token"] = token
            return Request(url, data=body, headers=headers, method="POST")

        try:
            with self.assertRaises(HTTPError) as missing_token:
                urlopen(picker_request(), timeout=3)
            self.assertEqual(missing_token.exception.code, 403)

            with mock.patch.object(app.RequestHandler, "_client_is_local_machine", return_value=False), mock.patch.object(
                app, "choose_gguf_file"
            ) as picker:
                with self.assertRaises(HTTPError) as remote_client:
                    urlopen(picker_request("token"), timeout=3)
                self.assertEqual(remote_client.exception.code, 403)
                picker.assert_not_called()

            with mock.patch.object(app.RequestHandler, "_client_is_local_machine", return_value=True), mock.patch.object(
                app, "choose_gguf_file", return_value="C:\\Models\\model.gguf"
            ) as picker:
                with urlopen(picker_request("token"), timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read()), {"path": "C:\\Models\\model.gguf"})
                picker.assert_called_once()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
