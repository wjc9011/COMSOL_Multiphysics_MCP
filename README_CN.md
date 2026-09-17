# COMSOL MCP Server

基于 MCP 协议的 COMSOL Multiphysics 仿真自动化服务器，支持 AI 代理（如 Claude）直接控制 COMSOL 进行多物理场仿真。

[English](README.md) | 中文

## ⭐ Star History

[![GitHub stars](https://img.shields.io/github/stars/wjc9011/COMSOL_Multiphysics_MCP?style=social)](https://github.com/wjc9011/COMSOL_Multiphysics_MCP/stargazers)

[![Star History Chart](https://starchart.cc/wjc9011/COMSOL_Multiphysics_MCP.svg)](https://starchart.cc/wjc9011/COMSOL_Multiphysics_MCP)

## 📄 相关论文与引用

介绍 COMSOL-MCP 的研究论文已发表于 *Neurocomputing*：

> Naiyin Zhang and Junchao Wang, “[COMSOL-MCP: An open-source model context protocol interface for AI-assisted multiphysics simulation](https://doi.org/10.1016/j.neucom.2026.134481),” *Neurocomputing*, vol. 703, article 134481, 2026.

出版商页面：[ScienceDirect](https://linkinghub.elsevier.com/retrieve/pii/S0925231226018795)

如果 COMSOL-MCP 对您的研究有帮助，请引用该论文：

```bibtex
@article{Zhang2026COMSOLMCP,
  title   = {COMSOL-MCP: An Open-Source Model Context Protocol Interface for AI-Assisted Multiphysics Simulation},
  author  = {Zhang, Naiyin and Wang, Junchao},
  journal = {Neurocomputing},
  volume  = {703},
  pages   = {134481},
  year    = {2026},
  doi     = {10.1016/j.neucom.2026.134481},
  url     = {https://doi.org/10.1016/j.neucom.2026.134481}
}
```

### 引用指引

- 介绍 COMSOL-MCP 的方法、架构、功能或评估结果时，请引用上述论文。
- 使用或修改本软件时，建议同时引用论文和代码仓库。可使用 GitHub 的 **Cite this repository**（引用此仓库）菜单；引用信息由 [`CITATION.cff`](CITATION.cff) 提供。
- 为确保研究可复现，请注明所使用的具体发布版本或 commit，并在数据/代码可用性声明中附上仓库链接和访问日期。

## 🎯 项目目标

构建完整的 COMSOL MCP Server，使 AI 代理能够通过 MCP 协议执行多物理场仿真：

1. **模型管理** - 创建、加载、保存、版本控制
2. **几何建模** - 长方体、圆柱、球体、布尔运算
3. **物理场配置** - 传热、流体、静电、固体力学
4. **网格与求解** - 自动网格划分、稳态/瞬态研究
5. **结果可视化** - 表达式求值、导出图表
6. **知识库集成** - 内置物理指南 + PDF 语义搜索

## 📋 系统要求

| 组件 | 要求 |
|------|------|
| COMSOL Multiphysics | 5.x 或 6.x 版本 |
| Python | 3.10+（**不要使用 Microsoft Store 版本**） |
| Java 运行时 | MPh/COMSOL 需要 |

## 🚀 安装步骤

```bash
# 1. 克隆仓库
git clone https://github.com/wjc9011/COMSOL_Multiphysics_MCP.git
cd COMSOL_Multiphysics_MCP

# 2. 安装依赖
python -m pip install -e .

# 3. 测试服务器
python -m src.server
```

### 构建 PDF 知识库（可选）

```bash
# 安装额外依赖
pip install pymupdf chromadb sentence-transformers

# 构建知识库
python scripts/build_knowledge_base.py

# 检查状态
python scripts/build_knowledge_base.py --status
```

## ⚙️ 配置方法

### 方法一：Claude Desktop 配置

编辑 `%APPDATA%\Claude\claude_desktop_config.json`（Windows）或 `~/Library/Application Support/Claude/claude_desktop_config.json`（macOS）：

```json
{
  "mcpServers": {
    "comsol": {
      "command": "python",
      "args": ["-m", "src.server"],
      "cwd": "/path/to/COMSOL_Multiphysics_MCP"
    }
  }
}
```

### 方法二：Claude Code 配置

在项目根目录创建 `.mcp.json`：

```json
{
  "mcpServers": {
    "comsol": {
      "command": "python",
      "args": ["-m", "src.server"],
      "cwd": "/path/to/COMSOL_Multiphysics_MCP",
      "env": {
        "JAVA_HOME": "/path/to/java"
      }
    }
  }
}
```

### 方法三：opencode 配置

在项目根目录创建 `opencode.json`：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "comsol": {
      "type": "local",
      "command": ["python", "-m", "src.server"],
      "enabled": true,
      "environment": {
        "HF_ENDPOINT": "https://hf-mirror.com"
      },
      "timeout": 30000
    }
  }
}
```

## 📁 代码结构

```
COMSOL_Multiphysics_MCP/
├── src/
│   ├── server.py                    # MCP 服务器入口
│   ├── tools/
│   │   ├── session.py               # COMSOL 会话管理
│   │   ├── model.py                 # 模型 CRUD + 版本控制
│   │   ├── parameters.py            # 参数管理 + 参数扫描
│   │   ├── geometry.py              # 几何创建（长方体/圆柱/球）
│   │   ├── physics.py               # 物理场接口 + 边界条件
│   │   ├── mesh.py                  # 网格生成
│   │   ├── study.py                 # 研究创建 + 求解（同步/异步）
│   │   └── results.py               # 结果求值 + 导出
│   ├── resources/
│   │   └── model_resources.py       # MCP 资源（模型树、参数）
│   ├── knowledge/
│   │   ├── embedded.py              # 内置物理指南 + 故障排除
│   │   ├── retriever.py             # PDF 向量搜索
│   │   └── pdf_processor.py         # PDF 分块 + 嵌入
│   ├── async_handler/
│   │   └── solver.py                # 异步求解 + 进度追踪
│   └── utils/
│       └── versioning.py            # 模型版本路径管理
├── scripts/
│   └── build_knowledge_base.py      # 构建 PDF 向量数据库
├── client_script/                   # 独立建模脚本（示例）
└── comsol_models/                   # 保存的模型（结构化）
```

## 🛠️ 可用工具（100+ 个）

### 会话管理（4 个）

| 工具 | 说明 |
|------|------|
| `comsol_start` | 启动本地 COMSOL 客户端 |
| `comsol_connect` | 连接远程服务器 |
| `comsol_disconnect` | 清除会话 |
| `comsol_status` | 获取会话信息 |

### 模型管理（9 个）

| 工具 | 说明 |
|------|------|
| `model_load` | 加载 .mph 文件 |
| `model_create` | 创建空模型 |
| `model_create_component` | 创建组件（支持 2D/3D） |
| `model_save` | 保存到文件 |
| `model_save_version` | 带时间戳保存 |
| `model_list` | 列出已加载模型 |
| `model_set_current` | 设置当前模型 |
| `model_clone` | 克隆模型 |
| `model_inspect` | 获取模型结构 |

### 参数管理（5 个）

| 工具 | 说明 |
|------|------|
| `param_get` | 获取参数值 |
| `param_set` | 设置参数 |
| `param_list` | 列出所有参数 |
| `param_sweep_setup` | 设置参数扫描 |
| `param_description` | 获取/设置参数描述 |

### 几何建模（16 个）

| 工具 | 说明 |
|------|------|
| `geometry_list` | 列出几何序列 |
| `geometry_create` | 创建几何序列 |
| `geometry_add_feature` | 添加通用特征 |
| `geometry_add_block` | 添加长方体 |
| `geometry_add_cylinder` | 添加圆柱体 |
| `geometry_add_sphere` | 添加球体 |
| `geometry_add_rectangle` | 添加 2D 矩形 |
| `geometry_add_circle` | 添加 2D 圆形 |
| `geometry_boolean_union` | 布尔并集 |
| `geometry_boolean_difference` | 布尔差集 |
| `geometry_import` | 导入 CAD 文件 |
| `geometry_build` | 构建几何 |
| `geometry_list_features` | 列出特征 |
| `geometry_get_boundaries` | 获取边界编号 |
| `geometry_create_box_selection` | 按位置创建命名选择 |
| `geometry_create_side_selections` | 创建左/右/上/下边界选择 |

### 物理场配置（28 个）

| 工具 | 说明 |
|------|------|
| `physics_list` | 列出物理场接口 |
| `physics_get_available` | 可用物理场类型 |
| `physics_add` | 添加通用物理场 |
| `physics_add_electrostatics` | 添加静电场 |
| `physics_add_solid_mechanics` | 添加固体力学 |
| `physics_add_heat_transfer` | 添加传热 |
| `physics_add_laminar_flow` | 添加层流 |
| `physics_add_acoustics` | 按 COMSOL 类型添加基于几何的声学接口 |
| `physics_add_pressure_acoustics` | 添加压力声学 |
| `physics_add_coefficient_form_pde` | 添加系数形式 PDE |
| `physics_add_general_form_pde` | 添加一般形式 PDE |
| `physics_add_weak_form_pde` | 添加弱形式 PDE |
| `physics_configure_boundary` | 配置边界条件 |
| `physics_configure_acoustic_boundary` | 配置单个声学边界条件 |
| `physics_setup_acoustic_boundaries` | 批量配置声学边界条件 |
| `physics_configure_pde_boundary` | 配置单个 PDE 边界条件 |
| `physics_setup_pde_boundaries` | 批量配置 PDE 边界条件 |
| `physics_get_acoustic_boundary_conditions` | 列出常用声学边界 feature 类型 |
| `physics_get_pde_boundary_conditions` | 列出常用 PDE 边界 feature 类型 |
| `physics_set_material` | 分配材料 |
| `physics_list_features` | 列出物理场特征 |
| `physics_remove` | 删除物理场 |
| `multiphysics_add` | 添加多物理场耦合 |
| `physics_interactive_setup_heat` | 交互式传热设置 |
| `physics_setup_heat_boundaries` | 配置传热边界 |
| `physics_interactive_setup_flow` | 交互式流体设置 |
| `physics_boundary_selection` | 通用边界设置 |

### 电化学（10 个）

电池、燃料电池、电镀与腐蚀支持，基于**在真实 COMSOL 6.3 会话中逐一创建验证**的类型
注册表（26 个接口、600+ 特征/子特征类型）。详见
`src/knowledge/prompts/electrochemistry.md`，或调用 `docs_get("electrochemistry")`。

| 工具 | 说明 |
|------|------|
| `electrochemistry_get_interfaces` | 列出已验证的接口类型（电池、电流分布、腐蚀等） |
| `electrochemistry_get_feature_types` | 列出某接口已验证的特征类型及维度 |
| `electrochemistry_add_interface` | 添加已验证接口（如 `LithiumIonBatteryMPH`、`LumpedBattery`） |
| `electrochemistry_list_features` | 用 COMSOL 真实类型名（`getType()`）内省特征 |
| `electrochemistry_add_feature` | 创建特征，子特征通过 `parent_feature` 指定父节点 |
| `electrochemistry_set_feature_selection` | 为已有特征指定域/边界选择 |
| `electrochemistry_set_feature_properties` | 设置属性（被拒绝的属性名会显式报告） |
| `electrochemistry_list_feature_properties` | 向 COMSOL 查询特征的合法属性名 |
| `electrochemistry_list_interface_properties` | 读取接口级属性组(如 `BatterySettings`) |
| `electrochemistry_set_interface_properties` | 配置接口级设置(容量/SOC/电流等) |

### 网格划分（4 个）

| 工具 | 说明 |
|------|------|
| `mesh_list` | 列出网格序列 |
| `mesh_create_sequence` | 创建网格序列 |
| `mesh_create` | 生成网格 |
| `mesh_info` | 获取网格统计 |

### 研究与求解（9 个）

| 工具 | 说明 |
|------|------|
| `study_list` | 列出研究 |
| `study_create` | 创建研究（稳态/瞬态/特征频率） |
| `study_solve` | 同步求解 |
| `study_solve_async` | 后台求解 |
| `study_get_progress` | 获取进度 |
| `study_cancel` | 取消求解 |
| `study_wait` | 等待完成 |
| `solutions_list` | 列出解 |
| `datasets_list` | 列出数据集 |

### 结果后处理（8 个）

| 工具 | 说明 |
|------|------|
| `results_evaluate` | 求值表达式 |
| `results_global_evaluate` | 求值标量 |
| `results_inner_values` | 获取时间步 |
| `results_outer_values` | 获取扫描值 |
| `results_export_data` | 导出数据 |
| `results_export_image` | 导出图像 |
| `results_exports_list` | 列出导出节点 |
| `results_plots_list` | 列出绘图节点 |

### 知识库（8 个）

| 工具 | 说明 |
|------|------|
| `docs_get` | 获取文档 |
| `docs_list` | 列出可用文档 |
| `physics_get_guide` | 物理场快速指南 |
| `troubleshoot` | 故障排除帮助 |
| `modeling_best_practices` | 最佳实践 |
| `pdf_search` | 搜索 PDF 文档 |
| `pdf_search_status` | PDF 搜索状态 |
| `pdf_list_modules` | 列出 PDF 模块 |

## 📚 使用示例

### 示例 1：芯片热分析（TSV）

3D 热分析：含硅通孔（TSV）的硅芯片。

**几何**: 60×60×5 µm 芯片，5 µm 直径 TSV 孔，10×10 µm 热源

```python
# 主要步骤：
# 1. 创建芯片块和 TSV 圆柱
# 2. 布尔差集（从芯片减去 TSV）
# 3. 添加硅材料（k=130 W/m·K）
# 4. 添加传热物理场
# 5. 设置顶部热通量，底部固定温度
# 6. 求解并评估温度分布
```

**脚本**: `client_script/create_chip_tsv_final.py`

### 示例 2：微混合器流体仿真

微流控通道中的 3D 层流仿真。

**几何**: 600×100×50 µm 矩形通道

```python
# 主要步骤：
# 1. 创建矩形通道块
# 2. 添加水材料（ρ=1000 kg/m³，μ=0.001 Pa·s）
# 3. 添加层流物理场
# 4. 设置入口速度（1 mm/s），出口压力
# 5. 添加稀物质传递用于混合分析
# 6. 求解并评估速度分布
```

**脚本**: `client_script/create_micromixer_auto.py`

## 📝 关键技术说明

### mph 库 API 模式

```python
# 通过属性访问 Java 模型（不是可调用对象）
jm = model.java  # 不是 model.java()

# 创建组件（第三个参数为维度）
comp = jm.component().create('comp1', True, 3)  # 3 = 3D

# 创建 3D 几何
geom = comp.geom().create('geom1', 3)

# 创建物理场
physics = comp.physics().create('ht', 'HeatTransfer', 'geom1')

# 创建边界条件
bc = physics.create('bc1', 'HeatFlux')
bc.selection().set([1, 2, 3])
bc.set('q0', '1e6[W/m^2]')
```

### 边界条件属性名

| 物理场 | 条件类型 | 属性名 |
|--------|----------|--------|
| 传热 | HeatFlux | `q0` |
| 传热 | Temperature | `T0` |
| 传热 | ConvectiveHeatFlux | `h`，`Text` |
| 层流 | InletBoundary | `U0` |
| 层流 | OutletBoundary | `p0` |
| 固体力学 | Fixed | 无额外属性 |
| 固体力学 | BoundaryLoad | `Fx`，`Fy`，`Fz` |
| 压力声学 | SoundHard | 无额外属性 |
| 压力声学 | SoundSoft | 无额外属性 |
| 压力声学 | Pressure | `p0` |
| 压力声学 | Impedance | `Zn` |
| 压力声学 | NormalAcceleration | `nacc` |
| 压力声学 | NormalVelocity | `nvel` |
| 压力声学 | PlaneWaveRadiation | 无额外属性 |
| 压力声学 | SphericalWaveRadiation | 无额外属性 |
| PDE | DirichletBoundary | `r` |
| PDE | FluxBoundary | `g`，`q` |
| PDE | ZeroFluxBoundary | 无额外属性 |
| PDE | WeakContribution | `weak` |
| PDE | PeriodicCondition | 无额外属性 |

### 离线嵌入模型

PDF 搜索支持离线操作，使用本地 HuggingFace 缓存：

```bash
# 国内用户设置镜像
export HF_ENDPOINT=https://hf-mirror.com
```

## 🔧 开发状态

| 阶段 | 描述 | 状态 |
|------|------|------|
| 1 | 基础框架 + 会话 + 模型管理 | ✅ 完成 |
| 2 | 参数 + 求解 + 结果 | ✅ 完成 |
| 3 | 几何 + 物理场 + 网格 | ✅ 完成 |
| 4 | 内置知识库 + 工具文档 | ✅ 完成 |
| 5 | PDF 向量检索 | ✅ 完成 |
| 6 | 集成测试 | 🔄 进行中 |

## 📊 MCP 资源

| URI | 说明 |
|-----|------|
| `comsol://session/info` | 会话信息 |
| `comsol://model/{name}/tree` | 模型树结构 |
| `comsol://model/{name}/parameters` | 模型参数 |
| `comsol://model/{name}/physics` | 物理场接口 |

## 📄 许可证

MIT
