"""Strict receipt ownership checks shared by lifecycle operations."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from dcc_mcp_substance3d_designer.__version__ import __version__
from dcc_mcp_substance3d_designer._install_io import hash_file, load_json
from dcc_mcp_substance3d_designer._install_model import (
    DCC_TYPE,
    INSTALL_SOP_DOCUMENT_SCHEMA_VERSION,
    InstallContext,
    LifecycleFailure,
)

_VERSION_RE = re.compile(r"(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5})){2}")


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _is_link_or_junction(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _payload_entries(root: Path) -> Iterable[Path]:
    if not root.exists():
        return ()
    return tuple(
        path
        for path in root.rglob("*")
        if (path.is_file() or _is_link_or_junction(path))
        and not (path.parent.name == "__pycache__" and path.suffix == ".pyc")
    )


def load_and_validate_receipt(
    ctx: InstallContext,
    *,
    allow_file_drift: bool = False,
    allow_adapter_mismatch: bool = False,
) -> Dict[str, Any]:
    """Require exact current ownership before verify, upgrade, or uninstall."""
    receipt = load_json(ctx.receipt_path)
    expected_paths = (ctx.plugin_path.resolve(), ctx.launcher_path.resolve())
    files = receipt.get("files")
    adapter_version = receipt.get("adapter_version")
    if (
        receipt.get("schema_version") != INSTALL_SOP_DOCUMENT_SCHEMA_VERSION
        or receipt.get("dcc_type") != DCC_TYPE
        or not isinstance(adapter_version, str)
        or len(adapter_version) > 32
        or _VERSION_RE.fullmatch(adapter_version) is None
        or (not allow_adapter_mismatch and adapter_version != __version__)
        or not isinstance(files, list)
        or len(files) != len(expected_paths)
    ):
        raise LifecycleFailure("receipt", "Designer install receipt does not match this adapter contract.")
    recorded: Dict[str, Mapping[str, Any]] = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise LifecycleFailure("receipt", "Designer install receipt has invalid file ownership.")
        candidate = Path(item["path"])
        key = os.path.normcase(str(candidate.resolve()))
        if key in recorded:
            raise LifecycleFailure("receipt", "Designer install receipt contains duplicate file ownership.")
        recorded[key] = item
    if set(recorded) != {os.path.normcase(str(path)) for path in expected_paths}:
        raise LifecycleFailure("receipt", "Designer install receipt does not own the exact managed files.")
    for expected in expected_paths:
        item = recorded[os.path.normcase(str(expected))]
        if allow_file_drift:
            if (expected.exists() or expected.is_symlink()) and (
                not expected.is_file() or _is_link_or_junction(expected)
            ):
                raise LifecycleFailure("receipt", "A receipted Designer artifact has an unsafe file type.")
            continue
        if not expected.is_file() or _is_link_or_junction(expected) or hash_file(expected) != item.get("sha256"):
            raise LifecycleFailure("receipt", "A receipted Designer artifact is missing or changed.")
    payload_entries = tuple(_payload_entries(ctx.payload_root))
    expected_payload = () if not ctx.plugin_path.exists() and allow_file_drift else (ctx.plugin_path,)
    if len(payload_entries) != len(expected_payload) or any(
        not _same_path(actual, expected) for actual, expected in zip(payload_entries, expected_payload)
    ):
        raise LifecycleFailure("receipt", "Designer payload contains content not owned by the receipt.")
    expected_python = {
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
        "server_distribution_root": None if ctx.server_distribution_root is None else str(ctx.server_distribution_root),
        "server_binary_path": None if ctx.server_binary_path is None else str(ctx.server_binary_path),
    }
    if receipt.get("python") != expected_python:
        raise LifecycleFailure("receipt", "Designer receipt interpreter provenance no longer matches preflight.")
    host = receipt.get("host")
    if not isinstance(host, dict) or host.get("version") != ctx.host_version:
        raise LifecycleFailure("receipt", "Designer receipt host version no longer matches preflight.")
    try:
        if not _same_path(Path(str(host.get("path") or "")), ctx.host_path):
            raise LifecycleFailure("receipt", "Designer receipt host path no longer matches preflight.")
    except (OSError, ValueError):
        raise LifecycleFailure("receipt", "Designer receipt host path is invalid.") from None
    return receipt


__all__ = ["load_and_validate_receipt"]
