"""A failed native export must be diagnosable and retryable at the same path."""

import hashlib
import struct
import sys
import zlib
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from dcc_mcp_substance3d_designer import graph_authoring as api
from dcc_mcp_substance3d_designer import graph_evaluation as evaluation


def png_header(width=8):
    header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">IIBBBBB", width, 8, 16, 0, 0, 0, 0)
    return header + struct.pack(">I", zlib.crc32(header[12:]))


def _texture():
    return SimpleNamespace(
        getSize=lambda: SimpleNamespace(x=8, y=8),
        getPixelFormat=lambda: SimpleNamespace(name="LUM16"),
        save=lambda path: Path(path).write_bytes(png_header()),
    )


@pytest.fixture
def native_host(monkeypatch):
    module = ModuleType("sd.api.sdproperty")
    module.SDPropertyCategory = SimpleNamespace(Output="Output", Input="Input")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    textures, nodes = [], []
    for index in range(2):
        textures.append(_texture())
        prop = SimpleNamespace(getId=lambda: "output")
        nodes.append(
            SimpleNamespace(
                getIdentifier=lambda index=index: str(index),
                getPropertyFromId=lambda *args, prop=prop: prop,
                getPropertyValue=lambda prop, index=index: SimpleNamespace(get=lambda: textures[index]),
                getProperties=lambda category, prop=prop: [prop],
            )
        )
    graph = SimpleNamespace(
        getUID=lambda: "graph-A",
        getNodes=lambda: nodes,
        getOutputNodes=lambda: nodes,
        getOutputIdentifiers=lambda: ["0", "1"],
    )
    monkeypatch.setattr(api, "active_graph", lambda: graph)
    monkeypatch.setattr(api, "remove_created", lambda created: None)
    monkeypatch.setattr(evaluation, "evaluate_graph", lambda *args: {})
    return textures


OUTPUTS = [
    {"name": "height", "node_id": "0", "property": "output"},
    {"name": "roughness", "node_id": "1", "property": "output"},
]


@pytest.mark.parametrize(
    "failure,code",
    [("missing", "OUTPUT_NOT_COMPUTED"), ("save", "MAP_OUTPUT_MISSING"), ("dimensions", "MAP_HEADER_INVALID")],
)
def test_failure_does_not_publish_partial_files_and_same_path_can_retry(native_host, tmp_path, failure, code):
    destination = tmp_path / "maps"
    second = native_host[1]
    if failure == "missing":
        native_host[1] = None
    elif failure == "save":
        second.save = lambda path: False
    else:
        second.save = lambda path: Path(path).write_bytes(png_header(width=4))
    with pytest.raises(api.GraphAuthoringError) as error:
        evaluation.export_native_maps(str(destination), OUTPUTS, "graph-A")
    assert error.value.code == code
    if failure == "missing":
        assert "roughness (1.output)" in str(error.value)
        assert "did not compute" in str(error.value)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
    native_host[1] = second
    second.save = lambda path: Path(path).write_bytes(png_header())
    result = evaluation.export_native_maps(str(destination), OUTPUTS, "graph-A")
    assert len(result["files"]) == 2
    assert result["pinned_intermediates"] == 0
    for item in result["files"]:
        assert Path(item["path"]).parent == destination
        assert item["pixel_format"] == "LUM16"
        assert item["property"] == "output"
        assert item["sha256"] == hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest()


def test_destination_created_during_save_is_preserved(native_host, tmp_path):
    destination = tmp_path / "maps"

    def save(path):
        destination.mkdir()
        (destination / "owned-by-user").write_text("keep")
        Path(path).write_bytes(png_header())

    native_host[1].save = save
    with pytest.raises(api.GraphAuthoringError) as error:
        evaluation.export_native_maps(str(destination), OUTPUTS, "graph-A")
    assert error.value.code == "OUTPUT_EXISTS"
    assert list(destination.iterdir()) == [destination / "owned-by-user"]
    assert (destination / "owned-by-user").read_text() == "keep"
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("outputs", [[None], [{"name": "height"}], OUTPUTS + [OUTPUTS[0]]])
def test_malformed_selection_leaves_destination_absent(native_host, tmp_path, outputs):
    with pytest.raises(api.GraphAuthoringError) as error:
        evaluation.export_native_maps(str(tmp_path / "maps"), outputs, "graph-A")
    assert error.value.code == "INVALID_GRAPH_OUTPUTS"
    assert list(tmp_path.iterdir()) == []


def test_intermediate_node_is_pinned_then_cleaned_up(monkeypatch, tmp_path):
    module = ModuleType("sd.api.sdproperty")
    module.SDPropertyCategory = SimpleNamespace(Output="Output", Input="Input")
    monkeypatch.setitem(sys.modules, module.__name__, module)

    mid_prop = SimpleNamespace(getId=lambda: "unique_filter_output")
    out_prop = SimpleNamespace(getId=lambda: "shape_preview")
    mid_texture = None
    out_texture = _texture()
    pin_texture = _texture()
    cleaned = []

    mid = SimpleNamespace(
        getIdentifier=lambda: "mid",
        getPropertyFromId=lambda *args: mid_prop,
        getPropertyValue=lambda prop: SimpleNamespace(get=lambda: mid_texture),
        getProperties=lambda category: [mid_prop],
    )
    out = SimpleNamespace(
        getIdentifier=lambda: "out",
        getPropertyFromId=lambda *args: out_prop,
        getPropertyValue=lambda prop: SimpleNamespace(get=lambda: out_texture),
        getProperties=lambda category: [out_prop],
    )
    nodes = [mid, out]
    pin_prop = SimpleNamespace(getId=lambda: "shape_preview")
    pin = SimpleNamespace(
        getIdentifier=lambda: "pin",
        getPropertyFromId=lambda *args: pin_prop,
        getPropertyValue=lambda prop: SimpleNamespace(get=lambda: pin_texture),
        getProperties=lambda category: [pin_prop],
    )

    def add_output(output_id, position=None):
        assert output_id == "mcp_pin_warped"
        pin.getIdentifier = lambda: output_id
        nodes.append(pin)
        return {"node_id": output_id}

    def connect_nodes(*args, **kwargs):
        return {"already_connected": False}

    graph = SimpleNamespace(
        getUID=lambda: "graph-A",
        getNodes=lambda: nodes,
        getOutputNodes=lambda: [node for node in nodes if node.getIdentifier() in {"out", "mcp_pin_warped"}],
        getOutputIdentifiers=lambda: ["shape_preview"],
    )
    monkeypatch.setattr(api, "active_graph", lambda: graph)
    monkeypatch.setattr(api, "add_output", add_output)
    monkeypatch.setattr(api, "connect_nodes", connect_nodes)
    monkeypatch.setattr(api, "remove_created", lambda created: cleaned.extend(created))
    monkeypatch.setattr(evaluation, "evaluate_graph", lambda *args: {})

    destination = tmp_path / "maps"
    result = evaluation.export_native_maps(
        str(destination),
        [
            {"name": "warped", "node_id": "mid", "property": "unique_filter_output"},
            {"name": "final", "node_id": "out", "property": "shape_preview"},
        ],
        "graph-A",
    )
    assert result["pinned_intermediates"] == 1
    assert [item["name"] for item in result["files"]] == ["warped", "final"]
    assert result["files"][0]["node_id"] == "mid"
    assert result["files"][0]["property"] == "unique_filter_output"
    assert cleaned == ["mcp_pin_warped"]


def test_pinned_outputs_use_unique_identifiers_and_save_before_next_read(monkeypatch, tmp_path):
    """Designer anonymous outputs and shared SDTexture handles must not alias exports."""
    module = ModuleType("sd.api.sdproperty")
    module.SDPropertyCategory = SimpleNamespace(Output="Output", Input="Input")
    monkeypatch.setitem(sys.modules, module.__name__, module)

    shared = {"bytes": png_header(width=8)}
    mid_props = [
        SimpleNamespace(getId=lambda: "unique_filter_output"),
        SimpleNamespace(getId=lambda: "unique_filter_output"),
    ]
    created_ids = []
    cleaned = []

    def texture_for(width: int):
        def save(path):
            # Mimic SDK handles that all write the *current* shared buffer.
            Path(path).write_bytes(shared["bytes"])
            return True

        return SimpleNamespace(
            getSize=lambda width=width: SimpleNamespace(x=width, y=8),
            getPixelFormat=lambda: SimpleNamespace(name="LUM16"),
            save=save,
        )

    nodes = []
    for index, prop in enumerate(mid_props):
        nodes.append(
            SimpleNamespace(
                getIdentifier=lambda index=index: f"mid{index}",
                getPropertyFromId=lambda *args, prop=prop: prop,
                getPropertyValue=lambda prop: None,
                getProperties=lambda category, prop=prop: [prop],
            )
        )

    pins = {}

    def add_output(output_id, position=None):
        created_ids.append(output_id)
        pin_prop = SimpleNamespace(getId=lambda: output_id)
        index = len(created_ids) - 1
        width = 8 + index

        def get_value(prop, index=index, width=width):
            shared["bytes"] = png_header(width=width)
            return SimpleNamespace(get=lambda width=width: texture_for(width))

        pin = SimpleNamespace(
            getIdentifier=lambda output_id=output_id: output_id,
            getPropertyFromId=lambda *args, pin_prop=pin_prop: pin_prop,
            getPropertyValue=get_value,
            getProperties=lambda category, pin_prop=pin_prop: [pin_prop],
        )
        pins[output_id] = pin
        nodes.append(pin)
        return {"node_id": output_id}

    graph = SimpleNamespace(
        getUID=lambda: "graph-A",
        getNodes=lambda: nodes,
        getOutputNodes=lambda: [node for node in nodes if node.getIdentifier() in pins],
        getOutputIdentifiers=lambda: [],
    )
    monkeypatch.setattr(api, "active_graph", lambda: graph)
    monkeypatch.setattr(api, "add_output", add_output)
    monkeypatch.setattr(api, "connect_nodes", lambda *args, **kwargs: {})
    monkeypatch.setattr(api, "remove_created", lambda created: cleaned.extend(created))
    monkeypatch.setattr(evaluation, "evaluate_graph", lambda *args: {})

    destination = tmp_path / "maps"
    result = evaluation.export_native_maps(
        str(destination),
        [
            {"name": "first", "node_id": "mid0", "property": "unique_filter_output"},
            {"name": "second", "node_id": "mid1", "property": "unique_filter_output"},
        ],
        "graph-A",
    )
    assert created_ids == ["mcp_pin_first", "mcp_pin_second"]
    assert result["pinned_intermediates"] == 2
    hashes = [item["sha256"] for item in result["files"]]
    assert hashes[0] != hashes[1]
    assert cleaned == ["mcp_pin_first", "mcp_pin_second"]


def test_pin_failure_still_removes_temporary_outputs(monkeypatch, tmp_path):
    module = ModuleType("sd.api.sdproperty")
    module.SDPropertyCategory = SimpleNamespace(Output="Output", Input="Input")
    monkeypatch.setitem(sys.modules, module.__name__, module)

    mid_prop = SimpleNamespace(getId=lambda: "unique_filter_output")
    mid = SimpleNamespace(
        getIdentifier=lambda: "mid",
        getPropertyFromId=lambda *args: mid_prop,
        getPropertyValue=lambda prop: SimpleNamespace(get=lambda: None),
        getProperties=lambda category: [mid_prop],
    )
    nodes = [mid]
    pin_prop = SimpleNamespace(getId=lambda: "shape_preview")
    pin = SimpleNamespace(
        getIdentifier=lambda: "pin",
        getPropertyFromId=lambda *args: pin_prop,
        getPropertyValue=lambda prop: SimpleNamespace(get=lambda: None),
        getProperties=lambda category: [pin_prop],
    )
    cleaned = []

    graph = SimpleNamespace(
        getUID=lambda: "graph-A",
        getNodes=lambda: nodes,
        getOutputNodes=lambda: [node for node in nodes if node.getIdentifier() == "mcp_pin_warped"],
        getOutputIdentifiers=lambda: [],
    )
    monkeypatch.setattr(api, "active_graph", lambda: graph)

    def add_output(output_id, position=None):
        pin.getIdentifier = lambda: output_id
        nodes.append(pin)
        return {"node_id": output_id}

    monkeypatch.setattr(api, "add_output", add_output)
    monkeypatch.setattr(api, "connect_nodes", lambda *args, **kwargs: {})
    monkeypatch.setattr(api, "remove_created", lambda created: cleaned.extend(created))
    monkeypatch.setattr(evaluation, "evaluate_graph", lambda *args: {})

    with pytest.raises(api.GraphAuthoringError) as error:
        evaluation.export_native_maps(
            str(tmp_path / "maps"),
            [{"name": "warped", "node_id": "mid", "property": "unique_filter_output"}],
            "graph-A",
        )
    assert error.value.code == "OUTPUT_NOT_COMPUTED"
    assert cleaned == ["mcp_pin_warped"]
    assert not (tmp_path / "maps").exists()
