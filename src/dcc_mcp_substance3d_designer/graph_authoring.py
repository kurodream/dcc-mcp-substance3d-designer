"""Typed, host-bound graph authoring primitives for Substance 3D Designer."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_PROPERTY_ID = re.compile(r"^\$?[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_TYPE_URL = re.compile(r"^sbs(?:::[A-Za-z0-9_.-]+)+$")
_COLOR_SPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. +()-]{0,127}$")


class GraphAuthoringError(RuntimeError):
    """Stable fail-closed error for a typed graph operation."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def value(obj: Any, *names: str) -> Any:
    for name in names:
        member = getattr(obj, name, None)
        if member is not None:
            return member() if callable(member) else member
    return None


def items(collection: Any) -> list[Any]:
    if collection is None:
        return []
    resolved = value(collection, "get")
    if resolved is not None and resolved is not collection:
        collection = resolved
    if isinstance(collection, (list, tuple)):
        return list(collection)
    try:
        return list(collection)
    except TypeError:
        size = value(collection, "getSize")
        if not isinstance(size, int):
            return []
        get_item = getattr(collection, "getItem", None)
        if callable(get_item):
            return [get_item(index) for index in range(size)]
        return [collection[index] for index in range(size)]


def require_identifier(identifier: str, label: str = "identifier") -> str:
    if not isinstance(identifier, str):
        raise GraphAuthoringError(f"{label} must be a string", "INVALID_IDENTIFIER")
    resolved = str(identifier).strip()
    if not _IDENTIFIER.fullmatch(resolved):
        raise GraphAuthoringError(
            f"{label} must start with a letter and contain only letters, digits, or underscores",
            "INVALID_IDENTIFIER",
        )
    return resolved


def require_type_url(type_url: str) -> str:
    if not isinstance(type_url, str):
        raise GraphAuthoringError("type_url must be a string", "INVALID_TYPE_URL")
    resolved = str(type_url).strip()
    if len(resolved) > 160 or not _TYPE_URL.fullmatch(resolved):
        raise GraphAuthoringError(
            "type_url must be a bounded built-in sbs:: namespace URL",
            "INVALID_TYPE_URL",
        )
    return resolved


def require_property(identifier: str, label: str = "property") -> str:
    if not isinstance(identifier, str):
        raise GraphAuthoringError(f"{label} must be a string", "INVALID_PROPERTY_ID")
    resolved = str(identifier).strip()
    if not _PROPERTY_ID.fullmatch(resolved):
        raise GraphAuthoringError(
            f"{label} must be a bounded Designer property identifier",
            "INVALID_PROPERTY_ID",
        )
    return resolved


def active_graph() -> Any:
    import sd  # Lazy import: Designer host only.

    application = sd.getContext().getSDApplication()
    graph = None
    for context_name in ("getUIMgr", "getQtForPythonUIMgr", "getLocationContext"):
        ui_context = value(application, context_name)
        graph = value(ui_context, "getCurrentGraph") if ui_context else None
        if graph is not None:
            break
    if graph is None:
        raise GraphAuthoringError("No active Designer graph", "NO_ACTIVE_GRAPH")
    return graph


def application() -> Any:
    import sd  # Lazy import: Designer host only.

    return sd.getContext().getSDApplication()


def package_manager() -> Any:
    return application().getPackageMgr()


def active_package() -> Any:
    graph = active_graph()
    package = value(graph, "getPackage", "getParent")
    if package is None:
        raise GraphAuthoringError("Active graph package is unavailable", "NO_ACTIVE_PACKAGE")
    return package


def package_path(package: Any) -> str | None:
    resolved = value(package, "getFilePath")
    return str(resolved) if resolved else None


def node_identifier(node: Any) -> str:
    identifier = value(node, "getIdentifier", "getId", "getUID")
    if identifier is None:
        raise GraphAuthoringError("Designer did not return a node identifier", "MISSING_NODE_ID")
    return str(identifier)


def require_node_id(identifier: str, label: str = "node_id") -> str:
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
        raise GraphAuthoringError(f"{label} must be a bounded native node identifier", "INVALID_IDENTIFIER")
    return identifier


def name_node(node: Any, name: str) -> bool:
    """Assign an identifier only on SDKs that expose a writable identifier.

    Compositing nodes have read-only native IDs. Their annotation properties
    are not arbitrary user metadata, so never invent a label property.
    """
    setter = getattr(node, "setIdentifier", None)
    if callable(setter):
        setter(name)
        return True
    return False


def create_node(
    type_url: str,
    node_id: str | None = None,
    position: list[float] | tuple[float, float] | None = None,
) -> dict[str, Any]:
    resolved_type = require_type_url(type_url)
    resolved_id = require_identifier(node_id, "node_id") if node_id is not None else None
    resolved_position = position if position is not None else [0.0, 0.0]
    if len(resolved_position) != 2:
        raise GraphAuthoringError("position must contain exactly two numbers", "INVALID_POSITION")
    try:
        xy = [float(resolved_position[0]), float(resolved_position[1])]
    except (TypeError, ValueError) as exc:
        raise GraphAuthoringError("position must contain exactly two numbers", "INVALID_POSITION") from exc
    if any(not math.isfinite(component) or abs(component) > 1_000_000 for component in xy):
        raise GraphAuthoringError("position components are out of range", "INVALID_POSITION")

    from sd.api.sdbasetypes import float2

    graph = active_graph()
    node = graph.newNode(resolved_type)
    if node is None:
        raise GraphAuthoringError("Designer rejected the requested node type", "NODE_TYPE_UNAVAILABLE")
    try:
        assigned = name_node(node, resolved_id) if resolved_id is not None else False
        node.setPosition(float2(*xy))
        return {
            "node_id": node_identifier(node),
            "type_url": resolved_type,
            "position": xy,
            "requested_node_id": resolved_id,
            "identifier_assigned": assigned,
        }
    except BaseException:
        graph.deleteNode(node)
        raise


def create_graph(graph_id: str, open_in_editor: bool = True) -> dict[str, Any]:
    identifier = require_identifier(graph_id, "graph_id")

    from sd.api.sbs.sdsbscompgraph import SDSBSCompGraph

    package = package_manager().newUserPackage()
    graph = SDSBSCompGraph.sNew(package)
    if graph is None:
        raise GraphAuthoringError("Designer could not create a compositing graph", "GRAPH_CREATE_FAILED")
    graph.setIdentifier(identifier)
    if open_in_editor:
        application().getUIMgr().openResourceInEditor(graph)
    return {"graph_id": identifier, "package_path": package_path(package)}


def find_node(node_id: str) -> Any:
    identifier = require_node_id(node_id)
    for node in items(active_graph().getNodes()):
        if node_identifier(node) == identifier:
            return node
    raise GraphAuthoringError(f"Node '{identifier}' was not found", "NODE_NOT_FOUND")


def connect_nodes(
    source_node: str,
    source_property: str,
    target_node: str,
    target_property: str,
    expected_graph_uid: str | None = None,
) -> dict[str, Any]:
    from .graph_connections import connect_nodes as connect

    return connect(source_node, source_property, target_node, target_property, expected_graph_uid)


def delete_node(node_id: str, expected_graph_uid: str | None = None) -> dict[str, str]:
    identifier = require_node_id(node_id)
    from .graph_inspection import checked_graph, find_in_graph

    graph = checked_graph(expected_graph_uid)
    node = find_in_graph(graph, identifier)
    delete = getattr(graph, "deleteNode", None)
    if not callable(delete):
        raise GraphAuthoringError("Designer node deletion API is unavailable", "DELETE_API_UNAVAILABLE")
    result = delete(node)
    if result is False:
        raise GraphAuthoringError("Designer rejected node deletion", "NODE_DELETE_FAILED")
    if any(node_identifier(item) == identifier for item in items(graph.getNodes())):
        raise GraphAuthoringError("Deleted node remains in the graph", "NODE_DELETE_READBACK_FAILED")
    return {"node_id": identifier}


def remove_created(created: list[Any]) -> None:
    """Remove only the nodes one call created, newest first.

    Shared rollback primitive for the effect, lighting, and series tools. Cleanup
    must never mask the exception that triggered it, and it must never swallow
    KeyboardInterrupt or SystemExit, so only ``Exception`` is absorbed here.
    """
    for node in reversed(created):
        try:
            delete_node(node if isinstance(node, str) else node["node_id"])
        except Exception:  # noqa: BLE001 - rollback is best effort by design.
            continue


def set_node_position(node_id: str, position: list[float]) -> dict[str, Any]:
    if len(position) != 2:
        raise GraphAuthoringError("position must contain exactly two numbers", "INVALID_POSITION")
    try:
        xy = [float(position[0]), float(position[1])]
    except (TypeError, ValueError) as exc:
        raise GraphAuthoringError("position must contain exactly two numbers", "INVALID_POSITION") from exc
    if any(not math.isfinite(component) or abs(component) > 1_000_000 for component in xy):
        raise GraphAuthoringError("position components are out of range", "INVALID_POSITION")

    from sd.api.sdbasetypes import float2

    node = find_node(node_id)
    node.setPosition(float2(*xy))
    return {"node_id": node_identifier(node), "position": xy}


def _numeric_sequence(raw: Any, size: int, label: str) -> list[float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != size:
        raise GraphAuthoringError(f"{label} requires exactly {size} numeric values", "INVALID_VALUE")
    if any(isinstance(item, bool) or not isinstance(item, (float, int)) for item in raw):
        raise GraphAuthoringError(f"{label} values must be numeric", "INVALID_VALUE")
    try:
        values = [float(item) for item in raw]
    except (TypeError, ValueError) as exc:
        raise GraphAuthoringError(f"{label} values must be numeric", "INVALID_VALUE") from exc
    if any(not math.isfinite(item) or abs(item) > 1_000_000 for item in values):
        raise GraphAuthoringError(f"{label} values are out of range", "INVALID_VALUE")
    return values


def typed_value(value_type: str, raw: Any) -> Any:
    kind = str(value_type).strip().lower()
    if kind == "bool":
        if not isinstance(raw, bool):
            raise GraphAuthoringError("bool values must be true or false", "INVALID_VALUE")
        from sd.api.sdvaluebool import SDValueBool

        return SDValueBool.sNew(raw)
    if kind == "int":
        if isinstance(raw, bool) or not isinstance(raw, int) or not -(2**31) <= raw < 2**31:
            raise GraphAuthoringError("int values must be integers", "INVALID_VALUE")
        from sd.api.sdvalueint import SDValueInt

        return SDValueInt.sNew(raw)
    if kind == "float":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise GraphAuthoringError("float values must be numeric", "INVALID_VALUE")
        from sd.api.sdvaluefloat import SDValueFloat

        if not math.isfinite(raw) or abs(raw) > 1_000_000:
            raise GraphAuthoringError("float value is out of range", "INVALID_VALUE")
        return SDValueFloat.sNew(float(raw))
    if kind == "string":
        if not isinstance(raw, str) or len(raw) > 4096:
            raise GraphAuthoringError("string values must contain at most 4096 characters", "INVALID_VALUE")
        from sd.api.sdvaluestring import SDValueString

        return SDValueString.sNew(raw)
    vector_specs = {
        "colorrgba": (4, "ColorRGBA", "SDValueColorRGBA", "sd.api.sdvaluecolorrgba"),
        "color": (4, "ColorRGBA", "SDValueColorRGBA", "sd.api.sdvaluecolorrgba"),
        "int2": (2, "int2", "SDValueInt2", "sd.api.sdvalueint2"),
        "float2": (2, "float2", "SDValueFloat2", "sd.api.sdvaluefloat2"),
        "float3": (3, "float3", "SDValueFloat3", "sd.api.sdvaluefloat3"),
        "float4": (4, "float4", "SDValueFloat4", "sd.api.sdvaluefloat4"),
    }
    if kind not in vector_specs:
        raise GraphAuthoringError("Unsupported typed parameter value", "UNSUPPORTED_VALUE_TYPE")
    size, base_name, wrapper_name, module_name = vector_specs[kind]
    values = _numeric_sequence(raw, size, kind)
    if kind == "int2":
        if any(isinstance(item, bool) or not isinstance(item, int) for item in raw):
            raise GraphAuthoringError("int2 values must be integers", "INVALID_VALUE")
        values = [int(item) for item in raw]
    import importlib

    base_type = getattr(importlib.import_module("sd.api.sdbasetypes"), base_name)
    wrapper = getattr(importlib.import_module(module_name), wrapper_name)
    return wrapper.sNew(base_type(*values))


def json_value(raw: Any) -> Any:
    resolved = value(raw, "get")
    if resolved is not None and resolved is not raw:
        return json_value(resolved)
    if raw is None or isinstance(raw, (bool, int, float, str)):
        return raw
    if isinstance(raw, (list, tuple)):
        return [json_value(item) for item in raw]
    components = []
    for name in ("x", "y", "z", "w"):
        component = value(raw, name)
        if component is None:
            break
        components.append(component)
    if components:
        return [json_value(component) for component in components]
    if all(hasattr(raw, name) for name in ("r", "g", "b", "a")):
        return [json_value(getattr(raw, name)) for name in ("r", "g", "b", "a")]
    identifier = value(raw, "getId", "getIdentifier")
    if identifier is not None:
        return str(identifier)
    return {"type": type(raw).__name__}


def set_parameter(
    node_id: str, parameter: str, value_type: str, raw: Any, expected_graph_uid: str | None = None
) -> dict[str, Any]:
    from .graph_inspection import checked_graph, find_in_graph, property_types, require_input

    graph = checked_graph(expected_graph_uid)
    node = find_in_graph(graph, node_id)
    prop = require_input(node, parameter)
    if prop.isReadOnly():
        raise GraphAuthoringError("Input property is read-only", "PROPERTY_READ_ONLY")
    if items(node.getPropertyConnections(prop)):
        raise GraphAuthoringError("Disconnect the input before setting its value", "INPUT_CONNECTED")
    if node.getPropertyGraph(prop) is not None:
        raise GraphAuthoringError("Input has a function graph", "PROPERTY_GRAPH_CONNECTED")
    wrapped = typed_value(value_type, raw)
    expected_type = wrapped.getType().getId()
    if expected_type not in {kind["id"] for kind in property_types(prop)}:
        raise GraphAuthoringError("Value type is not supported by the input", "PARAMETER_TYPE_MISMATCH")
    node.setInputPropertyValueFromId(prop.getId(), wrapped)
    observed = json_value(node.getPropertyValue(prop))
    expected = json_value(wrapped)
    if observed != expected:
        raise GraphAuthoringError("Parameter readback did not match the requested value", "PARAMETER_READBACK_FAILED")
    return {
        "node_id": node_identifier(node),
        "parameter": prop.getId(),
        "value_type": str(value_type).lower(),
        "value": observed,
    }


def get_parameter(node_id: str, parameter: str) -> dict[str, Any]:
    identifier = require_property(parameter, "parameter")

    from sd.api.sdproperty import SDPropertyCategory

    node = find_node(node_id)
    prop = node.getPropertyFromId(identifier, SDPropertyCategory.Input)
    if prop is None:
        raise GraphAuthoringError(f"Input parameter '{identifier}' was not found", "PARAMETER_NOT_FOUND")
    inheritance = None
    getter = getattr(node, "getPropertyInheritanceMethod", None)
    if callable(getter):
        method = getter(prop)
        inheritance = str(getattr(method, "name", method))
    return {
        "node_id": node_identifier(node),
        "parameter": identifier,
        "value": json_value(node.getPropertyValue(prop)),
        "inheritance": inheritance,
    }


def set_property_inheritance(
    node_id: str, parameter: str, method: str, expected_graph_uid: str | None = None
) -> dict[str, Any]:
    """Set Absolute / RelativeToParent / RelativeToInput on one input property."""
    from sd.api.sdproperty import SDPropertyInheritanceMethod

    from .graph_inspection import checked_graph, find_in_graph, require_input

    allowed = ("Absolute", "RelativeToParent", "RelativeToInput")
    resolved = str(method).strip()
    if resolved not in allowed:
        raise GraphAuthoringError(
            "method must be Absolute, RelativeToParent, or RelativeToInput", "INVALID_INHERITANCE_METHOD"
        )
    graph = checked_graph(expected_graph_uid)
    node = find_in_graph(graph, node_id)
    prop = require_input(node, parameter)
    setter = getattr(node, "setPropertyInheritanceMethod", None)
    if not callable(setter):
        raise GraphAuthoringError("Property inheritance API is unavailable", "INHERITANCE_API_UNAVAILABLE")
    setter(prop, getattr(SDPropertyInheritanceMethod, resolved))
    observed = node.getPropertyInheritanceMethod(prop)
    observed_name = str(getattr(observed, "name", observed))
    if observed_name != resolved:
        raise GraphAuthoringError(
            "Inheritance method readback did not match the requested value", "INHERITANCE_READBACK_FAILED"
        )
    return {
        "node_id": node_identifier(node),
        "parameter": prop.getId(),
        "inheritance": observed_name,
        "value": json_value(node.getPropertyValue(prop)),
    }


def expose_parameter(
    node_id: str, parameter: str, exposed_id: str, expected_graph_uid: str | None = None
) -> dict[str, Any]:
    from .graph_parameters import expose_parameter as expose

    return expose(node_id, parameter, exposed_id, expected_graph_uid)


def add_output(
    output_id: str,
    position: list[float] | None = None,
) -> dict[str, Any]:
    from .graph_inspection import find_in_graph

    identifier = require_identifier(output_id)
    graph = active_graph()
    if identifier in [json_value(item) for item in items(graph.getOutputIdentifiers())]:
        raise GraphAuthoringError("Output identifier already exists", "DUPLICATE_OUTPUT_ID")
    output = create_node("sbs::compositing::output", identifier, position)
    from sd.api.sdvaluestring import SDValueString

    node = find_in_graph(graph, output["node_id"])
    try:
        node.setAnnotationPropertyValueFromId("identifier", SDValueString.sNew(identifier))
        if json_value(node.getAnnotationPropertyValueFromId("identifier")) != identifier:
            raise GraphAuthoringError("Output identifier readback failed", "OUTPUT_IDENTIFIER_READBACK_FAILED")
    except BaseException:
        graph.deleteNode(node)
        raise
    return output


def set_output_usage(
    node_id: str,
    usage: str,
    channels: str,
    color_space: str,
) -> dict[str, str]:
    resolved_usage = require_identifier(usage, "usage")
    channel_options = {"L", "RGB", "RGBA"}
    resolved_channels = str(channels).strip().upper()
    if resolved_channels not in channel_options:
        raise GraphAuthoringError("channels must be L, RGB, or RGBA", "INVALID_CHANNELS")
    resolved_space = str(color_space).strip()
    if not _COLOR_SPACE.fullmatch(resolved_space):
        raise GraphAuthoringError("color_space must be a bounded non-empty name", "INVALID_COLOR_SPACE")

    from sd.api.sdtypeusage import SDTypeUsage
    from sd.api.sdvaluearray import SDValueArray
    from sd.api.sdvalueusage import SDUsage, SDValueUsage

    node = find_node(node_id)
    if node.getDefinition().getId() != "sbs::compositing::output":
        raise GraphAuthoringError("Usage metadata requires an output node", "NOT_OUTPUT_NODE")
    usages = SDValueArray.sNew(SDTypeUsage.sNew(), 0)
    usages.pushBack(SDValueUsage.sNew(SDUsage.sNew(resolved_usage, resolved_channels, resolved_space)))
    node.setAnnotationPropertyValueFromId("usages", usages)
    readback = items(node.getAnnotationPropertyValueFromId("usages"))
    actual = []
    for wrapped in readback:
        item = value(wrapped, "get") or wrapped
        actual.append((item.getName(), item.getComponents(), item.getColorSpace()))
    if actual != [(resolved_usage, resolved_channels, resolved_space)]:
        raise GraphAuthoringError("Output usage readback failed", "OUTPUT_USAGE_READBACK_FAILED")
    return {
        "node_id": node_identifier(node),
        "usage": resolved_usage,
        "channels": resolved_channels,
        "color_space": resolved_space,
    }


def _sbs_path(raw: str, *, must_exist: bool) -> Path:
    path = Path(raw).expanduser().resolve()
    if path.suffix.casefold() != ".sbs":
        raise GraphAuthoringError("Package path must end with .sbs", "INVALID_PACKAGE_PATH")
    if must_exist and not path.is_file():
        raise GraphAuthoringError("Package path does not exist", "PACKAGE_NOT_FOUND")
    return path


def new_package() -> dict[str, Any]:
    package = package_manager().newUserPackage()
    if package is None:
        raise GraphAuthoringError("Designer could not create a package", "PACKAGE_CREATE_FAILED")
    return {"package_path": package_path(package), "saved": False}


def open_package(path: str) -> dict[str, Any]:
    from .graph_resources import open_package as open_existing

    return open_existing(path)


def save_package() -> dict[str, Any]:
    package = active_package()
    if not package_path(package):
        raise GraphAuthoringError("Unsaved package requires save_package_as", "PACKAGE_PATH_REQUIRED")
    _sbs_path(package_path(package), must_exist=False)
    result = package_manager().savePackage(package)
    artifact = Path(package_path(package))
    if result is False or package.isModified() or not artifact.is_file() or not artifact.stat().st_size:
        raise GraphAuthoringError("Designer could not save the package", "PACKAGE_SAVE_FAILED")
    return {"package_path": package_path(package), "saved": True}


def save_package_as(path: str, expected_graph_uid: str | None = None) -> dict[str, Any]:
    from .graph_inspection import checked_graph

    if expected_graph_uid is not None:
        checked_graph(expected_graph_uid)
    resolved = _sbs_path(path, must_exist=False)
    package = active_package()
    if resolved.exists() and (not package_path(package) or resolved != Path(package_path(package)).resolve()):
        raise GraphAuthoringError("Refusing to overwrite another package", "PACKAGE_ALREADY_EXISTS")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    result = package_manager().savePackageAs(package, str(resolved))
    if (
        result is False
        or package.isModified()
        or not resolved.is_file()
        or not resolved.stat().st_size
        or Path(package_path(package)).resolve() != resolved
    ):
        raise GraphAuthoringError("Designer could not save the package", "PACKAGE_SAVE_FAILED")
    return {"package_path": str(resolved), "saved": True}


def close_package() -> dict[str, Any]:
    package = active_package()
    resolved_path = package_path(package)
    if package.isModified():
        raise GraphAuthoringError("Save the modified package before closing", "PACKAGE_MODIFIED")
    result = package_manager().unloadUserPackage(package)
    if result is False:
        raise GraphAuthoringError("Designer could not close the package", "PACKAGE_CLOSE_FAILED")
    return {"package_path": resolved_path, "closed": True}


def import_resource(path: str, kind: str, embed: bool = False) -> dict[str, Any]:
    resource_path = Path(path).expanduser().resolve()
    resolved_kind = str(kind).strip().lower()
    expected_suffixes = {
        "bitmap": {".bmp", ".exr", ".hdr", ".jpg", ".jpeg", ".png", ".psd", ".tga", ".tif", ".tiff"},
        "svg": {".svg"},
    }
    if (
        resolved_kind not in expected_suffixes
        or resource_path.suffix.casefold() not in expected_suffixes[resolved_kind]
    ):
        raise GraphAuthoringError("Resource extension does not match the declared kind", "INVALID_RESOURCE_TYPE")
    if not resource_path.is_file():
        raise GraphAuthoringError("Resource path does not exist", "RESOURCE_NOT_FOUND")

    from sd.api.sdresource import EmbedMethod

    method = EmbedMethod.Embedded if embed else EmbedMethod.Linked
    if resolved_kind == "bitmap":
        from sd.api.sdresourcebitmap import SDResourceBitmap

        resource = SDResourceBitmap.sNewFromFile(active_package(), str(resource_path), method)
    else:
        from sd.api.sdresourcesvg import SDResourceSVG

        resource = SDResourceSVG.sNewFromFile(active_package(), str(resource_path), method)
    if resource is None:
        raise GraphAuthoringError("Designer could not import the resource", "RESOURCE_IMPORT_FAILED")
    return {
        "resource_path": str(resource_path),
        "kind": resolved_kind,
        "embedded": bool(embed),
        "resource_url": str(value(resource, "getUrl", "getIdentifier") or ""),
    }


def export_maps(
    output_dir: str,
    image_format: str = "png",
    bit_depth: str = "8",
    color_space: str = "Raw",
    expected_graph_uid: str | None = None,
    output_color_spaces: dict[str, str] | None = None,
) -> dict[str, Any]:
    from .graph_evaluation import export_maps as export

    return export(output_dir, image_format, bit_depth, color_space, expected_graph_uid, output_color_spaces)


def export_sbsar(path: str) -> dict[str, str]:
    destination = Path(path).expanduser().resolve()
    if destination.suffix.casefold() != ".sbsar":
        raise GraphAuthoringError("SBSAR path must end with .sbsar", "INVALID_SBSAR_PATH")
    if destination.exists():
        raise GraphAuthoringError("Use a fresh SBSAR destination", "SBSAR_ALREADY_EXISTS")

    from sd.api.sbs.sdsbsarexporter import SDSBSARExporter

    package = active_package()
    destination.parent.mkdir(parents=True, exist_ok=True)
    exporter = SDSBSARExporter.sNew()
    result = exporter.exportPackageToSBSAR(package, str(destination))
    if result is False or not destination.is_file() or not destination.stat().st_size:
        raise GraphAuthoringError("Designer did not produce the SBSAR artifact", "SBSAR_EXPORT_FAILED")
    return {"sbsar_path": str(destination)}
