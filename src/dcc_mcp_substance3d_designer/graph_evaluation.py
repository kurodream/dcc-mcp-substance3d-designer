"""Bounded graph evaluation and isolated, artifact-verified map export."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import graph_authoring as api
from .graph_inspection import checked_graph, graph_identity
from .image_metadata import read_image_metadata
from .render_process import run_renderer

_OUTPUT_INPUT = "inputNodeOutput"


def _graph_output_ids(graph: Any) -> set[str]:
    return {api.node_identifier(node) for node in api.items(graph.getOutputNodes())}


def _pin_output_identifier(name: str, used: set[str]) -> str:
    """Build a unique Designer output identifier for one temporary pin."""
    base = api.require_identifier(f"mcp_pin_{name}")
    candidate = base
    suffix = 2
    while candidate.casefold() in used:
        candidate = api.require_identifier(f"{base}_{suffix}")
        suffix += 1
    used.add(candidate.casefold())
    return candidate


def _pin_intermediates_for_export(
    graph: Any, selected: list[tuple[str, Any, Any]], expected_graph_uid: str
) -> tuple[list[tuple[str, str, str, Any, Any]], list[str]]:
    """Pin non-output selections to temporary graph outputs so compute retains textures.

    Temporary outputs must receive unique annotation identifiers. Anonymous
    ``sbs::compositing::output`` nodes collide in Designer and make
    ``getPropertyValue`` / ``SDTexture.save`` return the same bitmap for every
    pin in a batch (and across later exports).

    Returns resolved rows ``(name, source_node_id, source_property, read_node, read_prop)``
    and created temp output node ids for rollback.
    """
    from sd.api.sdproperty import SDPropertyCategory

    from .graph_inspection import find_in_graph

    output_ids = _graph_output_ids(graph)
    identifiers = getattr(graph, "getOutputIdentifiers", None)
    used_identifiers = {
        str(api.json_value(item)).casefold()
        for item in api.items(identifiers() if callable(identifiers) else None)
        if api.json_value(item) is not None
    }
    resolved: list[tuple[str, str, str, Any, Any]] = []
    created: list[str] = []
    try:
        for name, node, prop in selected:
            source_id = api.node_identifier(node)
            source_property = prop.getId()
            if source_id in output_ids:
                resolved.append((name, source_id, source_property, node, prop))
                continue
            pin_id = _pin_output_identifier(name, used_identifiers)
            temp = api.add_output(pin_id)
            temp_id = temp["node_id"]
            created.append(temp_id)
            api.connect_nodes(source_id, source_property, temp_id, _OUTPUT_INPUT, expected_graph_uid)
            graph = checked_graph(expected_graph_uid)
            pinned = find_in_graph(graph, temp_id)
            ports = api.items(pinned.getProperties(SDPropertyCategory.Output))
            if not ports:
                raise api.GraphAuthoringError(
                    f"Temporary output for {name} has no output property", "OUTPUT_NOT_COMPUTED"
                )
            resolved.append((name, source_id, source_property, pinned, ports[0]))
            output_ids.add(temp_id)
        return resolved, created
    except BaseException:
        api.remove_created(created)
        raise


def export_native_maps(
    output_dir: str, outputs: list[dict], expected_graph_uid: str, max_resolution: int = 2048
) -> dict:
    from sd.api.sdproperty import SDPropertyCategory

    from .graph_inspection import find_in_graph

    if not isinstance(outputs, list) or not 1 <= len(outputs) <= 8:
        raise api.GraphAuthoringError("Select one to eight named texture outputs", "INVALID_GRAPH_OUTPUTS")
    graph = checked_graph(expected_graph_uid)
    selected, names = [], set()
    for item in outputs:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) for key in ("name", "node_id", "property")
        ):
            raise api.GraphAuthoringError(
                "Each output requires name, node_id, and property strings", "INVALID_GRAPH_OUTPUTS"
            )
        name = api.require_identifier(item["name"])
        if name.casefold() in names:
            raise api.GraphAuthoringError("Output names must be unique", "INVALID_GRAPH_OUTPUTS")
        names.add(name.casefold())
        node = find_in_graph(graph, item["node_id"])
        prop = node.getPropertyFromId(api.require_property(item["property"]), SDPropertyCategory.Output)
        if prop is None:
            raise api.GraphAuthoringError("Selected output property is missing", "PORT_NOT_FOUND")
        selected.append((name, node, prop))
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise api.GraphAuthoringError("Use a fresh output directory", "OUTPUT_EXISTS")
    created: list[str] = []
    try:
        resolved, created = _pin_intermediates_for_export(graph, selected, expected_graph_uid)
        # Temp outputs increase node count; keep the same evaluation budget as before.
        evaluate_graph(expected_graph_uid, 128 + len(created), max_resolution)
        destination.parent.mkdir(parents=True, exist_ok=True)
        files = []
        # Validate all files in owned storage before publishing the directory.
        # Save each texture immediately after readback: Designer SDTexture handles
        # can alias a shared buffer, so delaying save across the batch rewrites
        # every PNG with the last handle's pixels.
        with tempfile.TemporaryDirectory(prefix=".designer-maps-", dir=destination.parent) as temporary:
            staging = Path(temporary) / "maps"
            staging.mkdir()
            for name, source_id, source_property, read_node, read_prop in resolved:
                wrapped = read_node.getPropertyValue(read_prop)
                texture = wrapped.get() if wrapped else None
                if texture is None:
                    raise api.GraphAuthoringError(
                        f"Output {name} ({source_id}.{source_property}) did not compute after pinning "
                        "a temporary graph output. Check that the source node evaluates and that "
                        "Absolute $outputsize stays within the resolution budget.",
                        "OUTPUT_NOT_COMPUTED",
                    )
                size = texture.getSize()
                if not 0 < size.x <= max_resolution or not 0 < size.y <= max_resolution:
                    raise api.GraphAuthoringError("Texture exceeds the resolution budget", "RESOLUTION_LIMIT")
                path = staging / (name + ".png")
                if texture.save(str(path)) is False or not path.is_file() or not path.stat().st_size:
                    raise api.GraphAuthoringError(f"SDK did not save output {name}", "MAP_OUTPUT_MISSING")
                metadata = read_image_metadata(path, "png", None)
                if (metadata["width"], metadata["height"]) != (size.x, size.y):
                    raise api.GraphAuthoringError(
                        f"Saved output {name} dimensions do not match SDK readback", "MAP_HEADER_INVALID"
                    )
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                pixel_format = texture.getPixelFormat()
                files.append(
                    {
                        "name": name,
                        "node_id": source_id,
                        "property": source_property,
                        "pixel_format": str(getattr(pixel_format, "name", pixel_format)),
                        "path": str(destination / path.name),
                        "sha256": digest.hexdigest(),
                        "image_metadata": metadata,
                    }
                )
            if destination.exists() or destination.is_symlink():
                raise api.GraphAuthoringError("Use a fresh output directory", "OUTPUT_EXISTS")
            staging.rename(destination)
        return {
            "graph_uid": expected_graph_uid,
            "files": files,
            "precision_policy": "native SDK texture precision; no inferred height precision",
            "pinned_intermediates": len(created),
        }
    finally:
        api.remove_created(created)


def validate_resolution_budget(graph: Any, max_resolution: int) -> None:
    from sd.api.sdproperty import SDPropertyCategory

    if type(max_resolution) is not int or max_resolution not in (256, 512, 1024, 2048, 4096):
        raise api.GraphAuthoringError("Unsupported resolution budget", "INVALID_RESOLUTION_BUDGET")
    maximum = max_resolution.bit_length() - 1
    prop = graph.getPropertyFromId("$outputsize", SDPropertyCategory.Input)
    if prop is None or graph.getPropertyInheritanceMethod(prop).name != "Absolute":
        raise api.GraphAuthoringError(
            "Set an absolute graph output size before bounded evaluation", "RESOLUTION_UNPROVEN"
        )
    base = api.json_value(graph.getPropertyValue(prop))

    def checked(size):
        if (
            not isinstance(size, list)
            or len(size) != 2
            or any(type(x) is not int or not 0 <= x <= maximum for x in size)
        ):
            raise api.GraphAuthoringError("Output size exceeds the resolution budget", "RESOLUTION_LIMIT")

    checked(base)
    for node in api.items(graph.getNodes()):
        size_prop = node.getPropertyFromId("$outputsize", SDPropertyCategory.Input)
        if size_prop is None:
            continue
        if node.getPropertyGraph(size_prop) is not None:
            raise api.GraphAuthoringError("Dynamic output size cannot be bounded statically", "RESOLUTION_UNPROVEN")
        size = api.json_value(node.getPropertyValue(size_prop))
        if not isinstance(size, list) or len(size) != 2 or any(type(x) is not int for x in size):
            raise api.GraphAuthoringError("Node output size is unavailable", "RESOLUTION_UNPROVEN")
        method = node.getPropertyInheritanceMethod(size_prop).name
        if method == "Absolute":
            checked(size)
        elif method == "RelativeToParent":
            checked([a + b for a, b in zip(base, size)])
        elif method == "RelativeToInput" and all(x <= 0 for x in size):
            continue  # With no amplification, upstream sizes remain within this budget.
        else:
            raise api.GraphAuthoringError(
                "Input-relative amplification cannot be bounded statically", "RESOLUTION_UNPROVEN"
            )


def evaluate_graph(expected_graph_uid: str, max_nodes: int = 1000, max_resolution: int = 2048) -> dict[str, Any]:
    from sd.api.sdproperty import SDPropertyCategory

    if type(max_nodes) is not int or not 1 <= max_nodes <= 1000:
        raise api.GraphAuthoringError("max_nodes must be 1..1000", "INVALID_EVALUATION_LIMIT")
    graph = checked_graph(expected_graph_uid)
    if len(api.items(graph.getNodes())) > max_nodes:
        raise api.GraphAuthoringError("Graph exceeds the evaluation node budget", "EVALUATION_LIMIT")
    output_nodes = api.items(graph.getOutputNodes())
    if not output_nodes:
        raise api.GraphAuthoringError("Graph has no outputs", "NO_GRAPH_OUTPUTS")
    validate_resolution_budget(graph, max_resolution)
    graph.compute()  # SDK is synchronous. The main-thread tool is dispatched async.
    outputs = []
    for node in output_nodes:
        ports = api.items(node.getProperties(SDPropertyCategory.Output))
        if not ports:
            raise api.GraphAuthoringError("Output node has no output property", "OUTPUT_NOT_COMPUTED")
        for prop in ports:
            result = node.getPropertyValue(prop)
            texture = result.get() if result is not None else None
            if texture is None:
                raise api.GraphAuthoringError("An output did not compute", "OUTPUT_NOT_COMPUTED")
            size = texture.getSize()
            if size.x <= 0 or size.y <= 0:
                raise api.GraphAuthoringError("Output texture dimensions are invalid", "OUTPUT_NOT_COMPUTED")
            pixel_format = texture.getPixelFormat()
            outputs.append(
                {
                    "node_id": api.node_identifier(node),
                    "property": prop.getId(),
                    "width": size.x,
                    "height": size.y,
                    "pixel_format": str(getattr(pixel_format, "name", pixel_format)),
                }
            )
    return {"graph_uid": graph_identity(graph), "computed": True, "outputs": outputs}


def export_maps(
    output_dir: str,
    image_format: str = "png",
    bit_depth: str = "8",
    color_space: str = "Raw",
    expected_graph_uid: str | None = None,
    output_color_spaces: dict[str, str] | None = None,
) -> dict[str, Any]:
    formats = {"png": {"8", "16"}, "tga": {"8"}, "tiff": {"8", "16", "16f", "32f"}, "exr": {"16f", "32f"}}
    if image_format not in formats or bit_depth not in formats[image_format]:
        raise api.GraphAuthoringError("Image format and bit depth are incompatible", "INVALID_EXPORT_FORMAT")
    if not isinstance(color_space, str) or not api._COLOR_SPACE.fullmatch(color_space):
        raise api.GraphAuthoringError("Invalid output color space", "INVALID_COLOR_SPACE")
    graph = checked_graph(expected_graph_uid)
    graph_id = api.require_identifier(graph.getIdentifier(), "graph_id")
    identifiers = [
        api.require_node_id(api.json_value(item), "output_id") for item in api.items(graph.getOutputIdentifiers())
    ]
    if not identifiers or len(set(identifiers)) != len(identifiers):
        raise api.GraphAuthoringError("Output identifiers must be present and unique", "INVALID_GRAPH_OUTPUTS")
    spaces = output_color_spaces or {}
    if not isinstance(spaces, dict) or set(spaces) - set(identifiers):
        raise api.GraphAuthoringError("Color-space overrides must name graph outputs", "INVALID_OUTPUT_OVERRIDE")
    if any(not isinstance(space, str) or not api._COLOR_SPACE.fullmatch(space) for space in spaces.values()):
        raise api.GraphAuthoringError("Invalid per-output color space", "INVALID_COLOR_SPACE")
    executable = shutil.which("sbsrender")
    if executable is None:
        raise api.GraphAuthoringError("Official sbsrender is unavailable", "SBSRENDER_UNAVAILABLE")
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    # A fresh directory prevents any previous file from proving export success.
    run_dir = Path(tempfile.mkdtemp(prefix="designer-export-", dir=destination))
    from sd.api.sbs.sdsbsarexporter import SDSBSARExporter

    archive = run_dir / "material.sbsar"
    SDSBSARExporter.sNew().exportPackageToSBSAR(graph.getPackage(), str(archive))
    if not archive.is_file() or not archive.stat().st_size:
        raise api.GraphAuthoringError("Designer did not compile an SBSAR", "SBSAR_EXPORT_FAILED")
    command = [
        executable,
        "render",
        "--input",
        str(archive),
        "--input-graph",
        graph_id,
        "--output-path",
        str(run_dir),
        "--output-name",
        "{outputNodeName}",
        "--output-format",
        image_format,
        "--output-bit-depth",
        bit_depth,
        "--output-colorspace",
        color_space,
    ]
    for identifier, space in sorted(spaces.items()):
        command.extend(["--set-output-colorspace", f"{identifier}@{space}"])
    try:
        result = run_renderer(command, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise api.GraphAuthoringError("sbsrender timed out", "MAP_EXPORT_TIMEOUT") from exc
    if result.returncode != 0:
        raise api.GraphAuthoringError("sbsrender failed", "MAP_EXPORT_FAILED")
    files = []
    for identifier in identifiers:
        path = run_dir / f"{identifier}.{image_format}"
        if not path.is_file() or path.is_symlink() or not path.stat().st_size:
            raise api.GraphAuthoringError(f"Missing or empty output: {identifier}", "MAP_OUTPUT_MISSING")
        metadata = read_image_metadata(path, image_format, bit_depth)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        files.append(
            {
                "output_id": identifier,
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": digest.hexdigest(),
                "image_metadata": metadata,
                "color_space": spaces.get(identifier, color_space),
            }
        )
    return {
        "graph_uid": graph_identity(graph),
        "output_dir": str(run_dir),
        "image_format": image_format,
        "bit_depth": bit_depth,
        "color_space": color_space,
        "files": files,
        "sbsar_path": str(archive),
    }
