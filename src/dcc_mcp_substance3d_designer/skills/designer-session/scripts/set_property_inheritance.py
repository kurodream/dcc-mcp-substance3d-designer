"""Set Absolute / Relative inheritance on one Designer node input property."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry

from dcc_mcp_substance3d_designer.graph_authoring import set_property_inheritance
from dcc_mcp_substance3d_designer.skill_support import typed_result


@skill_entry
def main(node_id: str, parameter: str, method: str, expected_graph_uid: str | None = None, **_kwargs):
    return typed_result(
        "Set Designer property inheritance",
        set_property_inheritance,
        node_id,
        parameter,
        method,
        expected_graph_uid,
    )
