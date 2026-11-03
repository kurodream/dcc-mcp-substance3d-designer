from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import pytest

from dcc_mcp_substance3d_designer._install_model import INSTALL_SOP_DOCUMENT_SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]


def _require_typed_test_runtime(python_executable):
    """Validate exact installed runtime provenance before any gateway/server I/O."""
    from dcc_mcp_substance3d_designer import _install_preflight

    return _install_preflight.query_python(Path(python_executable).resolve(strict=True))


@contextmanager
def _owned_test_gateway(tmp_path, monkeypatch, runtime):
    """Run one isolated gateway whose complete process tree is owned by this test."""
    from dcc_mcp_substance3d_designer import _install_process

    registry_dir = tmp_path / "gateway-registry"
    registry_dir.mkdir()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        gateway_port = reservation.getsockname()[1]
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    mcp_url = gateway_url + "/mcp"
    assert _install_process.observe_listener_identity(mcp_url) is None

    server_binary = Path(runtime["server_binary"]).resolve(strict=True)
    environment = os.environ.copy()
    environment.update(
        {
            "DCC_MCP_GATEWAY_PORT": str(gateway_port),
            "DCC_MCP_GATEWAY_IDLE_TIMEOUT_SECS": "0",
            "DCC_MCP_REGISTRY_DIR": str(registry_dir),
        }
    )
    environment.pop("DCC_MCP_GATEWAY_PERSIST", None)
    for name in ("DCC_MCP_GATEWAY_PORT", "DCC_MCP_GATEWAY_IDLE_TIMEOUT_SECS", "DCC_MCP_REGISTRY_DIR"):
        monkeypatch.setenv(name, environment[name])
    monkeypatch.delenv("DCC_MCP_GATEWAY_PERSIST", raising=False)

    command = [
        str(server_binary),
        "gateway",
        "--host",
        "127.0.0.1",
        "--port",
        str(gateway_port),
        "--remote-port",
        "0",
        "--registry-dir",
        str(registry_dir),
        "--gateway-idle-timeout-secs",
        "0",
        "--no-admin",
    ]
    process = None
    owner = None
    expected_process = None
    observed_listener = None
    cleanup_error = None
    try:
        process, owner = _install_process._start_owned_supervised_process(
            command,
            env=environment,
            cwd=tmp_path,
            root=tmp_path / "gateway-supervisor",
        )
        expected_process = _install_process.observe_process_identity(process.pid)
        assert expected_process is not None
        assert expected_process["pid"] == process.pid
        assert Path(expected_process["executable"]).resolve() == server_binary

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            observed_listener = _install_process.observe_listener_identity(mcp_url)
            if observed_listener == {**expected_process, "listener_port": gateway_port}:
                try:
                    with urllib.request.urlopen(gateway_url + "/health", timeout=0.25) as response:
                        if response.status == 200:
                            break
                except (urllib.error.URLError, OSError):
                    pass
            time.sleep(0.02)
        else:
            raise AssertionError("owned test gateway readiness timed out")
        assert process.poll() is None
        assert observed_listener == {**expected_process, "listener_port": gateway_port}

        yield {
            "pid": process.pid,
            "port": gateway_port,
            "process_identity": expected_process,
            "listener_identity": observed_listener,
        }
    finally:
        if process is not None and owner is not None:
            current_process = _install_process.observe_process_identity(process.pid)
            current_listener = _install_process.observe_listener_identity(mcp_url)
            if process.poll() is None and (
                current_process != expected_process or current_listener != observed_listener
            ):
                cleanup_error = "owned gateway identity changed before cleanup"
            if not _install_process._cleanup_owned_process(process, owner):
                cleanup_error = cleanup_error or "owned gateway process-tree cleanup failed"

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if (
                    _install_process.observe_process_identity(process.pid) is None
                    and _install_process.observe_listener_identity(mcp_url) is None
                ):
                    break
                time.sleep(0.02)
            else:
                cleanup_error = cleanup_error or "owned gateway PID or listener survived cleanup"
        if cleanup_error is not None:
            raise AssertionError(cleanup_error)


@pytest.fixture(autouse=True)
def _trust_synthetic_designer_product(monkeypatch):
    from dcc_mcp_substance3d_designer import _install_preflight

    monkeypatch.setattr(_install_preflight, "_host_product_identity", lambda _path: True)


def _synthetic_designer(tmp_path, version="15.1.0", embedded_python=None):
    host_root = tmp_path / f"Adobe Substance 3D Designer {version}"
    host = host_root / ("Adobe Substance 3D Designer.exe" if os.name == "nt" else "Adobe Substance 3D Designer")
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_bytes(b"synthetic host")
    python_version = embedded_python or f"{sys.version_info.major}.{sys.version_info.minor}"
    (host.parent / "plugins" / "pythonsdk" / "lib" / f"python{python_version}").mkdir(parents=True)
    return host


def test_install_defaults_to_a_non_mutating_public_plan(tmp_path, monkeypatch, capsys):
    host = _synthetic_designer(tmp_path)
    install_root = tmp_path / "install-root"
    source_path = str(ROOT / "src")
    inherited = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(part for part in (source_path, inherited) if part))
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_DESIGNER_INSTALL_ROOT", str(install_root))

    from dcc_mcp_substance3d_designer.install_cli import main

    exit_code = main(
        [
            "install",
            "--dcc-path",
            str(host),
            "--python",
            sys.executable,
            "--json",
        ]
    )
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    # Core 0.20.34 repurposed INSTALL_SOP_SCHEMA_VERSION as the schema *artifact*
    # revision (2); the report document field stays at the value the schema pins.
    # Assert the pinned literal too: checking only the derived constant would
    # compare the report against its own source and could never fail.
    assert result["schema_version"] == INSTALL_SOP_DOCUMENT_SCHEMA_VERSION
    assert result["schema_version"] == 1
    assert result["status"] == "planned"
    assert result["dcc_type"] == "substance3d_designer"
    assert result["adapter_version"]
    assert result["core_version"]
    assert result["receipt_path"]
    assert result["profile"] == {
        "plugin_search_path": str(install_root / "payload" / "plugins"),
        "selection_source": "receipted_launcher",
    }
    assert result["verify"] == {
        "directly_usable": False,
        "failure_stage": None,
        "failure_reason": None,
    }
    assert [step["id"] for step in result["steps"]] == [
        "preflight",
        "install-launcher",
        "receipt",
        "verify",
    ]
    assert result["next_steps"] == [
        {
            "id": "execute_install",
            "description": "Execute the validated Designer install plan.",
            "command": [
                "dcc-mcp-substance3d-designer",
                "install",
                "--dcc-path",
                str(host),
                "--python",
                str(Path(sys.executable).absolute()),
                "--json",
                "--yes",
            ],
            "why": "Planning does not modify the Designer installation.",
        }
    ]
    assert not install_root.exists()


def test_install_stages_a_receipted_launcher_and_uninstall_consumes_only_the_receipt(tmp_path, monkeypatch, capsys):
    host = _synthetic_designer(tmp_path)
    install_root = tmp_path / "install-root"
    source_path = str(ROOT / "src")
    inherited = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(part for part in (source_path, inherited) if part))
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_DESIGNER_INSTALL_ROOT", str(install_root))
    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("DCC_MCP_INSTALL_VERIFY_TIMEOUT", "0.01")

    from dcc_mcp_substance3d_designer.install_cli import main

    common = ["--dcc-path", str(host), "--python", sys.executable, "--json"]
    install_exit = main(["install", *common, "--yes"])
    installed = json.loads(capsys.readouterr().out)

    assert install_exit == 40
    assert installed["status"] == "partial"
    assert installed["verify"]["directly_usable"] is False
    assert installed["verify"]["failure_stage"] == "readiness"
    receipt_path = Path(installed["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    suffix = ".cmd" if os.name == "nt" else ".sh"
    owned_paths = {Path(item["path"]) for item in receipt["files"]}
    assert owned_paths == {
        install_root / "payload" / "plugins" / "dcc_mcp_substance3d_designer_plugin.py",
        install_root / "launchers" / f"substance3d_designer{suffix}",
    }
    assert all(path.is_file() for path in owned_paths)
    assert all(len(item["sha256"]) == 64 for item in receipt["files"])
    launcher = next(path for path in owned_paths if path.suffix == suffix)
    launcher_text = launcher.read_text(encoding="utf-8")
    assert "SBS_DESIGNER_PYTHON_PATH" in launcher_text
    assert "PYTHONPATH" in launcher_text
    assert "PYTHONDONTWRITEBYTECODE=1" in launcher_text
    site_packages = Path(common[common.index("--python") + 1]).parent.parent / "Lib" / "site-packages"
    sbs_path_lines = [line for line in launcher_text.splitlines() if "SBS_DESIGNER_PYTHON_PATH=" in line]
    assert any(str(site_packages) in line for line in sbs_path_lines)
    plugin = next(path for path in owned_paths if path.name == "dcc_mcp_substance3d_designer_plugin.py")
    plugin_text = plugin.read_text(encoding="utf-8")
    assert "sys.dont_write_bytecode = True" in plugin_text
    assert "sys.path.insert(0" in plugin_text
    assert "site-packages" in plugin_text

    assert main(["uninstall", *common]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert all(path.exists() for path in owned_paths)

    assert main(["uninstall", *common, "--yes"]) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["status"] == "ok"
    assert not receipt_path.exists()
    assert all(not path.exists() for path in owned_paths)

    assert main(["uninstall", *common, "--yes"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_status_reports_repair_and_reinstall_converges(tmp_path, monkeypatch, capsys):
    host = _synthetic_designer(tmp_path)
    install_root = tmp_path / "install-root"
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_DESIGNER_INSTALL_ROOT", str(install_root))
    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("DCC_MCP_INSTALL_VERIFY_TIMEOUT", "0.01")

    from dcc_mcp_substance3d_designer.install_cli import main

    common = ["--dcc-path", str(host), "--python", sys.executable, "--json"]
    assert main(["install", *common, "--yes"]) == 40
    installed = json.loads(capsys.readouterr().out)
    receipt = json.loads(Path(installed["receipt_path"]).read_text(encoding="utf-8"))
    plugin = next(Path(item["path"]) for item in receipt["files"] if item["path"].endswith("_plugin.py"))
    plugin.unlink()

    assert main(["status", *common]) == 10
    damaged = json.loads(capsys.readouterr().out)
    assert damaged["status"] == "partial"
    assert damaged["install_state"] == "repair"

    assert main(["install", *common, "--yes"]) == 40
    capsys.readouterr()
    assert main(["status", *common]) == 0
    repaired = json.loads(capsys.readouterr().out)
    assert repaired["status"] == "ok"
    assert repaired["install_state"] == "current"


def test_failed_upgrade_restores_the_previous_receipted_installation(tmp_path, monkeypatch, capsys):
    host = _synthetic_designer(tmp_path)
    install_root = tmp_path / "install-root"
    inherited = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(part for part in (str(ROOT / "src"), inherited) if part),
    )
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_DESIGNER_INSTALL_ROOT", str(install_root))
    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("DCC_MCP_INSTALL_VERIFY_TIMEOUT", "0.01")

    from dcc_mcp_substance3d_designer import _installer
    from dcc_mcp_substance3d_designer.install_cli import main

    common = ["--dcc-path", str(host), "--python", sys.executable, "--json"]
    assert main(["install", *common, "--yes"]) == 40
    installed = json.loads(capsys.readouterr().out)
    receipt_path = Path(installed["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    owned = [Path(item["path"]) for item in receipt["files"]]
    before = {path: path.read_bytes() for path in [*owned, receipt_path]}

    monkeypatch.setattr(_installer, "_plugin_source", lambda _ctx: "changed payload")

    def fail_receipt_commit(*_args, **_kwargs):
        raise OSError("synthetic receipt commit failure")

    monkeypatch.setattr(_installer, "_write_json_atomic", fail_receipt_commit)

    assert main(["upgrade", *common, "--yes"]) == 30
    failed = json.loads(capsys.readouterr().out)

    assert failed["status"] == "failed"
    assert failed["verify"]["failure_stage"] == "install"
    assert {path: path.read_bytes() for path in [*owned, receipt_path]} == before
    staging = install_root / "staging"
    assert not staging.exists() or not any(staging.iterdir())


def test_upgrade_does_not_rename_the_live_payload_before_the_core_lock_gate(tmp_path, monkeypatch, capsys):
    host = _synthetic_designer(tmp_path)
    install_root = tmp_path / "install-root"
    inherited = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(part for part in (str(ROOT / "src"), inherited) if part),
    )
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_DESIGNER_INSTALL_ROOT", str(install_root))
    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("DCC_MCP_INSTALL_VERIFY_TIMEOUT", "0.01")

    from dcc_mcp_substance3d_designer import _installer
    from dcc_mcp_substance3d_designer.install_cli import main

    common = ["--dcc-path", str(host), "--python", sys.executable, "--json"]
    assert main(["install", *common, "--yes"]) == 40
    capsys.readouterr()

    original_replace = _installer.os.replace
    live_payload = install_root / "payload"

    def reject_live_payload_rename(source, destination):
        if Path(source) == live_payload and "backup" in Path(destination).parts:
            raise AssertionError("live payload bypassed safe_replace_tree")
        return original_replace(source, destination)

    monkeypatch.setattr(_installer.os, "replace", reject_live_payload_rename)

    assert main(["upgrade", *common, "--yes"]) == 40
    upgraded = json.loads(capsys.readouterr().out)
    assert upgraded["status"] == "partial"
    assert upgraded["verify"]["failure_stage"] == "readiness"


def test_core_lock_evidence_returns_restart_without_losing_the_previous_install(tmp_path, monkeypatch, capsys):
    host = _synthetic_designer(tmp_path)
    install_root = tmp_path / "install-root"
    inherited = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(part for part in (str(ROOT / "src"), inherited) if part),
    )
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_DESIGNER_INSTALL_ROOT", str(install_root))
    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("DCC_MCP_INSTALL_VERIFY_TIMEOUT", "0.01")

    from dcc_mcp_substance3d_designer import _installer
    from dcc_mcp_substance3d_designer.install_cli import main

    common = ["--dcc-path", str(host), "--python", sys.executable, "--json"]
    assert main(["install", *common, "--yes"]) == 40
    installed = json.loads(capsys.readouterr().out)
    receipt_path = Path(installed["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    owned = [Path(item["path"]) for item in receipt["files"]]
    before = {path: path.read_bytes() for path in [*owned, receipt_path]}

    monkeypatch.setattr(
        _installer,
        "safe_replace_tree",
        lambda *_args: {
            "success": False,
            "requires_restart": True,
            "message": "synthetic Windows lock",
        },
    )

    assert main(["upgrade", *common, "--yes"]) == 50
    blocked = json.loads(capsys.readouterr().out)

    assert blocked["status"] == "requires_restart"
    assert blocked["receipt_path"] == str(receipt_path)
    assert blocked["next_steps"][0]["command"][-1] == "--yes"
    assert {path: path.read_bytes() for path in [*owned, receipt_path]} == before


def test_locked_uninstall_returns_one_machine_executable_retry(tmp_path, monkeypatch, capsys):
    host = _synthetic_designer(tmp_path)
    install_root = tmp_path / "install-root"
    inherited = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(part for part in (str(ROOT / "src"), inherited) if part),
    )
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_DESIGNER_INSTALL_ROOT", str(install_root))
    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("DCC_MCP_INSTALL_VERIFY_TIMEOUT", "0.01")

    from dcc_mcp_substance3d_designer import _installer
    from dcc_mcp_substance3d_designer.install_cli import main

    common = ["--dcc-path", str(host), "--python", sys.executable, "--json"]
    assert main(["install", *common, "--yes"]) == 40
    installed = json.loads(capsys.readouterr().out)
    receipt_path = Path(installed["receipt_path"])

    original_safe_remove_tree = _installer.safe_remove_tree

    def locked_payload_only(path):
        if Path(path).resolve() == (install_root / "payload").resolve():
            return {
                "success": False,
                "requires_restart": True,
                "message": "synthetic Windows lock",
            }
        return original_safe_remove_tree(path)

    monkeypatch.setattr(_installer, "safe_remove_tree", locked_payload_only)

    assert main(["uninstall", *common, "--yes"]) == 50
    blocked = json.loads(capsys.readouterr().out)

    assert blocked["status"] == "requires_restart"
    assert blocked["next_steps"] == [
        {
            "id": "retry_uninstall",
            "description": "Close Designer and retry the uninstall operation.",
            "command": [
                "dcc-mcp-substance3d-designer",
                "uninstall",
                "--dcc-path",
                str(host),
                "--python",
                str(Path(sys.executable).absolute()),
                "--json",
                "--yes",
            ],
            "why": "Core reported a loaded or locked artifact under the install root.",
        }
    ]
    assert receipt_path.exists()


def test_verify_proves_direct_usability_with_a_typed_designer_probe(tmp_path, monkeypatch, capsys):
    host = _synthetic_designer(tmp_path)
    install_root = tmp_path / "install-root"
    inherited = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(part for part in (str(ROOT / "src"), inherited) if part),
    )
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_DESIGNER_INSTALL_ROOT", str(install_root))
    monkeypatch.setenv("DCC_MCP_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("DCC_MCP_DISABLE_FILE_LOGGING", "1")
    monkeypatch.setenv("DCC_MCP_DISABLE_JOB_PERSISTENCE", "1")
    monkeypatch.setenv("DCC_MCP_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("DCC_MCP_INSTALL_VERIFY_TIMEOUT", "10")

    runtime = _require_typed_test_runtime(sys.executable)
    import dcc_mcp_substance3d_designer as adapter

    # Pytest imports the source tree, while the lifecycle contract models the
    # normal wheel install selected by the embedded interpreter.
    adapter.__file__ = runtime["adapter_file"]

    color_engine = types.SimpleNamespace(
        getName=lambda: "legacy",
        getWorkingColorSpaceName=lambda: "Linear",
        getRawColorSpaceName=lambda: "Raw",
        getOCIOConfigFileName=lambda: "",
    )
    ui_manager = types.SimpleNamespace(getCurrentGraph=lambda: None)
    probe_calls = {"version": 0}

    def get_version():
        probe_calls["version"] += 1
        return "15.1.0"

    app = types.SimpleNamespace(
        getVersion=get_version,
        getUIMgr=lambda: ui_manager,
        getColorManagementEngine=lambda: color_engine,
    )
    monkeypatch.setitem(
        sys.modules,
        "sd",
        types.SimpleNamespace(getContext=lambda: types.SimpleNamespace(getSDApplication=lambda: app)),
    )

    from dcc_mcp_substance3d_designer import _install_preflight, _install_process
    from dcc_mcp_substance3d_designer._install_process import observe_listener_identity, observe_process_identity
    from dcc_mcp_substance3d_designer.dispatcher import DesignerQtDispatcher
    from dcc_mcp_substance3d_designer.install_cli import main
    from dcc_mcp_substance3d_designer.server import start_server, stop_server

    observed_process = observe_process_identity(os.getpid())
    assert observed_process is not None
    assert observed_process["pid"] == os.getpid()
    selected_process = Path(str(observed_process["executable"])).resolve()
    assert selected_process.is_file()
    monkeypatch.setattr(_install_preflight, "_resolve_host_path", lambda *_args: selected_process)
    monkeypatch.setattr(_install_preflight, "_detect_host_version", lambda *_args: ("15.1.0", "test"))
    monkeypatch.setattr(
        _install_preflight,
        "_detect_embedded_python_version",
        lambda *_args: (f"{sys.version_info.major}.{sys.version_info.minor}", "test"),
    )
    monkeypatch.setitem(
        sys.modules,
        "dcc_mcp_substance3d_designer_plugin",
        types.SimpleNamespace(
            __file__=str(install_root / "payload" / "plugins" / "dcc_mcp_substance3d_designer_plugin.py")
        ),
    )

    foreign_gateway_before = _install_process.observe_listener_identity("http://127.0.0.1:9765/mcp")
    dispatcher = DesignerQtDispatcher()
    with _owned_test_gateway(tmp_path, monkeypatch, runtime) as gateway:
        start_server(dispatcher, port=0, enable_gateway_failover=False)
        try:
            common = ["--dcc-path", str(host), "--python", sys.executable, "--json"]

            def run_while_pumping(arguments):
                result = []
                worker = threading.Thread(target=lambda: result.append(main(arguments)))
                worker.start()
                deadline = time.monotonic() + 20
                while worker.is_alive() and time.monotonic() < deadline:
                    dispatcher.drain_queue(5)
                    time.sleep(0.01)
                worker.join(timeout=1)
                assert not worker.is_alive()
                return result[0]

            assert run_while_pumping(["install", *common, "--yes"]) == 0
            installed = json.loads(capsys.readouterr().out)
            assert installed["verify"] == {
                "directly_usable": True,
                "failure_stage": None,
                "failure_reason": None,
                "probe_tool": "designer_diagnostics__ping",
            }
            assert probe_calls["version"] > 0

            assert run_while_pumping(["verify", *common]) == 0
            verified = json.loads(capsys.readouterr().out)
            assert verified["verify"]["directly_usable"] is True
            assert verified["verify"]["probe_tool"] == "designer_diagnostics__ping"
        finally:
            stop_server()

    assert observe_process_identity(gateway["pid"]) is None
    assert observe_listener_identity(f"http://127.0.0.1:{gateway['port']}/mcp") is None
    foreign_gateway_after = _install_process.observe_listener_identity("http://127.0.0.1:9765/mcp")
    for foreign_gateway in (foreign_gateway_before, foreign_gateway_after):
        if foreign_gateway is not None:
            assert foreign_gateway["listener_port"] == 9765
            assert foreign_gateway["pid"] != gateway["pid"]


@pytest.mark.parametrize("failure", [TimeoutError("synthetic timeout"), KeyboardInterrupt()])
def test_owned_gateway_cleanup_survives_timeout_and_cancellation(tmp_path, monkeypatch, failure):
    from dcc_mcp_substance3d_designer._install_process import observe_listener_identity, observe_process_identity

    runtime = _require_typed_test_runtime(sys.executable)
    gateway = None
    with pytest.raises(type(failure)):
        with _owned_test_gateway(tmp_path, monkeypatch, runtime) as gateway:
            raise failure

    assert gateway is not None
    assert observe_process_identity(gateway["pid"]) is None
    assert observe_listener_identity(f"http://127.0.0.1:{gateway['port']}/mcp") is None


def test_runtime_mismatch_fails_before_gateway_or_server_io(tmp_path, monkeypatch):
    from dcc_mcp_substance3d_designer import _install_preflight, _install_process, server
    from dcc_mcp_substance3d_designer.__version__ import __version__
    from dcc_mcp_substance3d_designer._install_model import LifecycleFailure

    effects = []
    mismatch = {
        "python_version": "{}.{}.{}".format(*sys.version_info[:3]),
        "executable": str(Path(sys.executable).resolve()),
        "adapter_version": __version__,
        "adapter_dist_version": "9.9.9",
    }
    monkeypatch.setattr(
        _install_preflight,
        "_run_bounded_command",
        lambda *_args, **_kwargs: {"success": True, "stdout": json.dumps(mismatch), "stderr": ""},
    )
    monkeypatch.setattr(
        _install_process,
        "_start_owned_supervised_process",
        lambda *_args, **_kwargs: effects.append("gateway") or (_ for _ in ()).throw(AssertionError()),
        raising=False,
    )
    monkeypatch.setattr(server, "start_server", lambda *_args, **_kwargs: effects.append("server"))

    with pytest.raises(LifecycleFailure, match="installed distribution"):
        _require_typed_test_runtime(sys.executable)

    assert effects == []
    assert not tmp_path.joinpath("gateway-supervisor").exists()


def test_packaged_designer_plugin_captures_bootstrap_failures():
    plugin = (
        ROOT
        / "src"
        / "dcc_mcp_substance3d_designer"
        / "designer"
        / "plugins"
        / "dcc_mcp_substance3d_designer_plugin.py"
    ).read_text(encoding="utf-8")

    assert "capture_bootstrap_errors" in plugin
    assert 'phase="import"' in plugin
    assert 'phase="startup"' in plugin
    assert 'phase="shutdown"' in plugin

    interactive = ROOT.joinpath("src", "dcc_mcp_substance3d_designer", "designer_plugin.py").read_text(encoding="utf-8")
    assert "designer.plugins.dcc_mcp_substance3d_designer_plugin" in interactive
    assert "from dcc_mcp_substance3d_designer.plugin import" not in interactive


def test_distribution_exposes_the_standard_lifecycle_contract():
    pyproject = ROOT.joinpath("pyproject.toml").read_text(encoding="utf-8")

    assert "[project.scripts]" in pyproject
    assert 'dcc-mcp-substance3d-designer = "dcc_mcp_substance3d_designer.install_cli:main"' in pyproject
    assert "dcc-mcp-core>=0.20.15,<1.0.0" in pyproject
    assert '"src/dcc_mcp_substance3d_designer/designer/**"' in pyproject
    assert '"install.md"' in pyproject


def test_readiness_uses_a_small_read_only_diagnostics_skill():
    skill_root = ROOT / "src" / "dcc_mcp_substance3d_designer" / "skills" / "designer-diagnostics"
    tools = skill_root.joinpath("tools.yaml").read_text(encoding="utf-8")
    server = ROOT.joinpath("src", "dcc_mcp_substance3d_designer", "server.py").read_text(encoding="utf-8")

    assert skill_root.joinpath("SKILL.md").is_file()
    assert skill_root.joinpath("scripts", "ping.py").is_file()
    assert "name: ping" in tools
    assert "read_only: true" in tools
    assert "execution: sync" in tools
    assert "affinity: main" in tools
    assert 'load_skill("designer-diagnostics")' in server
    assert 'load_skill("designer-session")' not in server


def test_install_runbook_and_ci_publish_the_canonical_contract():
    runbook = ROOT.joinpath("install.md").read_text(encoding="utf-8")
    readme = ROOT.joinpath("README.md").read_text(encoding="utf-8")
    workflow = ROOT.joinpath(".github", "workflows", "ci.yml").read_text(encoding="utf-8")

    for heading in (
        "## Requirements",
        "## Supported versions",
        "## Agent quick path",
        "## Manual path",
        "## Verify",
        "## Upgrade",
        "## Uninstall",
        "## Troubleshooting",
    ):
        assert heading in runbook
    for platform in ("Windows", "macOS", "Linux"):
        assert platform in runbook
    for verb in ("install", "status", "verify", "uninstall", "upgrade"):
        assert f"dcc-mcp-substance3d-designer {verb}" in runbook
    assert "--config-file" in runbook
    assert "directly_usable" in runbook
    assert "dcc-mcp-core 0.20.15" in runbook
    assert "[Install SOP](install.md)" in readme
    assert "Install lifecycle smoke" in workflow
    assert "tests/test_install_lifecycle.py" in workflow


def test_preflight_rejects_a_python_abi_that_cannot_load_in_designer(tmp_path, monkeypatch, capsys):
    host = _synthetic_designer(tmp_path, embedded_python="0.1")
    install_root = tmp_path / "install-root"
    inherited = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(part for part in (str(ROOT / "src"), inherited) if part),
    )
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_DESIGNER_INSTALL_ROOT", str(install_root))

    from dcc_mcp_substance3d_designer.install_cli import main

    exit_code = main(["install", "--dcc-path", str(host), "--python", sys.executable, "--json", "--dry-run"])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 10
    assert result["verify"]["directly_usable"] is False
    assert result["verify"]["failure_stage"] == "python_compatibility"
    assert "embedded Python 0.1" in result["verify"]["failure_reason"]
    assert not install_root.exists()


def test_posix_launcher_preserves_existing_paths_when_owned_paths_contain_spaces():
    from dcc_mcp_substance3d_designer._installer import _launcher_payload

    context = types.SimpleNamespace(
        plugin_path=PurePosixPath("/tmp/root with spaces/payload/plugins/plugin.py"),
        python_root=PurePosixPath("/tmp/python with spaces/site-packages"),
        host_path=PurePosixPath("/opt/Adobe Designer/Designer"),
    )

    launcher = _launcher_payload(context, platform_name="posix").decode("utf-8")

    assert "adapter_plugins='/tmp/root with spaces/payload/plugins'" in launcher
    assert "python_root='/tmp/python with spaces/site-packages'" in launcher
    assert '${SBS_DESIGNER_PYTHON_PATH}:}${python_root}:${adapter_plugins}"' in launcher
    assert 'PYTHONPATH="${python_root}${PYTHONPATH:+:${PYTHONPATH}}"' in launcher
    assert "exec '/opt/Adobe Designer/Designer' \"$@\"" in launcher


@pytest.mark.skipif(os.name != "nt", reason="Windows standard install discovery")
def test_preflight_discovers_a_single_standard_designer_install(tmp_path, monkeypatch, capsys):
    host = tmp_path / "Adobe" / "Adobe Substance 3D Designer 15.1.0" / "Adobe Substance 3D Designer.exe"
    host.parent.mkdir(parents=True)
    host.write_bytes(b"synthetic host")
    (host.parent / "plugins" / "pythonsdk" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}").mkdir(
        parents=True
    )
    install_root = tmp_path / "install-root"
    inherited = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(part for part in (str(ROOT / "src"), inherited) if part),
    )
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("ProgramW6432", str(tmp_path))
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_DESIGNER_INSTALL_ROOT", str(install_root))

    from dcc_mcp_substance3d_designer.install_cli import main

    exit_code = main(["install", "--python", sys.executable, "--json", "--dry-run"])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert result["host"]["path"] == str(host.resolve())
    assert result["host"]["version"] == "15.1.0"
    assert not install_root.exists()
