"""
Batch Animation Frame Sampler (bpy).

输入布局（ModelWithAnimationSelected）：
  input_dir/                         ← 多角色根，或单个角色目录
    ├── dataset_manifest.json        （可选，忽略）
    ├── Aj/
    │   ├── Aj.fbx                   # 角色模型（蒙皮网格 + 骨架）
    │   └── animations/
    │       ├── Walk.fbx
    │       └── ...
    └── Arissa/
        ├── Arissa.fbx
        └── animations/
            └── ...

对每个角色：加载根目录角色 FBX → 把 animations/ 里的 Action 赋到角色骨架
→ 按 frame_gap 插针导出 clean。

用法（Blender background）:
  Blender --background --python stage1_sample/batch_animation_frame_sampler.py -- \\
    --input_dir "/path/to/ModelWithAnimationSelected" \\
    --output_dir "/path/to/out" \\
    --max_armatures 50 \\
    --frame_gap 20
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import traceback
from pathlib import Path

import bpy

# =========================
# 默认路径 / 可调参数
# =========================
SCRIPT_DIR = Path(__file__).resolve().parent
PIPELINE_ROOT = SCRIPT_DIR.parent
# stage1_sample → Animation_Mesh_Pipeline → Animation_Mesh_Pipeline → Scripts → Data_Processing
PROJECT_ROOT = PIPELINE_ROOT.parent.parent.parent

DEFAULT_INPUT_DIR = PROJECT_ROOT / "Mixamo_Data" / "ModelWithAnimationSelected"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "Data" / "output_animation_frames"

ANIMATIONS_SUBDIR = "animations"
FRAME_GAP = 20
MAX_CHARACTERS = None  # None / <0 = 全部角色子目录
CHARACTER_START = 0
MAX_ARMATURES = 50  # 每个角色采样 armature 数量；None / <0 = 全部
ARMATURE_START = 0
EXPORT_FORMAT = "fbx"  # fbx | obj | ply
ACTIONS_PER_RESET = 50
SHUFFLE_ARMATURES = False  # True = 随机抽 N 个；False = 按文件名排序截取
RANDOM_SEED = 42

RUN_LOG_PATH: str | None = None


def log(msg: str) -> None:
    text = f"[AnimFrameSampler] {msg}"
    print(text)
    if RUN_LOG_PATH:
        try:
            with open(RUN_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass


def show_popup(title: str, message: str, icon: str = "INFO") -> None:
    try:
        if getattr(bpy.app, "background", False):
            return
        wm = bpy.context.window_manager
        if wm is None:
            return

        def _draw(self, _context):
            for line in message.split("\n"):
                self.layout.label(text=line)

        wm.popup_menu(_draw, title=title, icon=icon)
    except Exception:
        pass


def sanitize_name(name: str) -> str:
    return re.sub(r"[^\w\-.]+", "_", name).strip("_") or "unnamed"


def purge_orphans() -> None:
    for _ in range(3):
        try:
            bpy.ops.outliner.orphans_purge(
                do_local_ids=True, do_linked_ids=True, do_recursive=True
            )
        except Exception:
            break


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    purge_orphans()


def import_fbx_collect_objects(fbx_path: Path) -> list[bpy.types.Object]:
    before = set(bpy.data.objects.keys())
    bpy.ops.import_scene.fbx(filepath=str(fbx_path), automatic_bone_orientation=True)
    after = set(bpy.data.objects.keys())
    return [bpy.data.objects[n] for n in (after - before) if n in bpy.data.objects]


def delete_objects(objects: list[bpy.types.Object]) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    selected = False
    for obj in objects:
        if obj and obj.name in bpy.data.objects:
            obj.select_set(True)
            selected = True
    if selected:
        bpy.ops.object.delete(use_global=False)
    purge_orphans()


def import_contains_mesh(imported_objects: list[bpy.types.Object]) -> bool:
    return any(obj.type == "MESH" for obj in imported_objects)


def get_primary_armature(objects: list[bpy.types.Object]) -> bpy.types.Object | None:
    armatures = [o for o in objects if o.type == "ARMATURE"]
    if not armatures:
        return None
    return sorted(armatures, key=lambda o: len(o.data.bones), reverse=True)[0]


def collect_mesh_objects(objects: list[bpy.types.Object]) -> list[bpy.types.Object]:
    return [o for o in objects if o.type == "MESH" and o.name in bpy.data.objects]


def probe_fbx_has_mesh(fbx_path: Path) -> bool:
    clear_scene()
    imported = import_fbx_collect_objects(fbx_path)
    try:
        return import_contains_mesh(imported)
    finally:
        delete_objects(imported)


def character_root_fbx_files(character_dir: Path) -> list[Path]:
    """角色根目录下的 *.fbx（不含 animations/ 子目录）。"""
    return sorted(
        p for p in character_dir.glob("*.fbx") if p.is_file()
    )


def animations_dir(character_dir: Path) -> Path:
    return character_dir / ANIMATIONS_SUBDIR


def is_character_package(character_dir: Path) -> bool:
    """角色包：根目录有角色 FBX，且 animations/ 下有动画 FBX。"""
    if not character_dir.is_dir():
        return False
    if not character_root_fbx_files(character_dir):
        return False
    anim_dir = animations_dir(character_dir)
    return anim_dir.is_dir() and any(anim_dir.glob("*.fbx"))


def find_mesh_fbx(character_dir: Path) -> Path:
    """
    找角色模型 FBX：优先 {目录名}.fbx，否则根目录第一个含 MESH 的 FBX。
    不扫描 animations/。
    """
    preferred = character_dir / f"{character_dir.name}.fbx"
    if preferred.is_file():
        if probe_fbx_has_mesh(preferred):
            log(f"Mesh FBX (by folder name): {preferred.name}")
            return preferred
        raise RuntimeError(
            f"Found {preferred.name}, but import contains no MESH objects."
        )

    fbx_files = character_root_fbx_files(character_dir)
    if not fbx_files:
        raise FileNotFoundError(
            f"No character FBX in root of: {character_dir} "
            f"(expected e.g. {character_dir.name}.fbx)"
        )

    log(
        f"{character_dir.name}.fbx not found; "
        f"probing {len(fbx_files)} root FBX file(s) for mesh..."
    )
    for fbx_path in fbx_files:
        if probe_fbx_has_mesh(fbx_path):
            log(f"Mesh FBX (auto-detect): {fbx_path.name}")
            return fbx_path

    raise RuntimeError(f"No FBX with MESH objects found in: {character_dir}")


def collect_armature_fbx_files(character_dir: Path, mesh_fbx: Path | None = None) -> list[Path]:
    """角色 animations/ 子目录内的全部动画 FBX。"""
    _ = mesh_fbx  # 角色根与动画目录已分离
    anim_dir = animations_dir(character_dir)
    if not anim_dir.is_dir():
        raise FileNotFoundError(f"Missing animations/ under: {character_dir}")
    return sorted(p for p in anim_dir.glob("*.fbx") if p.is_file())


def discover_character_dirs(input_dir: Path) -> list[tuple[str, Path]]:
    """
    发现角色目录列表，返回 (角色名, 路径)。

    多角色：input_dir 下一级子目录，每个含 {Name}.fbx + animations/*.fbx。
    单角色：input_dir 本身就是角色包。
    """
    subdirs = sorted(
        [
            p
            for p in input_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".") and is_character_package(p)
        ],
        key=lambda p: p.name.lower(),
    )

    if subdirs:
        return [(sanitize_name(p.name), p) for p in subdirs]

    if is_character_package(input_dir):
        return [(sanitize_name(input_dir.name), input_dir)]

    return []


def apply_list_slice(
    items: list,
    start: int,
    limit: int | None,
    shuffle: bool,
) -> list:
    pool = list(items)
    if shuffle:
        random.shuffle(pool)
    if start < 0:
        start = 0
    if start:
        pool = pool[start:]
    if limit is not None and limit >= 0:
        pool = pool[:limit]
    return pool


def ensure_armature_modifier(mesh_obj: bpy.types.Object) -> None:
    if not any(mod.type == "ARMATURE" for mod in mesh_obj.modifiers):
        mesh_obj.modifiers.new(name="Armature", type="ARMATURE")


def retarget_mesh_to_armature(
    mesh_objects: list[bpy.types.Object], armature_obj: bpy.types.Object
) -> None:
    for mesh_obj in mesh_objects:
        ensure_armature_modifier(mesh_obj)
        for mod in mesh_obj.modifiers:
            if mod.type == "ARMATURE":
                mod.object = armature_obj


def strip_namespace_in_action(action: bpy.types.Action) -> None:
    ns_regex = re.compile(r'pose\.bones\["([^"]+)"\]')
    fcurves = getattr(action, "fcurves", None)
    if fcurves is None:
        return

    for fcurve in fcurves:
        data_path = fcurve.data_path

        def _replace(match: re.Match[str]) -> str:
            bone_name = match.group(1).split(":")[-1]
            return f'pose.bones["{bone_name}"]'

        cleaned = ns_regex.sub(_replace, data_path)
        if cleaned != data_path:
            fcurve.data_path = cleaned


def evaluate_mesh_datablocks(
    mesh_objects: list[bpy.types.Object],
) -> list[bpy.types.Mesh]:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    meshes: list[bpy.types.Mesh] = []

    for src_obj in mesh_objects:
        if src_obj.name not in bpy.data.objects:
            continue
        eval_obj = src_obj.evaluated_get(depsgraph)
        try:
            mesh = bpy.data.meshes.new_from_object(
                eval_obj,
                depsgraph=depsgraph,
                preserve_all_data_layers=True,
            )
        except TypeError:
            mesh = bpy.data.meshes.new_from_object(eval_obj, depsgraph=depsgraph)
        if mesh is None:
            continue
        mesh.transform(eval_obj.matrix_world)
        meshes.append(mesh)

    if not meshes:
        raise RuntimeError("No deformed mesh available for export.")
    return meshes


def export_mesh_datablocks(
    mesh_datablocks: list[bpy.types.Mesh],
    export_path: Path,
    export_format: str,
) -> None:
    scene = bpy.context.scene
    temp_created: list[bpy.types.Object] = []

    try:
        for idx, mesh in enumerate(mesh_datablocks):
            obj_name = "mesh" if idx == 0 else f"mesh_{idx}"
            temp_obj = bpy.data.objects.new(obj_name, mesh)
            scene.collection.objects.link(temp_obj)
            temp_obj.matrix_world.identity()
            temp_created.append(temp_obj)

        bpy.ops.object.select_all(action="DESELECT")
        for temp_obj in temp_created:
            temp_obj.select_set(True)
        bpy.context.view_layer.objects.active = temp_created[0]

        export_path.parent.mkdir(parents=True, exist_ok=True)
        filepath = str(export_path)

        if export_format == "fbx":
            bpy.ops.export_scene.fbx(
                filepath=filepath,
                use_selection=True,
                object_types={"MESH"},
                add_leaf_bones=False,
                bake_anim=False,
                use_armature_deform_only=False,
                axis_forward="-Y",
                axis_up="Z",
                apply_unit_scale=True,
                apply_scale_options="FBX_SCALE_NONE",
            )
        elif export_format == "obj":
            if hasattr(bpy.ops, "wm") and hasattr(bpy.ops.wm, "obj_export"):
                bpy.ops.wm.obj_export(
                    filepath=filepath,
                    export_selected_objects=True,
                    export_uv=False,
                    export_normals=True,
                    export_colors=False,
                    export_materials=False,
                )
            else:
                bpy.ops.export_scene.obj(
                    filepath=filepath,
                    use_selection=True,
                    use_materials=False,
                    use_uvs=False,
                    use_normals=True,
                )
        elif export_format == "ply":
            if hasattr(bpy.ops, "wm") and hasattr(bpy.ops.wm, "ply_export"):
                ply_kwargs: dict = {
                    "filepath": filepath,
                    "export_selected_objects": True,
                    "export_uv": False,
                    "export_normals": True,
                }
                # Blender 5.x: export_colors is ENUM, not bool
                color_prop = bpy.ops.wm.ply_export.get_rna_type().properties.get(
                    "export_colors"
                )
                if color_prop is not None:
                    ply_kwargs["export_colors"] = (
                        "NONE" if color_prop.type == "ENUM" else False
                    )
                bpy.ops.wm.ply_export(**ply_kwargs)
            else:
                bpy.ops.export_mesh.ply(
                    filepath=filepath,
                    use_selection=True,
                    use_uvs=False,
                    use_normals=True,
                )
        else:
            raise ValueError(f"Unsupported export format: {export_format}")
    finally:
        bpy.ops.object.select_all(action="DESELECT")
        for temp_obj in temp_created:
            if temp_obj.name in bpy.data.objects:
                temp_obj.select_set(True)
        selected = [o for o in bpy.data.objects if o.select_get()]
        if selected:
            bpy.ops.object.delete(use_global=True)


def export_frame_clean(
    mesh_objects: list[bpy.types.Object],
    export_path: Path,
    export_format: str,
) -> None:
    eval_meshes = evaluate_mesh_datablocks(mesh_objects)
    try:
        export_mesh_datablocks(eval_meshes, export_path, export_format)
    finally:
        for mesh in eval_meshes:
            if mesh.name in bpy.data.meshes:
                bpy.data.meshes.remove(mesh)


def assign_action_to_armature(
    armature_obj: bpy.types.Object, action: bpy.types.Action
) -> bpy.types.Action:
    """Copy action onto character armature (keep mesh bound to its own bind pose)."""
    action_copy = action.copy()
    strip_namespace_in_action(action_copy)
    if armature_obj.animation_data is None:
        armature_obj.animation_data_create()
    armature_obj.animation_data.action = action_copy
    return action_copy


def init_base_scene(
    mesh_fbx: Path,
) -> tuple[list[bpy.types.Object], bpy.types.Object]:
    clear_scene()
    imported = import_fbx_collect_objects(mesh_fbx)
    mesh_objects = collect_mesh_objects(imported)
    if not mesh_objects:
        raise RuntimeError(f"Mesh FBX contains no MESH objects: {mesh_fbx}")

    char_armature = get_primary_armature(imported)
    if char_armature is None:
        raise RuntimeError(f"Mesh FBX contains no ARMATURE: {mesh_fbx}")

    # Keep mesh skinned to its own armature (bind pose). Do NOT point modifier
    # at animation FBX armatures — their rest poses often differ and stretch limbs.
    retarget_mesh_to_armature(mesh_objects, char_armature)

    log(
        f"Loaded base mesh, mesh count: {len(mesh_objects)}, "
        f"armature: {char_armature.name}, bones: {len(char_armature.data.bones)}"
    )
    return mesh_objects, char_armature


def get_action_frame_range_from_action(action: bpy.types.Action) -> tuple[int, int]:
    return int(action.frame_range[0]), int(action.frame_range[1])


def process_single_animation(
    anim_fbx: Path,
    base_mesh_objects: list[bpy.types.Object],
    char_armature: bpy.types.Object,
    output_dir: Path,
    frame_gap: int,
    export_format: str,
) -> tuple[int, int]:
    imported: list[bpy.types.Object] = []
    frame_count = 0
    file_count = 0
    anim_label = sanitize_name(anim_fbx.stem)
    assigned_action: bpy.types.Action | None = None

    try:
        imported = import_fbx_collect_objects(anim_fbx)

        extra_meshes = [o for o in imported if o.type == "MESH"]
        if extra_meshes:
            delete_objects(extra_meshes)
            imported = [o for o in imported if o.name in bpy.data.objects]

        anim_armature = get_primary_armature(imported)
        if anim_armature is None:
            raise RuntimeError(f"No armature found in animation FBX: {anim_fbx}")

        if (
            anim_armature.animation_data is None
            or anim_armature.animation_data.action is None
        ):
            raise RuntimeError(f"Animation FBX has no action: {anim_fbx}")

        src_action = anim_armature.animation_data.action
        assigned_action = assign_action_to_armature(char_armature, src_action)

        # Mesh stays bound to char_armature; animation armature is only an action source.
        retarget_mesh_to_armature(base_mesh_objects, char_armature)

        scene = bpy.context.scene
        frame_start, frame_end = get_action_frame_range_from_action(assigned_action)
        scene.frame_start = frame_start
        scene.frame_end = frame_end

        if frame_gap <= 0:
            raise ValueError(f"frame_gap must be a positive integer, got: {frame_gap}")

        for frame in range(frame_start, frame_end + 1, frame_gap):
            scene.frame_set(frame)
            bpy.context.view_layer.update()

            export_path = (
                output_dir / f"{anim_label}_{frame}" / f"clean.{export_format}"
            )
            export_frame_clean(base_mesh_objects, export_path, export_format)
            frame_count += 1
            file_count += 1

        log(
            f"Done {anim_fbx.name}: {frame_count} frame samples, "
            f"{file_count} files -> {output_dir}"
        )
        return frame_count, file_count
    finally:
        if (
            char_armature.animation_data is not None
            and char_armature.animation_data.action is assigned_action
        ):
            char_armature.animation_data.action = None
        if assigned_action is not None and assigned_action.name in bpy.data.actions:
            bpy.data.actions.remove(assigned_action)
        if imported:
            delete_objects(imported)


def process_character(
    character_name: str,
    character_dir: Path,
    character_output: Path,
    frame_gap: int,
    max_armatures: int | None,
    armature_start: int,
    export_format: str,
    shuffle_armatures: bool,
    error_log_path: Path,
) -> tuple[int, int, int, int]:
    """
    处理单个角色目录。
    返回 (成功动画数, 尝试动画数, 帧采样数, 导出文件数)。
    """
    character_output.mkdir(parents=True, exist_ok=True)

    mesh_fbx = find_mesh_fbx(character_dir)
    all_armatures = collect_armature_fbx_files(character_dir, mesh_fbx)
    total_armatures = len(all_armatures)
    armature_files = apply_list_slice(
        all_armatures, armature_start, max_armatures, shuffle_armatures
    )

    if not armature_files:
        raise FileNotFoundError(
            f"No animation FBX found under: {animations_dir(character_dir)}"
        )

    log(f"Character: {character_name}")
    log(f"  Character dir: {character_dir}")
    log(f"  Output dir: {character_output}")
    log(f"  MESH_FBX={mesh_fbx.name}")
    log(
        f"  Armatures: {len(armature_files)}/{total_armatures} "
        f"(start={armature_start}, max={max_armatures}, shuffle={shuffle_armatures})"
    )

    base_mesh_objects, char_armature = init_base_scene(mesh_fbx)

    processed = 0
    total_frames = 0
    total_files = 0
    attempted = 0

    for idx, anim_fbx in enumerate(armature_files, start=1):
        attempted += 1
        try:
            log(f"  [{idx}/{len(armature_files)}] {anim_fbx.name}")
            frames, files = process_single_animation(
                anim_fbx,
                base_mesh_objects,
                char_armature,
                character_output,
                frame_gap,
                export_format,
            )
            total_frames += frames
            total_files += files
            processed += 1
        except Exception as exc:
            tb = traceback.format_exc()
            with open(error_log_path, "a", encoding="utf-8") as f:
                f.write(
                    f"[{character_name}] {anim_fbx}\n{exc}\n{tb}\n{'-' * 80}\n"
                )
            log(f"  Failed: {anim_fbx.name} | {exc}")
        finally:
            if attempted % ACTIONS_PER_RESET == 0:
                log("  Periodic scene reset: re-importing base mesh...")
                base_mesh_objects, char_armature = init_base_scene(mesh_fbx)

    log(
        f"Character {character_name} done: animations {processed}/{attempted} ok, "
        f"{total_frames} frame samples, {total_files} files."
    )
    return processed, attempted, total_frames, total_files


def run_pipeline(
    input_dir: str,
    output_dir: str,
    frame_gap: int = FRAME_GAP,
    max_characters: int | None = MAX_CHARACTERS,
    character_start: int = CHARACTER_START,
    max_armatures: int | None = MAX_ARMATURES,
    armature_start: int = ARMATURE_START,
    export_format: str = EXPORT_FORMAT,
    shuffle_armatures: bool = SHUFFLE_ARMATURES,
    random_seed: int = RANDOM_SEED,
) -> None:
    in_path = Path(bpy.path.abspath(input_dir)).expanduser().resolve()
    out_path = Path(bpy.path.abspath(output_dir)).expanduser().resolve()

    if not in_path.is_dir():
        raise FileNotFoundError(f"INPUT_DIR does not exist: {in_path}")

    out_path.mkdir(parents=True, exist_ok=True)
    random.seed(int(random_seed))
    global RUN_LOG_PATH
    RUN_LOG_PATH = str(out_path / "run_log.txt")
    error_log_path = out_path / "error_log.txt"
    export_format = export_format.lower().strip(".")

    character_entries = discover_character_dirs(in_path)
    total_characters = len(character_entries)
    character_entries = apply_list_slice(
        character_entries, character_start, max_characters, shuffle=False
    )

    if not character_entries:
        raise FileNotFoundError(
            f"No character packages found under: {in_path}\n"
            f"Expected: <char>/<Name>.fbx + <char>/animations/*.fbx"
        )

    log("Pipeline started (multi-character)")
    log(f"INPUT_DIR={in_path}")
    log(f"OUTPUT_DIR={out_path}")
    log(f"FRAME_GAP={frame_gap}, EXPORT_FORMAT={export_format}")
    log(
        f"Characters: {len(character_entries)}/{total_characters} "
        f"(start={character_start}, max={max_characters})"
    )
    log(
        f"Per-character armatures: start={armature_start}, "
        f"max={max_armatures}, shuffle={shuffle_armatures}, seed={random_seed}"
    )

    chars_ok = 0
    total_anim_ok = 0
    total_anim_attempted = 0
    total_frames = 0
    total_files = 0
    skipped: list[str] = []

    for char_idx, (character_name, character_dir) in enumerate(
        character_entries, start=1
    ):
        log(
            f"=== Character [{char_idx}/{len(character_entries)}] "
            f"{character_name} ==="
        )
        try:
            anim_ok, anim_attempted, frames, files = process_character(
                character_name,
                character_dir,
                out_path / character_name,
                frame_gap,
                max_armatures,
                armature_start,
                export_format,
                shuffle_armatures,
                error_log_path,
            )
            chars_ok += 1
            total_anim_ok += anim_ok
            total_anim_attempted += anim_attempted
            total_frames += frames
            total_files += files
        except Exception as exc:
            tb = traceback.format_exc()
            with open(error_log_path, "a", encoding="utf-8") as f:
                f.write(
                    f"[{character_name}] {character_dir}\n{exc}\n{tb}\n{'-' * 80}\n"
                )
            skipped.append(character_name)
            log(f"Character failed: {character_name} | {exc}")

    summary = (
        f"Done: characters {chars_ok}/{len(character_entries)} ok, "
        f"animations {total_anim_ok}/{total_anim_attempted} ok, "
        f"{total_frames} frame samples, {total_files} files."
    )
    if skipped:
        summary = f"{summary}\nSkipped ({len(skipped)}): " + ", ".join(skipped)
        skipped_path = out_path / "skipped_characters.txt"
        try:
            skipped_path.write_text("\n".join(skipped) + "\n", encoding="utf-8")
        except OSError as exc:
            log(f"Failed to write skipped_characters.txt: {exc}")
    log(summary)
    show_popup("Animation Frame Sampler", summary, icon="INFO")


def prompt_dir(label: str, default_dir: Path, create: bool = False) -> Path:
    raw = input(f"Enter {label} [{default_dir}]: ").strip()
    selected_dir = Path(raw).expanduser().resolve() if raw else default_dir
    if create:
        selected_dir.mkdir(parents=True, exist_ok=True)
    elif not selected_dir.exists():
        raise FileNotFoundError(f"{label} not found: {selected_dir}")
    return selected_dir


def parse_args():
    argv = []
    if "--" in sys.argv:
        argv = sys.argv[sys.argv.index("--") + 1 :]

    parser = argparse.ArgumentParser(
        description=(
            "Sample deformed clean meshes from "
            "input_dir/<character>/{Name}.fbx + animations/*.fbx"
        )
    )
    parser.add_argument(
        "--input_dir",
        default=str(DEFAULT_INPUT_DIR),
        help=(
            "ModelWithAnimationSelected root, or a single character folder "
            "with {Name}.fbx + animations/"
        ),
    )
    parser.add_argument(
        "--output_dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Export root; writes <output>/<character>/<anim>_<frame>/clean.*",
    )
    parser.add_argument(
        "--frame_gap",
        type=int,
        default=FRAME_GAP,
        help="Frame sampling interval (插针间隔)",
    )
    parser.add_argument(
        "--max_characters",
        type=int,
        default=-1 if MAX_CHARACTERS is None else MAX_CHARACTERS,
        help="Max characters to process; <0 = all",
    )
    parser.add_argument(
        "--character_start",
        type=int,
        default=CHARACTER_START,
        help="Skip first N character folders (after sorting)",
    )
    parser.add_argument(
        "--max_armatures",
        type=int,
        default=MAX_ARMATURES if MAX_ARMATURES is not None else -1,
        help="Max armature animations per character; <0 = all",
    )
    parser.add_argument(
        "--armature_start",
        type=int,
        default=ARMATURE_START,
        help="Skip first N armature FBXs per character (after sort / shuffle)",
    )
    parser.add_argument(
        "--export_format",
        choices=("fbx", "obj", "ply"),
        default=EXPORT_FORMAT,
        help="Export format",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="Randomly sample armatures instead of sorted order",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=RANDOM_SEED,
        help="Random seed used by --shuffle (default: 42)",
    )
    return parser.parse_args(argv)


def has_cli_args() -> bool:
    return "--" in sys.argv


def _normalize_limit(value: int) -> int | None:
    return None if value < 0 else value


def main_cli() -> None:
    args = parse_args()
    run_pipeline(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        frame_gap=int(args.frame_gap),
        max_characters=_normalize_limit(int(args.max_characters)),
        character_start=int(args.character_start),
        max_armatures=_normalize_limit(int(args.max_armatures)),
        armature_start=int(args.armature_start),
        export_format=args.export_format,
        shuffle_armatures=bool(args.shuffle),
        random_seed=int(args.seed),
    )


def main_interactive() -> None:
    input_dir = prompt_dir("input_dir", DEFAULT_INPUT_DIR.expanduser().resolve())
    output_dir = prompt_dir(
        "output_dir", DEFAULT_OUTPUT_DIR.expanduser().resolve(), create=True
    )
    log(f"Input dir: {input_dir}")
    log(f"Output dir: {output_dir}")
    run_pipeline(
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        frame_gap=FRAME_GAP,
        max_characters=MAX_CHARACTERS,
        character_start=CHARACTER_START,
        max_armatures=MAX_ARMATURES,
        armature_start=ARMATURE_START,
        export_format=EXPORT_FORMAT,
        shuffle_armatures=SHUFFLE_ARMATURES,
        random_seed=RANDOM_SEED,
    )


def run_entrypoint() -> None:
    if has_cli_args():
        main_cli()
    else:
        main_interactive()


if __name__ == "__main__" or "CTX" in globals():
    try:
        run_entrypoint()
    except Exception as exc:
        log(f"Fatal error: {exc}")
        traceback.print_exc()
        show_popup("Animation Frame Sampler", f"Error:\n{exc}", icon="ERROR")
        raise
