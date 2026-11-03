"""Designer-owned Install SOP lifecycle service."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import threading
import time
import uuid
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, Tuple

import dcc_mcp_core
from dcc_mcp_core.deployment import (
    INSTALL_EXIT_INSTALL,
    INSTALL_EXIT_OK,
    INSTALL_EXIT_PREFLIGHT,
    INSTALL_EXIT_REQUIRES_RESTART,
    INSTALL_EXIT_VERIFY,
    inspect_install_root,
    safe_remove_tree,
    safe_replace_tree,
)

from dcc_mcp_substance3d_designer.__version__ import __version__
from dcc_mcp_substance3d_designer._install_io import (
    hash_file as _hash_file,
)
from dcc_mcp_substance3d_designer._install_io import (
    write_bytes_atomic as _write_bytes_atomic,
)
from dcc_mcp_substance3d_designer._install_io import (
    write_json_atomic as _write_json_atomic,
)
from dcc_mcp_substance3d_designer._install_model import (
    COMMAND,
    DCC_TYPE,
    INSTALL_SOP_DOCUMENT_SCHEMA_VERSION,
    MIN_CORE_VERSION,
    InstallContext,
    LifecycleFailure,
    LifecycleOutcome,
)
from dcc_mcp_substance3d_designer._install_model import (
    PLUGIN_NAME as _PLUGIN_NAME,
)
from dcc_mcp_substance3d_designer._install_preflight import resolve_context as _resolve_context
from dcc_mcp_substance3d_designer._install_receipt import load_and_validate_receipt as _validate_receipt
from dcc_mcp_substance3d_designer._install_verify import verify as _verify

_LOCK_GUARD = threading.Lock()
_LOCKED_ROOTS: set[str] = set()


@contextmanager
def _install_lock(install_root: Path) -> Iterator[None]:
    """Own one install-root mutation in-process and across processes."""
    key = os.path.normcase(str(install_root.resolve()))
    lock_path = install_root / "locks" / f"{DCC_TYPE}.lock"
    with _LOCK_GUARD:
        if key in _LOCKED_ROOTS:
            raise LifecycleFailure(
                "busy", "A Designer lifecycle mutation is already in progress.", INSTALL_EXIT_INSTALL
            )
        _LOCKED_ROOTS.add(key)
    descriptor: Optional[int] = None
    identity: Optional[Tuple[int, int]] = None
    active_failure: Optional[BaseException] = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise LifecycleFailure(
                "busy", "A Designer lifecycle mutation is already in progress.", INSTALL_EXIT_INSTALL
            ) from exc
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        stat = os.fstat(descriptor)
        identity = (int(stat.st_dev), int(stat.st_ino))
        yield
    except BaseException as exc:
        active_failure = exc
        raise
    finally:
        release_failed = False
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                release_failed = True
        if identity is not None:
            try:
                current = os.stat(lock_path, follow_symlinks=False)
                if (int(current.st_dev), int(current.st_ino)) == identity:
                    lock_path.unlink()
                else:
                    release_failed = True
            except OSError:
                release_failed = True
        with _LOCK_GUARD:
            _LOCKED_ROOTS.discard(key)
        if release_failed:
            message = "Designer lifecycle lock release failed; manual cleanup is required."
            if active_failure is None:
                raise LifecycleFailure("cleanup", message, INSTALL_EXIT_INSTALL)
            warnings.warn(message, RuntimeWarning, stacklevel=2)


def _command(ctx: InstallContext, verb: str, *, execute: bool = False) -> Sequence[str]:
    command = [
        COMMAND,
        verb,
        "--dcc-path",
        str(ctx.host_path),
        "--python",
        str(ctx.python_path),
        "--json",
    ]
    if execute:
        command.append("--yes")
    return command


def _plan(ctx: InstallContext, verb: str) -> LifecycleOutcome:
    if verb == "uninstall":
        result = _base_result(ctx, status="planned")
        result["steps"] = [
            {"id": "receipt", "status": "ok" if ctx.receipt_path.exists() else "absent"},
            {"id": "uninstall", "status": "planned"},
        ]
        result["next_steps"] = [
            {
                "id": "execute_uninstall",
                "description": "Remove only the receipted Designer adapter files.",
                "command": list(_command(ctx, verb, execute=True)),
                "why": "Planning does not remove Designer adapter files.",
            }
        ]
        return LifecycleOutcome(result, INSTALL_EXIT_OK)
    result = _base_result(ctx, status="planned")
    result["steps"] = [
        {"id": "preflight", "status": "ok"},
        {"id": "install-launcher", "status": "planned"},
        {"id": "receipt", "status": "planned"},
        {"id": "verify", "status": "planned"},
    ]
    result["next_steps"] = [
        {
            "id": f"execute_{verb}",
            "description": f"Execute the validated Designer {verb} plan.",
            "command": list(_command(ctx, verb, execute=True)),
            "why": "Planning does not modify the Designer installation.",
        }
    ]
    return LifecycleOutcome(result, INSTALL_EXIT_OK)


def _base_result(ctx: InstallContext, *, status: str, verify: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result = {
        "schema_version": INSTALL_SOP_DOCUMENT_SCHEMA_VERSION,
        "status": status,
        "dcc_type": DCC_TYPE,
        "adapter_version": __version__,
        "core_version": ctx.core_version,
        "steps": [],
        "next_steps": [],
        "receipt_path": str(ctx.receipt_path),
        "verify": verify or {"directly_usable": False, "failure_stage": None, "failure_reason": None},
        "host": {
            "path": str(ctx.host_path),
            "version": ctx.host_version,
            "version_source": ctx.host_version_source,
            "embedded_python_version": ctx.embedded_python_version,
            "embedded_python_version_source": ctx.embedded_python_version_source,
        },
        "profile": {
            "plugin_search_path": str(ctx.plugin_path.parent),
            "selection_source": "receipted_launcher",
        },
        "python": {
            "path": str(ctx.python_path),
            "version": ctx.python_version,
            "site_packages": str(ctx.python_root),
            "adapter_module_path": None if ctx.adapter_module_path is None else str(ctx.adapter_module_path),
            "core_module_path": None if ctx.core_module_path is None else str(ctx.core_module_path),
            "adapter_distribution_root": (
                None if ctx.adapter_distribution_root is None else str(ctx.adapter_distribution_root)
            ),
            "core_distribution_root": None if ctx.core_distribution_root is None else str(ctx.core_distribution_root),
            "python_prefix": None if ctx.python_prefix is None else str(ctx.python_prefix),
            "server_module_path": None if ctx.server_module_path is None else str(ctx.server_module_path),
            "server_distribution_root": (
                None if ctx.server_distribution_root is None else str(ctx.server_distribution_root)
            ),
            "server_binary_path": None if ctx.server_binary_path is None else str(ctx.server_binary_path),
        },
        "install_state": ctx.state,
    }
    return result


def _plugin_source(ctx: InstallContext) -> str:
    return f'''"""Generated DCC-MCP Designer plugin. Owned by its install receipt."""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True
sys.path.insert(0, {json.dumps(str(ctx.python_root))})

from dcc_mcp_core import capture_bootstrap_errors

_CAPTURE = {{
    "dcc_name": "substance3d_designer",
    "adapter_version": {json.dumps(__version__)},
    "min_core_version": {json.dumps(MIN_CORE_VERSION)},
    "log_dir": {json.dumps(str(ctx.bootstrap_log_dir))},
}}

with capture_bootstrap_errors(phase="import", **_CAPTURE):
    from dcc_mcp_substance3d_designer.plugin import initializeSDPlugin as _initialize
    from dcc_mcp_substance3d_designer.plugin import uninitializeSDPlugin as _uninitialize


def initializeSDPlugin():
    with capture_bootstrap_errors(phase="startup", **_CAPTURE):
        return _initialize()


def uninitializeSDPlugin():
    with capture_bootstrap_errors(phase="shutdown", **_CAPTURE):
        return _uninitialize()


__all__ = ["initializeSDPlugin", "uninitializeSDPlugin"]
'''


def _batch_literal(value: str) -> str:
    if any(character in value for character in ('"', "\r", "\n", "\0")):
        raise LifecycleFailure("launcher", "Unsafe character in launcher path.", INSTALL_EXIT_INSTALL)
    return value.replace("%", "%%")


def _launcher_payload(ctx: InstallContext, *, platform_name: Optional[str] = None) -> bytes:
    plugin_root = str(ctx.plugin_path.parent)
    python_root = str(ctx.python_root)
    if (platform_name or os.name) == "nt":
        host = _batch_literal(str(ctx.host_path))
        plugins = _batch_literal(plugin_root)
        site_packages = _batch_literal(python_root)
        designer_python_path = f"{site_packages};{plugins}"
        content = (
            "@echo off\r\n"
            "setlocal DisableDelayedExpansion\r\n"
            "set \"PYTHONDONTWRITEBYTECODE=1\"\r\n"
            "if defined SBS_DESIGNER_PYTHON_PATH (\r\n"
            f'  set "SBS_DESIGNER_PYTHON_PATH=%SBS_DESIGNER_PYTHON_PATH%;{designer_python_path}"\r\n'
            ") else (\r\n"
            f'  set "SBS_DESIGNER_PYTHON_PATH={designer_python_path}"\r\n'
            ")\r\n"
            "if defined PYTHONPATH (\r\n"
            f'  set "PYTHONPATH={site_packages};%PYTHONPATH%"\r\n'
            ") else (\r\n"
            f'  set "PYTHONPATH={site_packages}"\r\n'
            ")\r\n"
            f'"{host}" %*\r\n'
        )
    else:
        content = (
            "#!/bin/sh\n"
            f"adapter_plugins={shlex.quote(plugin_root)}\n"
            f"python_root={shlex.quote(python_root)}\n"
            "export PYTHONDONTWRITEBYTECODE=1\n"
            'SBS_DESIGNER_PYTHON_PATH="${SBS_DESIGNER_PYTHON_PATH:+${SBS_DESIGNER_PYTHON_PATH}:}'
            '${python_root}:${adapter_plugins}"\n'
            'PYTHONPATH="${python_root}${PYTHONPATH:+:${PYTHONPATH}}"\n'
            "export SBS_DESIGNER_PYTHON_PATH PYTHONPATH\n"
            f'exec {shlex.quote(str(ctx.host_path))} "$@"\n'
        )
    return content.encode("utf-8")


def _receipt(ctx: InstallContext, installed_at: float) -> Dict[str, Any]:
    return {
        "schema_version": INSTALL_SOP_DOCUMENT_SCHEMA_VERSION,
        "dcc_type": DCC_TYPE,
        "adapter_version": __version__,
        "core_version": ctx.core_version,
        "host": {
            "path": str(ctx.host_path),
            "version": ctx.host_version,
            "version_source": ctx.host_version_source,
            "embedded_python_version": ctx.embedded_python_version,
            "embedded_python_version_source": ctx.embedded_python_version_source,
        },
        "profile": {
            "plugin_search_path": str(ctx.plugin_path.parent),
            "selection_source": "receipted_launcher",
        },
        "python": {
            "path": str(ctx.python_path),
            "version": ctx.python_version,
            "site_packages": str(ctx.python_root),
            "adapter_module_path": None if ctx.adapter_module_path is None else str(ctx.adapter_module_path),
            "core_module_path": None if ctx.core_module_path is None else str(ctx.core_module_path),
            "adapter_distribution_root": (
                None if ctx.adapter_distribution_root is None else str(ctx.adapter_distribution_root)
            ),
            "core_distribution_root": None if ctx.core_distribution_root is None else str(ctx.core_distribution_root),
            "python_prefix": None if ctx.python_prefix is None else str(ctx.python_prefix),
            "server_module_path": None if ctx.server_module_path is None else str(ctx.server_module_path),
            "server_distribution_root": (
                None if ctx.server_distribution_root is None else str(ctx.server_distribution_root)
            ),
            "server_binary_path": None if ctx.server_binary_path is None else str(ctx.server_binary_path),
        },
        "files": [{"path": str(path), "sha256": _hash_file(path)} for path in (ctx.plugin_path, ctx.launcher_path)],
        "installed_at": datetime.fromtimestamp(installed_at, timezone.utc).isoformat(),
        "installed_at_epoch": installed_at,
        "bootstrap_error_dir": str(ctx.bootstrap_log_dir),
    }


def _remove_transaction_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        removed = safe_remove_tree(path)
        if not removed.get("success"):
            raise LifecycleFailure("rollback", "Designer rollback could not remove a managed directory.")
    elif path.exists() or path.is_symlink():
        path.unlink()


def _rollback_path(current: Path, backup: Path, *, previously_existed: bool) -> None:
    if backup.exists():
        _remove_transaction_path(current)
        current.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(backup), str(current))
    elif not previously_existed:
        _remove_transaction_path(current)
    elif not current.exists():
        raise LifecycleFailure("rollback", "Designer rollback is missing a prior artifact.")


def _rollback_transaction(paths: Sequence[Tuple[Path, Path, bool]]) -> None:
    failed = False
    for current, backup, previously_existed in paths:
        try:
            _rollback_path(current, backup, previously_existed=previously_existed)
        except (LifecycleFailure, OSError):
            failed = True
    if failed:
        raise LifecycleFailure("rollback", "Designer rollback could not restore every managed artifact.")


def _execute_install(ctx: InstallContext, environ: Mapping[str, str]) -> LifecycleOutcome:
    if ctx.state == "partial":
        raise LifecycleFailure("partial", "Designer adapter files exist without a matching receipt.")
    if ctx.receipt_path.exists():
        _validate_receipt(
            ctx,
            allow_file_drift=ctx.state == "repair",
            allow_adapter_mismatch=ctx.state == "upgrade",
        )
    inspection = inspect_install_root(ctx.payload_root)
    if inspection.get("requires_restart"):
        result = _base_result(ctx, status="requires_restart")
        result["steps"] = [{"id": "preflight", "status": "requires_restart"}]
        result["next_steps"] = [
            {
                "id": "retry_install",
                "description": "Close Designer and repeat the install command.",
                "command": list(_command(ctx, "install", execute=True)),
                "why": "Loaded files prevent safe staged replacement.",
            }
        ]
        return LifecycleOutcome(result, INSTALL_EXIT_REQUIRES_RESTART)

    transaction = ctx.install_root / "staging" / uuid.uuid4().hex
    staged_payload = transaction / "payload"
    staged_plugin = staged_payload / "plugins" / _PLUGIN_NAME
    backup_payload = transaction / "backup" / "payload"
    backup_launcher = transaction / "backup" / ctx.launcher_path.name
    backup_receipt = transaction / "backup" / ctx.receipt_path.name
    previous_payload = ctx.payload_root.exists()
    previous_launcher = ctx.launcher_path.exists()
    previous_receipt = ctx.receipt_path.exists()
    rollback_paths = (
        (ctx.payload_root, backup_payload, previous_payload),
        (ctx.launcher_path, backup_launcher, previous_launcher),
        (ctx.receipt_path, backup_receipt, previous_receipt),
    )
    verify: Optional[Dict[str, Any]] = None
    next_steps: Sequence[Dict[str, Any]] = []
    staged_plugin.parent.mkdir(parents=True)
    staged_plugin.write_text(_plugin_source(ctx), encoding="utf-8")
    installed_at = time.time()
    try:
        backup_payload.parent.mkdir(parents=True, exist_ok=True)
        if ctx.payload_root.exists():
            shutil.copytree(str(ctx.payload_root), str(backup_payload))
        if ctx.launcher_path.exists():
            shutil.copy2(str(ctx.launcher_path), str(backup_launcher))
        if ctx.receipt_path.exists():
            shutil.copy2(str(ctx.receipt_path), str(backup_receipt))
        replaced = safe_replace_tree(staged_payload, ctx.payload_root)
        if not replaced.get("success"):
            exit_code = INSTALL_EXIT_REQUIRES_RESTART if replaced.get("requires_restart") else INSTALL_EXIT_INSTALL
            raise LifecycleFailure("install", "Designer payload could not be replaced safely.", exit_code)
        _write_bytes_atomic(ctx.launcher_path, _launcher_payload(ctx), 0o700)
        _write_json_atomic(ctx.receipt_path, _receipt(ctx, installed_at))
        verify, next_steps = _verify(ctx, environ)
        if ctx.state in {"current", "upgrade"} and not verify["directly_usable"]:
            _rollback_transaction(rollback_paths)
            result = _base_result(ctx, status="partial", verify=verify)
            result["previous_restored"] = True
            result["steps"] = [
                {"id": "preflight", "status": "ok"},
                {"id": "install-launcher", "status": "rolled_back"},
                {"id": "receipt", "status": "restored"},
                {"id": "verify", "status": "failed"},
            ]
            result["next_steps"] = list(next_steps)
            return LifecycleOutcome(result, INSTALL_EXIT_VERIFY)
    except BaseException:
        _rollback_transaction(rollback_paths)
        raise
    finally:
        if transaction.exists():
            cleaned = safe_remove_tree(transaction)
            if not cleaned.get("success"):
                raise LifecycleFailure("cleanup", "Designer staging cleanup did not complete.", INSTALL_EXIT_INSTALL)
    if verify is None:
        raise LifecycleFailure("install", "Designer install verification did not run.", INSTALL_EXIT_INSTALL)
    usable = bool(verify["directly_usable"])
    result = _base_result(ctx, status="ok" if usable else "partial", verify=verify)
    result["steps"] = [
        {"id": "preflight", "status": "ok"},
        {"id": "install-launcher", "status": "ok"},
        {"id": "receipt", "status": "ok"},
        {"id": "verify", "status": "ok" if usable else "failed"},
    ]
    result["next_steps"] = list(next_steps)
    return LifecycleOutcome(result, INSTALL_EXIT_OK if usable else INSTALL_EXIT_VERIFY)


def _execute_uninstall(ctx: InstallContext) -> LifecycleOutcome:
    if not ctx.receipt_path.exists():
        if ctx.payload_root.exists() or ctx.launcher_path.exists():
            raise LifecycleFailure("partial", "Designer adapter files exist without an install receipt.")
        result = _base_result(ctx, status="ok")
        result["steps"] = [{"id": "uninstall", "status": "already_absent"}]
        return LifecycleOutcome(result, INSTALL_EXIT_OK)
    _validate_receipt(ctx, allow_adapter_mismatch=True)
    transaction = ctx.install_root / "staging" / uuid.uuid4().hex
    backup_payload = transaction / "backup" / "payload"
    backup_launcher = transaction / "backup" / ctx.launcher_path.name
    backup_receipt = transaction / "backup" / ctx.receipt_path.name
    transaction.mkdir(parents=True, exist_ok=False)
    payload_removed = False
    try:
        backup_payload.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ctx.payload_root, backup_payload, symlinks=True)
        shutil.copy2(ctx.launcher_path, backup_launcher)
        shutil.copy2(ctx.receipt_path, backup_receipt)
        removed = safe_remove_tree(ctx.payload_root)
        if not removed.get("success"):
            exit_code = INSTALL_EXIT_REQUIRES_RESTART if removed.get("requires_restart") else INSTALL_EXIT_INSTALL
            raise LifecycleFailure("uninstall", "Designer payload could not be removed safely.", exit_code)
        payload_removed = True
        ctx.launcher_path.unlink()
        ctx.receipt_path.unlink()
    except BaseException:
        if not payload_removed:
            raise
        failed = False
        try:
            if ctx.payload_root.exists():
                removed = safe_remove_tree(ctx.payload_root)
                failed = not removed.get("success")
            if backup_payload.exists() and not ctx.payload_root.exists():
                shutil.copytree(backup_payload, ctx.payload_root, symlinks=True)
            for backup, target in ((backup_launcher, ctx.launcher_path), (backup_receipt, ctx.receipt_path)):
                if backup.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, target)
        except OSError:
            failed = True
        if failed:
            raise LifecycleFailure(
                "rollback", "Designer uninstall rollback could not restore every artifact."
            ) from None
        raise
    finally:
        if transaction.exists():
            cleaned = safe_remove_tree(transaction)
            if not cleaned.get("success"):
                raise LifecycleFailure("cleanup", "Designer staging cleanup did not complete.", INSTALL_EXIT_INSTALL)
    result = _base_result(ctx, status="ok")
    result["steps"] = [{"id": "receipt", "status": "consumed"}, {"id": "uninstall", "status": "ok"}]
    return LifecycleOutcome(result, INSTALL_EXIT_OK)


def _status(ctx: InstallContext) -> LifecycleOutcome:
    incomplete = ctx.state in {"partial", "repair"}
    result = _base_result(ctx, status="partial" if incomplete else "ok")
    result["steps"] = [
        {"id": "receipt", "status": "ok" if ctx.receipt_path.exists() else "absent"},
        {
            "id": "artifacts",
            "status": "present" if ctx.plugin_path.exists() and ctx.launcher_path.exists() else "absent",
        },
    ]
    return LifecycleOutcome(result, INSTALL_EXIT_PREFLIGHT if incomplete else INSTALL_EXIT_OK)


def _verify_outcome(ctx: InstallContext, environ: Mapping[str, str]) -> LifecycleOutcome:
    verify, next_steps = _verify(ctx, environ)
    usable = bool(verify["directly_usable"])
    result = _base_result(ctx, status="ok" if usable else "failed", verify=verify)
    result["steps"] = [{"id": "verify", "status": "ok" if usable else "failed"}]
    result["next_steps"] = list(next_steps)
    return LifecycleOutcome(result, INSTALL_EXIT_OK if usable else INSTALL_EXIT_VERIFY)


def _failure_result(
    failure: LifecycleFailure,
    context: Optional[InstallContext] = None,
    verb: Optional[str] = None,
) -> LifecycleOutcome:
    requires_restart = failure.exit_code == INSTALL_EXIT_REQUIRES_RESTART
    verify = {
        "directly_usable": False,
        "failure_stage": failure.stage,
        "failure_reason": str(failure),
    }
    if context is None:
        result = {
            "schema_version": INSTALL_SOP_DOCUMENT_SCHEMA_VERSION,
            "status": "requires_restart" if requires_restart else "failed",
            "dcc_type": DCC_TYPE,
            "adapter_version": __version__,
            "core_version": str(getattr(dcc_mcp_core, "__version__", "unknown")),
            "steps": [{"id": "preflight", "status": "failed", "message": str(failure)}],
            "next_steps": [],
            "receipt_path": None,
            "verify": verify,
        }
    else:
        result = _base_result(
            context,
            status="requires_restart" if requires_restart else "failed",
            verify=verify,
        )
        result["steps"] = [
            {
                "id": failure.stage,
                "status": "requires_restart" if requires_restart else "failed",
                "message": str(failure),
            }
        ]
        if requires_restart and verb:
            result["next_steps"] = [
                {
                    "id": f"retry_{verb}",
                    "description": f"Close Designer and retry the {verb} operation.",
                    "command": list(_command(context, verb, execute=True)),
                    "why": "Core reported a loaded or locked artifact under the install root.",
                }
            ]
    return LifecycleOutcome(result, failure.exit_code)


def run_lifecycle(
    verb: str,
    *,
    dcc_path: Optional[str],
    python_path: Optional[str],
    yes: bool,
    dry_run: bool,
    environ: Optional[Mapping[str, str]] = None,
) -> LifecycleOutcome:
    """Run one public Designer lifecycle verb."""
    resolved_environ = os.environ if environ is None else environ
    context: Optional[InstallContext] = None
    try:
        context = _resolve_context(dcc_path, python_path, resolved_environ)
        if verb in {"install", "upgrade"}:
            if dry_run or not yes:
                return _plan(context, verb)
            with _install_lock(context.install_root):
                context = _resolve_context(dcc_path, python_path, resolved_environ)
                return _execute_install(context, resolved_environ)
        if verb == "uninstall":
            if dry_run or not yes:
                return _plan(context, verb)
            with _install_lock(context.install_root):
                context = _resolve_context(dcc_path, python_path, resolved_environ)
                return _execute_uninstall(context)
        if verb == "status":
            return _status(context)
        if verb == "verify":
            return _verify_outcome(context, resolved_environ)
        raise LifecycleFailure("verb", f"Lifecycle verb is not implemented yet: {verb}")
    except LifecycleFailure as exc:
        return _failure_result(exc, context, verb)
    except BaseException:
        return _failure_result(
            LifecycleFailure("install", "Designer lifecycle operation failed safely.", INSTALL_EXIT_INSTALL),
            context,
            verb,
        )


__all__ = ["COMMAND", "DCC_TYPE", "LifecycleOutcome", "run_lifecycle"]
