"""
Interactive dirty topology generator (bpy debug / preview).

Select a MESH object → creates <name>_clean (hidden) + <name>_dirty (visible).

Tune DirtyParams below (or CLI flags when run with --).

Usage: Blender Development → Run Script
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

import bmesh
import bpy

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import dirty_topology_core

importlib.reload(dirty_topology_core)
from dirty_topology_core import DirtyParams, apply_dirty_topology_to_bmesh

# =========================
# 默认可调参数（改这里）
# =========================
PARAMS = DirtyParams(
    # 切面位移：magnitude = bbox * displace_scale * (displace_strength/100)
    displace_scale=0.002,
    displace_strength=100.0,  # 100=满强度，50=一半
    # 随机融并
    enable_random_merge=True,
    merge_edge_ratio=0.05,
    # 局部病变
    enable_local_disease=True,
    disease_centers=2,
    disease_ring=2,
    disease_subdivide=True,
    disease_collapse_ratio=0.08,
    # 可选
    enable_edge_rotate=False,
    rotate_edge_ratio=0.02,
)


def log(msg: str) -> None:
    print(f"[DirtyTopology] {msg}")


def undo_push(message: str) -> None:
    try:
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.ed.undo_push(message=message)
    except Exception:
        pass


def get_base_name(name: str) -> str:
    if name.endswith("_clean"):
        return name[: -len("_clean")]
    if name.endswith("_dirty"):
        return name[: -len("_dirty")]
    return name


def get_selected_mesh() -> bpy.types.Object | None:
    obj = bpy.context.active_object
    if obj is None:
        log("Error: no object selected.")
        return None
    if obj.type != "MESH":
        log(f"Error: active object type is {obj.type}; expected MESH.")
        return None
    if obj not in bpy.context.selected_objects:
        log("Error: active object is not in the current selection.")
        return None
    if obj.name.endswith("_clean"):
        log("Error: select the original mesh or _dirty object, not hidden _clean.")
        return None
    return obj


def prepare_clean_dirty_pair(
    source_obj: bpy.types.Object,
) -> tuple[bpy.types.Object, bpy.types.Object, int]:
    base_name = get_base_name(source_obj.name)
    original_face_count = len(source_obj.data.polygons)

    dirty_obj = source_obj.copy()
    dirty_obj.data = source_obj.data.copy()
    bpy.context.collection.objects.link(dirty_obj)

    source_obj.name = f"{base_name}_clean"
    source_obj.hide_viewport = True
    source_obj.hide_render = True

    dirty_obj.name = f"{base_name}_dirty"
    dirty_obj.hide_viewport = False
    dirty_obj.hide_render = False

    return source_obj, dirty_obj, original_face_count


def select_only(obj: bpy.types.Object) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def parse_cli_params(base: DirtyParams) -> DirtyParams:
    argv = []
    if "--" in sys.argv:
        argv = sys.argv[sys.argv.index("--") + 1 :]
    if not argv:
        return base

    parser = argparse.ArgumentParser(description="Dirty topology params")
    parser.add_argument("--displace_scale", type=float, default=base.displace_scale)
    parser.add_argument(
        "--displace_strength", type=float, default=base.displace_strength
    )
    parser.add_argument(
        "--merge_edge_ratio", type=float, default=base.merge_edge_ratio
    )
    parser.add_argument("--disease_centers", type=int, default=base.disease_centers)
    parser.add_argument(
        "--disease_collapse_ratio", type=float, default=base.disease_collapse_ratio
    )
    parser.add_argument("--no_merge", action="store_true")
    parser.add_argument("--no_disease", action="store_true")
    args = parser.parse_args(argv)

    return DirtyParams(
        displace_scale=args.displace_scale,
        displace_strength=args.displace_strength,
        enable_random_merge=not args.no_merge,
        merge_edge_ratio=args.merge_edge_ratio,
        enable_local_disease=not args.no_disease,
        disease_centers=args.disease_centers,
        disease_ring=base.disease_ring,
        disease_subdivide=base.disease_subdivide,
        disease_collapse_ratio=args.disease_collapse_ratio,
        enable_edge_rotate=base.enable_edge_rotate,
        rotate_edge_ratio=base.rotate_edge_ratio,
        merge_distance=base.merge_distance,
    )


def main() -> None:
    source_obj = get_selected_mesh()
    if source_obj is None:
        return

    params = parse_cli_params(PARAMS)
    undo_push("Generate Dirty Topology: Start")

    try:
        clean_obj, dirty_obj, original_face_count = prepare_clean_dirty_pair(source_obj)
        select_only(dirty_obj)

        log(
            f"Params: displace_scale={params.displace_scale}, "
            f"displace_strength={params.displace_strength}%, "
            f"merge={params.enable_random_merge}@{params.merge_edge_ratio}, "
            f"disease={params.enable_local_disease} "
            f"centers={params.disease_centers}"
        )

        bm = bmesh.new()
        try:
            bm.from_mesh(dirty_obj.data)
            stats = apply_dirty_topology_to_bmesh(bm, params)
            bm.to_mesh(dirty_obj.data)
            dirty_obj.data.update()
        finally:
            bm.free()

        undo_push("Generate Dirty Topology: Finish")
        select_only(dirty_obj)

        log("=" * 60)
        log("Dirty topology generation complete")
        log(f"Clean object: {clean_obj.name} (hidden)")
        log(f"Dirty object: {dirty_obj.name} (selected)")
        log(f"Original face count: {original_face_count}")
        log(f"Dirty face count: {len(dirty_obj.data.polygons)}")
        log(f"Displace magnitude: {stats.get('displace_magnitude', 0):.6g}")
        log(f"Collapsed edges: {stats.get('collapsed_edges', 0)}")
        log(f"Rotated edges: {stats.get('rotated_edges', 0)}")
        log(f"Local disease centers: {stats.get('disease_center_count', 0)}")
        log(f"Merged duplicate verts: {stats.get('merged_verts', 0)}")
        log(f"All triangles: {'yes' if stats.get('all_triangles') else 'no'}")
        log("=" * 60)
    except Exception as exc:
        log(f"Error: {exc}")
        raise


if __name__ == "__main__" or "CTX" in globals():
    main()
