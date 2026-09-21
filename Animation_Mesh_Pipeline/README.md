# Animation Mesh Pipeline

完整两阶段流程：动画帧采样 → 变脏拓扑。在 Blender 内运行（`--background` 或 Development → Run Script）。

产出 `clean.fbx` + `dirty*.fbx` 后，交给 `lab/perface-data-preproc-pipeline/mesh_retopo_data_preproc` 做 **FBX → 带 feature/label 的训练用 PLY**（本 pipeline 不再做几何 PLY 转换）。

> **本目录为当前维护的正式 clean → dirty pipeline。**  
> 动画筛选（~2000 → ~200）在 [`Scripts/Mesh_Clean_To_Dirty/`](../../Mesh_Clean_To_Dirty/)（只放筛选 / 组包脚本）。

```
Animation_Mesh_Pipeline/
├── README.md
├── stage1_sample/
│   └── batch_animation_frame_sampler.py   # ① 采样 clean 变形网格
└── stage2_dirty/
    ├── dirty_topology_core.py             # 变脏算法核心
    ├── generate_dirty_topology.py         # 单 mesh 预览
    └── batch_apply_dirty.py               # ② 批量变脏
```

---

## 总览

```
角色包（ModelWithAnimationSelected：单角色 或 多角色根）
  → ① 插针导出 clean.fbx
  → ② 对 clean 写 dirty{强度}.fbx
  → mesh_retopo_data_preproc（外部）→ dirty*.ply（feature + label）
```

| 阶段 | 脚本 | 输入 | 输出 |
|------|------|------|------|
| ① | `stage1_sample/batch_animation_frame_sampler.py` | 角色目录 / 多角色根 | `<char>/<anim>_<frame>/clean.fbx` |
| ② | `stage2_dirty/batch_apply_dirty.py` | ① 的输出根 | 同目录 `dirty10.fbx` … |

---

## 输入约定（多角色 / 单角色）

Stage ① 的 `--input_dir` 读取 **ModelWithAnimationSelected** 布局：

### 多角色（推荐）

传数据集根目录。每个子文件夹是一个角色：

```
ModelWithAnimationSelected/          ← --input_dir
├── dataset_manifest.json            （可选，脚本忽略）
├── Aj/
│   ├── Aj.fbx                       # 角色模型（蒙皮网格 + 骨架）
│   └── animations/
│       ├── Back_Flip_To_Uppercut.fbx
│       ├── Bartending.fbx
│       └── ...
├── Arissa/
│   ├── Arissa.fbx
│   └── animations/
│       └── ...
└── ...
```

### 单角色

直接传单个角色目录：

```
Aj/                                  ← --input_dir
├── Aj.fbx
└── animations/
    └── ...
```

规则：
- 角色模型：优先 `{目录名}.fbx`，否则用根目录第一个含 MESH 的 FBX
- 动画：只读 `animations/*.fbx`（不扫角色根目录下的其它 FBX）
- 有一级合法角色子目录 → 多角色；目录本身就是角色包 → 单角色

---

## ① 动画帧采样

找 mesh → 把动画 Action 赋到角色自己的 armature（mesh 保持绑在 bind pose 骨架上）→ 按 `frame_gap` 插针 → 导出 `clean.fbx`。

```bash
Blender --background --python stage1_sample/batch_animation_frame_sampler.py -- \
  --input_dir ".../Mixamo_Data/ModelWithAnimationSelected" \
  --output_dir ".../Data/output_animation_frames" \
  --max_armatures 50 \
  --frame_gap 20
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `--input_dir` | `Mixamo_Data/ModelWithAnimationSelected` | 多角色根，或单角色目录 |
| `--output_dir` | `Data/output_animation_frames` | 导出根 |
| `--max_characters` | all | 处理多少个角色；`<0` = 全部 |
| `--character_start` | `0` | 跳过前 N 个角色 |
| `--max_armatures` | `50` | 每个角色采样多少个动画；`<0` = 全部 |
| `--armature_start` | `0` | 每个角色跳过前 N 个动画 |
| `--frame_gap` | `20` | 帧间隔（插针） |
| `--shuffle` | off | 随机抽动画，而不是按文件名 |
| `--export_format` | `fbx` | `fbx` / `obj` / `ply` |

也可改脚本顶部常量：`MAX_CHARACTERS`、`MAX_ARMATURES`、`FRAME_GAP`、`SHUFFLE_ARMATURES`。

**输出：**

```
output/
├── Aj/
│   ├── Back_Flip_To_Uppercut_1/clean.fbx
│   ├── Bartending_1/clean.fbx
│   └── ...
├── Arissa/
│   └── ...
├── run_log.txt
└── error_log.txt
```

---

## ② 变脏拓扑

### 算法

1. **先三角化**
2. **切面 XY 随机位移**（沿顶点法线切平面的局部 u/v）
3. **随机融并**（按比例 collapse 边）
4. **局部病变**（随机中心 → n-ring 细分 + 局部 collapse）

位移量：

```
magnitude = bbox_diagonal × displace_scale × (displace_strength / 100)
```

- `displace_strength=100` → 满强度  
- `displace_strength=50` → 一半  

### 单 mesh 预览 `generate_dirty_topology.py`

选中 MESH → Run Script → 生成 `_clean`（隐藏）+ `_dirty`。

改脚本顶部 `PARAMS`，或：

```bash
Blender --python stage2_dirty/generate_dirty_topology.py -- \
  --displace_strength 50 --merge_edge_ratio 0.08
```

### 批量 `batch_apply_dirty.py`

对采样输出目录递归找 `clean.*`，同目录按强度写多档 dirty：

```bash
Blender --background --python stage2_dirty/batch_apply_dirty.py -- \
  --input_dir ".../Data/output_animation_frames" \
  --displace_strengths 10,20,40,70,100 \
  --merge_edge_ratio 0.05 \
  --disease_centers 2
```

每帧输出示例：`clean.fbx` + `dirty10.fbx` / `dirty20.fbx` / `dirty40.fbx` / `dirty70.fbx` / `dirty100.fbx`

| 参数 | 默认 | 说明 |
|------|------|------|
| `--displace_strengths` | `10,20,40,70,100` | 每帧导出的位移强度列表（%） |
| `--displace_scale` | `0.002` | 相对 bbox 的基础位移尺度 |
| `--merge_edge_ratio` | `0.05` | 随机融并边比例 |
| `--disease_centers` | `2` | 病变中心数 |
| `--disease_collapse_ratio` | `0.08` | 病变区 collapse 比例 |
| `--no_merge` / `--no_disease` | — | 关掉对应步骤 |

更细的 knobs 在 `dirty_topology_core.py` 的 `DirtyParams`。

---

## 下游：训练用 PLY（`mesh_retopo_data_preproc`）

本 pipeline 只产出 mesh FBX。训练前用外部预处理把 `clean.fbx` + `dirty*.fbx` 转成带 per-face feature/label 的 ASCII PLY：

```bash
cd lab/perface-data-preproc-pipeline/mesh_retopo_data_preproc
Blender --background --python launcher.py -- \
  --input  ".../Data/output_animation_frames" \
  --output ".../Data/output_animation_frames_preproc"
```

详见该目录下的 README。

---

## 推荐调参

| 目标 | 建议 |
|------|------|
| 轻微脏 | `displace_strength=30~50`, `merge_edge_ratio=0.02`, `disease_centers=1` |
| 中等 | `displace_strength=100`, `merge_edge_ratio=0.05`, `disease_centers=2` |
| 很脏 | `displace_strength=150~200`, `merge_edge_ratio=0.1`, `disease_centers=4` |

先用 `generate_dirty_topology.py` 在单个 clean 上试参数，再批量跑。

---

## 推荐跑法

1. **单角色冒烟**：`--input_dir` 指到一个角色包，`--max_armatures 5`，`--frame_gap 9999`
2. **预览 dirty**：Blender 里打开一个 `clean.fbx`，跑 `generate_dirty_topology.py`
3. **多角色正式**：`--input_dir` 指多角色根，按需 `--max_characters` / `--character_start`
4. **批量 dirty** → 交给 `mesh_retopo_data_preproc`
