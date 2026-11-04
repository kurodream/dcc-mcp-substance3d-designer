---
name: designer-session
description: >-
  Host skill - inspect, author, save, and export a Substance 3D Designer graph.
  Use when an Agent needs bounded node, connection, parameter, gradient, output, package,
  resource, map, or SBSAR operations instead of arbitrary Python execution.
license: MIT
compatibility: "Substance 3D Designer Python 3.9+; dcc-mcp-core 0.20.15+"
allowed-tools: Python
metadata:
  dcc-mcp:
    dcc: substance3d_designer
    version: "0.8.1" # x-release-please-version
    layer: bootstrap
    stage: bootstrap
    search-hint: "substance designer typed graph nodes connections parameters RGBA gradient keys outputs package sbs sbsar render maps inspect"
    tags: "substance, designer, package, graph, node, connection, parameter, gradient, output, sbsar, render, inspect"
    tools: tools.yaml
---

# Designer Session

Read the active Designer session and graph before mutating it. The public tools
create built-in `sbs::` nodes or explicit open-package resource instances, address native node and
property identifiers, and accept an allowlisted set of scalar and vector value
types. They do not expose an eval, script, shell, or generic action surface.

Use `export_graph_state` after each authoring sequence to verify node count,
type URLs, positions, parameters, topology, and output usages. A typical graph
sequence is `create_graph` → `create_node` → `connect_nodes` →
`set_parameter` → `add_output`/`set_output_usage` →
`save_package_as` → `export_graph_state`.

Package tools use Designer's package manager. Bitmap and SVG imports validate
the declared resource kind and use linked resources unless `embed=true` is
explicitly requested. `export_sbsar` uses Designer's official SBSAR exporter
and requires a fresh, nonempty artifact. `export_maps` compiles the current package
to SBSAR in a new run directory, then invokes only the
official `sbsrender` executable discovered by the host environment and always
requires explicit format, bit depth, and color-space settings; it fails closed
when that executable is unavailable.

`expose_parameter` binds a writable constant input through the public graph-input
and function-graph APIs. The SDK must advertise the required variable reader and
a unique string input; unsupported types return `EXPOSE_API_UNAVAILABLE` or
`EXPOSE_TYPE_UNSUPPORTED`. Use `set_graph_parameter` to edit the exposed value.
Host API calls remain on the
Designer main thread through the adapter's Core execution bridge.

Use `get_gradient_keys` and `set_gradient_keys` for the native `gradientrgba`
input of `sbs::compositing::gradient` nodes. Both require the observed graph UID.
The setter replaces 2..64 strictly ordered keys, with position, RGBA channels,
and midpoint in 0..1 (midpoint defaults to 0.5). It rejects other node/input types,
connected or function-driven inputs, and nonfinite values before mutation.
Results contain the actual native float values, which may have float32 rounding.
Reads preserve native pin order, including stacked pins with equal positions.
Writes require strictly increasing positions after native float32 conversion.
Readback failure restores the prior gradient. These tools do not change color
mode, interpolation mode, other node parameters, or graph connections.

Start with `list_nodes` and `list_node_types`, then `describe_node` or
`describe_node_type` to inspect supported parameter and port types. Reuse returned
native IDs; `create_node.node_id` sets a display label, not the SDK handle.
Pass the observed `graph_uid` as `expected_graph_uid` on supported mutations.
Use `validate_connection` before connecting and `disconnect_nodes` for exact-edge
removal. Open package resources can be inspected, selected or instanced through
`list_resources`, `select_graph` and `instance_resource`; `create_subgraph` creates
a same-package composition graph. Recursive instances are rejected.

`inspect_graph_session` reports package ownership/dirty state, graph inputs, node
connections/function bindings and the last application interop error. This is not
a per-node compiler log.

`evaluate_graph` checks node and static resolution budgets and output texture metadata.
Compute remains synchronous with no hard cancellation deadline. Exported file
headers verify dimensions and actual bit depth; hashes prove fresh bytes, not
visual material accuracy. `export_native_maps` preserves native texture precision
without requiring SAT. Intermediate node ports are pinned to temporary graph
outputs for compute, then removed; prefer named graph outputs when possible.
Missing texture errors identify the requested name, node and port. Native export
checks all selected textures and stages verified files before publishing the
destination; a failed export leaves that destination available for retry. Each
file receipt includes its native pixel format and source node/port. Use
`get_parameter` / `set_property_inheritance` on `$pixelsize` when Directional Warp
intensity appears set but pixels do not move (Absolute `[1,1]` is not “full
resolution pixels”). SAT export owns the renderer process tree for timeout cleanup.
Save before closing a modified package; opening an already-open package preserves
its in-memory edits. Use a new path to export SBSAR or save another package.

### Reopen source and inspect compiled output

`open_package` accepts `.sbs` and `.sbsar`; save paths remain `.sbs` only. To
prove a source reopen, save and close its unmodified package, open the saved
file, then select its explicit resource and call `evaluate_graph` and
`export_native_maps` with the newly observed graph UID. An `already_open=true`
receipt preserves edits and does not prove a disk reload.

To read compiled output without SAT, open the exported `.sbsar`, call
`list_resources`, and instantiate its returned material resource with
`instance_resource` in a separate source composition graph. Inspect the
instance's returned native output ports, connect them to named graph outputs,
set an absolute bounded output size, and use `export_native_maps`. Keep source
and compiled output receipts separate; successful package loading alone does
not prove that compiled textures match the source. Compiled resources are not
editable source graph topology.

The higher-level procedural and imported-PBR tools remain available for common
material recipes. Use the granular tools when the graph topology or package
lifecycle must be controlled and verified step by step.

## Native graph views

Use `list_graph_views` to bind an open view to its native graph UID and read selected nodes. Pass its opaque string ID and the expected graph UID to `get_graph_view_node_bounds`. A stale view or graph mismatch fails before querying node bounds. The SDK bounds are x/y/width/height in native graph-view coordinates, not desktop pixels; do not use them directly for screen clicks.

The inventory reports whether the official view-created and explorer-selection callback APIs exist. It does not register hooks, inject code, operate the UI, fit the view, or export a screenshot. Continue using the project DCC-CUA route for UI capture until graph-widget binding and full graph coverage are verified.
