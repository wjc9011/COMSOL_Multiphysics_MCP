# COMSOL 6.4 MCP Server

0.8.0 将研究策略与原生执行分离，保留训练和 surrogate/ONNX，以及[无求解器 Ubuntu 实验支持](docs/experimental_linux/README_CN.md)和[显式严格波长验证](docs/strict_wavelength/README_CN.md)。Windows 仍是原生求解平台。调用工具前请先阅读平台限制。

[English](README.md) | 中文

[![CI](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
![Candidate: 0.8.1](https://img.shields.io/badge/candidate-0.8.1-blue)
![Status: alpha](https://img.shields.io/badge/status-alpha-red)
[![GitHub stars](https://img.shields.io/github/stars/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated?style=social)](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/stargazers)

> [wjc9011/COMSOL_Multiphysics_MCP](https://github.com/wjc9011/COMSOL_Multiphysics_MCP) 的维护型 Fork，已接受 **`COMSOL 6.4.0.*` release line** 和 **MPh 1.3.1 standalone/clientapi**。licensed reference 证据使用 **COMSOL 6.4.0.293**；第三位数字变化视为新的 release family，需要重新验收。

该服务器为 AI agent 提供更安全、更紧凑的 COMSOL 接口，用于模型检查、受控单点验证、可恢复的分段扫描与离线手册检索。它适配 `mph.Client()` 返回的 `model.java` clientapi 对象。该对象与上游面向的直接 `com.comsol.model.Model` API 有实质差异。

## 特色功能

- **证据完整性与 AI 防幻觉验证（默认开启）。** 正式结论可以针对精确的 outcome
  contract、原始 artifact chain、summary citation 以及 resume producer/driver
  identity 做验证。用户可以为了探索逐项 opt-out，但受影响的结果必须携带未验证
  warning。请阅读独立的[证据完整性指南](docs/evidence_integrity/README_CN.md)。
- **COMSOL Desktop/Server 交互协作（默认关闭）。** 用户和 agent 可以在一个用户拥有的
  本地 Server、一个连接的 Desktop 和一个精确 server-held model 上明确轮流操作。
  该模式要求显式启用独立功能以及每次 session confirmation。请阅读
  [交互协作指南](docs/interactive_shared_session/README_CN.md)。
- **五种仿真运行方式。** `interactive`、`inline`、`launcher`、`standalone` 和
  `mphonly` 分别用于短反馈、直接脚本、可恢复的本地长任务、目标机无 Python 的
  Windows 交付，以及可移植的 COMSOL 集群或云端交付。agent 默认只使用前三种。准备
  跨设备方式前必须先询问目标环境。请阅读独立的
  [运行方式指南](docs/simulation_execution_modes/README_CN.md)。

## 推荐配套 Skill

Claude Code、Codex CLI、opencode、Hermes Agent 及其他支持 skill 的 agent，推荐将本服务器与
[COMSOL 6.4+ metasurface agent skill](https://github.com/garbage-enzyme/COMSOL_6_4_agentskill_for_metasurfaces)
配合使用。该 skill 采用短 `SKILL.md` 入口，并按需路由到 clientapi、周期 Wave
Optics、材料与边界、durable jobs、物理证据、资源安全和故障诊断模块，避免每轮
都把整份指南载入上下文。仓库开发和发布流程保留在本仓库的 development kit 中。

## Client 兼容性与部署

早期安装包测试覆盖了 Codex CLI 和 opencode。Windows 11 上的 Claude Code 2.1.220 测试完成了 stdio 初始化，并发现测试所用的 `wave_optics` 工具集。该测试调用了 `capabilities`、`comsol_status` 和 `solver_status`。服务器退出时没有创建求解器租约或 COMSOL 进程。

这些结果只覆盖传输、schema、工具发现和状态调用。它们不能证明 `comsol_start`、真实 COMSOL 的启动、求解、清理或完整错误处理通过验收。

0.7.6 的外部客户端验收尚未完成。Claude 编写的 Python SDK 冒烟测试不能证明 Claude 原生 Tasks 支持。后续持久化扫描虽已完成，但实际求解波长与请求波长不一致。因此，该扫描不能验证请求的光谱。OpenCode 测试在连接失败后停止。本版本尚无这两个客户端的完整原生互操作验收结论。

Hermes Agent 仍只有配置层指导，尚无端到端 client 测试。全新安装、精确配置路径、
profile 选择、重启规则和 solver-free 验证请阅读独立指南：

- [部署指南](DEPLOYMENT_CN.md)

关键规则：使用非 editable 安装。配置安装后的 `comsol-mcp` executable 绝对路径。
普通用户通过设置界面完成配置。修改启动设置或安装包后重启 client。保持 COMSOL 工具
串行。直接编辑 JSON 是面向开发者、自动部署和获得用户明确授权的 agent 的高级路径。
调用 `capabilities` 可在不启动 COMSOL 的情况下验证实际部署的 profile。

仓库内的 client 示例依据 Claude Code 官方
[MCP 文档](https://code.claude.com/docs/en/mcp)、Hermes 官方
[MCP 文档](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md)
和 [client 源码](https://github.com/NousResearch/hermes-agent/blob/main/tools/mcp_tool.py)
编写。一个示例在其精确 client 路径被实际执行前只属于配置指导。真实 client
acceptance 报告应至少包含不启动 COMSOL 的 `initialize`、实时 `list_tools` 和
`capabilities` 回读，并把 licensed start/solve/cleanup 覆盖单独标注。已安装工具界面
以实时 discovery 为准，不以文档中复制的数量为准。

`0.7.6` 把运行基座升级到最新的稳定 MCP Python SDK `2.2.x`。工具、profile、schema
和 stdio 配置保持同一应用合同，MCP 1.x client 通过普通工具路径继续保持 wire 兼容。
SDK 版本、`2026-07-28` 协议修订版和任何 extension wire generation 是三个互相独立
的身份：升级 SDK 不等于实现 extension。

本版本同时把稳定的 `2026-07-28` MCP Tasks extension
（`io.modelcontextprotocol/tasks`）实现为现有 durable job engine 之上的受限
适配层：opt in 的 client 可以启动长时间仿真并通过 `tasks/get` 轮询，而不必阻塞
等待。该适配层不引入第二个调度器：`job_submit` 是唯一 task-capable 工具，只有
当**该次请求**声明了 extension 时才返回 task handle，重复提交相同规格只会返回
已有的 durable job。task TTL 允许后续到期。当前实现会在 TTL 后保留映射，包括已完成任务。
它不会放弃运行中的求解，也不会删除科学证据。取消的协议确认与已验证清理分开报告。未 opt in 的 client 继续
使用原有的 `job_submit`/`job_status`/`job_tail`/`job_cancel`/`job_resume` 工具。
实验性的 `2025-11-25` Tasks 方言会被拒绝，而不是被重新解释。

2026-07-28 核心协议使用逐请求元数据，不使用 initialize 握手。
客户端每次请求都必须声明协议修订版和客户端能力。
旧协议继续使用 initialize 协商和普通工具结果。
Tasks 提交复用普通 `job_submit` 已注册的保护流程。

旧协议 initialize 响应会通过旧 MCP initialize schema 携带一段简短安全说明：先 discovery
和 preflight。只有用户明确要求时才 start、solve 或 mutate。源模型保持只读。执行成功、
证据完整性和科学验证分开报告。client 可以采用或忽略这段提示。真正的安全边界仍由
server 内强制执行的 ownership 和 validation 检查提供。

### 可选的 DeepSeek Harness bridge

仓库还包含 [`dsh_bridge/`](dsh_bridge/)，这是供 DeepSeek Harness 使用的可选、仅仓库
内 Node.js 插件。它通过 MCP stdio 连接已安装的 `comsol-mcp`，以干净名称注册实时发现
的工具，并把持久化的 `job_submit` 镜像到 `ctx.jobs`，提供完成通知、进度输出、取消和
重新挂接。它不是第二个 COMSOL server，不修改 Python 包、公共 schema、求解器所有权或
生产设置。

bridge 不进入 Python wheel 或 sdist，需要 Node.js 20+，且没有 npm 依赖。验证和安装：

```powershell
npm test --prefix dsh_bridge
npm run smoke --prefix dsh_bridge
.\dsh_bridge\install.ps1
```

然后按 [`dsh_bridge/README.md`](dsh_bridge/README.md) 把 `comsol-bridge` 条目加入 DSH
web profile，使用绝对路径的已安装 `comsol-mcp.exe`、runtime 工作目录和共享 settings
路径。bridge 自己的 `config.enabled` Boolean 控制客户端插件是否启用。它有意不是
COMSOL Settings GUI 的 server Boolean。相同 DSH 会话中不要再配置第二个
`dsh-mcp-client` COMSOL 条目，因为 bridge 必须独占并串行化这一条 stdio 连接。
安装、设置变更、验证与恢复边界见版本化的
[`DeepSeek 兼容性契约`](dsh_bridge/DEEPSEEK_COMPATIBILITY.md)。
fake-server 测试只证明 bridge 行为，真实 DSH discovery、cleanup 和 licensed solve 必须
单独记录。
重启恢复会将离线完成通知路由到原会话。通知持久性遵循原生 DSH 语义，不保证会话落盘前崩溃时仍能送达。

## 主要能力

- **ClientAPI 适配。** 几何、物理场、材料、网格、研究、结果、模型克隆和 Unicode 安全的 `.mph` 保存已在 COMSOL 6.4.0.293 上通过 licensed acceptance。6.4.0.* 内只改变最后 build 数字时继承 release-line 结论，其他 release family 在独立验收前均为 unknown。
- **类型化声学与偏微分方程构建。** `basic_fem` 提供可回滚的命名 Box/四边
  selection、Pressure Acoustics、Coefficient/General/Weak Form PDE，以及具有
  精确类型和属性白名单的原子边界批量配置。
- **安全的求解器所有权。** ASCII 路径租约、进程身份核验、外部客户端检测、状态和预检可避免意外启动并发 COMSOL 客户端。
- **持久化后台任务。** 独立 worker 执行调用方声明的分段扫描和 validation matrix，保留不可变规格、原子状态、持久化证据行、检查点、校验后的恢复与已验证的取消清理。[本地启动器](launcher/README_CN.md)支持项目驱动。
- **有界原生伴随优化（实验性）。** `adjoint_optimization` 持久化任务仅支持一个
  allowlist 固定拓扑周期 MIM adapter，并使用已验证的 COMSOL 原生梯度与 GCMMA。
  每次请求必须显式声明 cores、solve/iteration/wall/commit/disk/review budgets。
  接受的结果会绑定 move limit、solution/dataset 身份、重建网格后的独立 forward
  证据和不可变源模型。它不代表通用 CAD、多状态鲁棒优化、拓扑优化或全局最优声明。
- **无需 Python 的独立执行。** 原生 Windows x64 启动器保留三层结构：本机已安装并
  授权的 COMSOL 6.4、由 COMSOL 编译的 Java 单点驱动，以及启动器 EXE。目标机不需要
  Python、Conda、MPh、JPype 或另装 Java。COMSOL 本身仍然必需，项目不会打包或替代它。
- **共享 Desktop 协作（默认关闭）。** 独立的 `shared_server.enabled` 功能可与任意工具 profile 组合，连接用户手动启动的本地 COMSOL Server，精确采用一个 Server 模型，执行非拥有式租约和 revision lock，运行持久化 attached job，并在 detach 时保留用户的 Server、Desktop、listener 和模型。并行 solver owner 最多两个，各自限用一个核心；服务器会拒绝多核求解、第三个 owner 以及跨 owner 的模型访问。
- **COMSOL Model Manager（默认关闭）。** 独立的 `model_manager.enabled` 功能增加有界的 Model Manager 工具，可与任意 profile 组合。另一个独立开关 `model_manager.upload_enabled` 控制是否允许写回 Model Manager。关闭 upload 时 open、run、download 仍可使用，共享会话也绝不会顺带打开这两个开关。即使开启 upload，仍需显式声明派生来源身份，且已存在的目标、revision 不匹配或任何 overwrite 都会被拒绝而不强制写入。每次操作（包括被拒绝的）都会持久记录。
- **Wave Optics 验证。** 专用 profile 支持只读模型预检，以及用于周期性超表面的单波长证据审计。
- **渐进式 discovery。** 冷启动保持精简：`capabilities` 只返回 artifact schema 注册表的紧凑身份与计数视图，而不是全部 141 条记录。只读的 `catalog` 工具可按需返回 domain/tool 摘要、某个 domain 的工具及其完整输入 schema、单个工具或单个具名 schema。完整注册表仍以服务器为准，并与摘要公布的是同一个指纹绑定，因此按需获取的结果不可能与已公布内容不一致。另有默认关闭的 `discovery.pagination_enabled` 开关，用于启用实验性的 `tools/list` 游标分页。关闭时只读取首页的客户端仍会收到常规的完整列表，开启时每页都会声明真实总数，客户端必须读取该总数并继续获取后续页面，才能发现全部工具。
- **有界离线手册检索（默认关闭）。** `manuals.root` 表示原始 COMSOL PDF 目录。独立的 `lexical_docs.enabled` 功能在生成本地索引后增加 SQLite FTS5/BM25 检索和页读取。
- **如实标注的可选语义检索。** 隔离式 `semantic_docs.enabled` 功能可与任意工具 profile 组合，但基线模型未通过质量和内存的晋级门槛。推荐默认使用词法手册检索。

## 统一项目设置

**普通用户首选设置界面。** 它修改 Codex、opencode、Claude Code 和 Hermes 共用的
同一份 `settings.json`，避免不同 agent 悄悄使用不同的 profile、路径、Java runtime 或
证据规则。仓库中的 [`settings.json`](settings.json) 是 canonical 模板，也是开发者、
自动部署和 agent 使用的高级接口。每个设置项的含义、默认值和可接受值见
[设置指南](docs/setting_guide/README_CN.md)。用户删去设置条目时，
该条目回填安全默认值。用户输入非法值时，仅该条目保持默认值，并由 `capabilities` 和
`evidence_integrity_status` 报告 `settings_errors`。JSON 整体损坏时回退完整安全默认值
并报告错误。不要为每个 agent 创建第二份 settings 文件。

从源码树运行时，项目根目录文件可写。安装 wheel 后，包内文件只是只读模板。若没有使用
下面的绝对定位变量，持久设置写入 `%LOCALAPPDATA%/comsol_mcp/settings.json`：

```text
COMSOL_MCP_SETTINGS_PATH=D:\path\to\COMSOL_Multiphysics_MCP\settings.json
```

旧的 `COMSOL_MCP_*`、`COMSOL_SEMANTIC_*` 和 Java 环境变量仍保留一个 release 的
兼容覆盖能力，但已从提交的 client 配置示例中移除。修改 `settings.json` 后，profile、
shared-server 或 Java 设置需要重启 MCP host。随后调用 `capabilities` 检查
`project_settings`。

首次设置默认把 settings 文件和支持 Unicode 的模型读取目录放在
`%LOCALAPPDATA%/comsol_mcp`，把必须仅含 ASCII 字符的 runtime 与自有 artifacts 放在
`%PROGRAMDATA%/comsol_mcp`。可选语义资产保持未设置。

安装后首次使用时，`capabilities.project_settings` 会报告 `setup_required: true`、
`configuration_source: bundled_template`、设置方式 `settings.start` 与 `agent_edit`，以及
修改后需要重启。对普通用户，只调用一次所有 profile 都有的 `settings.start`，说明启动
设置要在重启 Codex 或所属 MCP client 后生效，然后停止继续输出并等待用户下一条消息。
GUI 打开期间不要同时编辑 JSON。只有用户明确要求 agent 管理或自动配置时才使用
`agent_edit`。只修改解析出的可写文件，验证后请求同样的重启。安装后的
`comsol-mcp-settings` 命令可直接打开 GUI。从
仓库或源码分发包使用时也可运行 `./Open_Settings_GUI.ps1`。该脚本会查找受支持的 Python
3.14/3.15 标准 GIL 解释器（通过 py launcher 查找时优先 3.14），也可通过
`-PythonPath` 和 `-SettingsPath` 明确指定。`-ValidateOnly` 只验证启动条件，
不会打开界面或启动 COMSOL。服务器会返回 `agent_action_required: pause_for_user`，但无法从
技术上强制任意第三方 agent 遵守暂停。
安装入口也支持 `--settings-path` 和 `--validate-only`，即使 MCP stdio 已停止也能独立工作。
“关于”页可由用户明确创建或移除每用户 `COMSOL MCP Settings.lnk`。安装和普通 GUI 操作
从不自动创建该快捷方式。快捷方式指向安装后的 Windows GUI-subsystem 入口
`comsol-mcp-settings-gui`，因此不会同时打开终端窗口。控制台入口
`comsol-mcp-settings` 仍用于验证和脚本操作。

## Profile

普通用户在启动服务器前通过设置界面选择 profile。开发者和 agent 可以修改 JSON 中等价的
`profile.name` 字段。一个 profile 在该服务器进程的整个生命周期内固定。更改后需重启。

| Profile | 适用场景 |
| --- | --- |
| `core`（默认） | 紧凑且成熟的控制面：状态、所有权、会话/模型检查及单点求解/求值。 |
| `basic_fem` | 在 `core` 基础上增加传统 FEM 的类型化构建、命名 selection、Pressure Acoustics 与数学 PDE、派生几何编辑、有界导出，以及无需 Python 的独立启动器工具。 |
| `wave_optics` | 超表面推荐：在 `core` 基础上增加派生几何编辑、材料预览、locale-safe 场数据发现及有界 NPZ/manifest 提取、周期网格审计/冒烟、视觉审查合同、Wave Optics 预检和单点/参考审计。持久化分段任务仍通过 `job_submit`。 |
| `electro_chemistry` | 隔离的电化学模块 profile：在 `core` 基础上增加电化学发现、接口创建、电极反应与电解质工具，不并入 `full`。 |
| `experimental` | 显式选择的通用创建、异步、属性逃生口和项目辅助工具。 |
| `full` | 宽兼容/发现界面，包含除隔离的 `electro_chemistry` 增补之外的全部非 feature-gated 工具。 |
| `comsolless_read_only` | 未安装 COMSOL 的电脑：仅提供离线模型/证据探查工具，无法启动 COMSOL 或 Java。 |

0.8 版本从工具发现中移除研究 campaign、优化器、鲁棒规划、谱分析、收敛、
热分析和 robust-shape 工具，不提供兼容包装。仍依赖这些工具的工作流可使用已验收的
0.7.8 包。分析、候选选择、聚合目标与科学验收由调用方负责；训练与 surrogate/ONNX
工具保留在 MCP。

| 移除的职责 | 移除的工具 |
| --- | --- |
| 热分析 | `thermal_kirchhoff_assess`, `thermal_material_evaluate`, `thermal_material_validate`, `thermal_radiation_evaluate` |
| 谱分析 | `spectral_characterize`, `spectral_model_compare` |
| 收敛与延续 | `convergence_evaluate`, `branch_continuation_plan` |
| 研究策略 | `research_campaign_compile`, `research_robustness_plan`, `research_optimizer_advance` |
| 鲁棒 campaign | `robust_shape_plan_preview`, `robust_shape_job_submit`, `robust_shape_evidence_inspect`, `robust_shape_evidence_verify` |

保留的 `job_spec_preview` 和 `job_submit` 支持带
`native_condition_forward` 或 `native_condition_gradient` collector 的 `validation_matrix` 点。
两者的 `inputs` 分别使用 `comsol_mcp.native_condition_forward_request` 或
`comsol_mcp.native_condition_gradient_request`，版本均为 `1.0.0`：
`support`、`shape_policy`、`adapter_configuration`、`condition_table` 和
`initial_values` 绑定一个显式条件与候选。点的波长、入射、源哈希、observable 和网格上限
必须与请求一致。目前原生 adapter 为 `lin2025_pedot_cylinder_v1`。
前向采集保留 condition-controls 的 `1.0.0` 至 `1.5.0`；梯度采集要求
`1.4.0` 或 `1.5.0` 的显式 sensitivity 控制。前向采集不会执行梯度求解。

原生执行必须显式声明 COMSOL 版本、启动 RAM/磁盘策略及资源策略。out-of-core 执行还须
提供调用方已存在的 ASCII 绝对目录 `comsol_temporary_directory`，worker 会验证 Java
回读的 `COMSOL_TMPDIR`。MCP 保留源快照、独占求解器所有权、wall watchdog、取消、恢复
检查和派生模型清理。两种 collector 均持久化前向观测、控制回读与清理证据；
梯度 collector 还保存原始复数梯度。
manifest 状态为 `native_unchecked` 和 `UNVALIDATED`。调用方须独立验证梯度与科学结果
后才能验收。

鲁棒 condition-controls schema `1.4.0` 保持对 `1.3.0` mesh-reference 控制的可读
兼容，并进一步绑定原生 Sensitivity study feature、重新生成的 solver topology、direct
solver 的 out-of-core 策略、derivative solution/dataset、adjoint method 与 stationary
nonlinearity。licensed runtime 会替换不兼容的旧 solver sequence、核验生成身份，并分别
持久化原始 complex `fsens` 与采用的 real component。
该 contract 还会在斜入射导致 Wave Optics 与 material-coordinate constraints 跨组时，
显式合并 COMSOL 生成的两个 segregated steps，并回读单一 merged step 及 caller-selected
direct solver。caller-owned mesh reference 仍只
属于验证 fixture 证据，不是自动粗化规则，也不能替代 finalist 的独立 finer-mesh
convergence。gradient 仍须通过独立 finite-difference 与 directional reconciliation。

鲁棒 condition-controls schema `1.5.0` 增加强制的 `forward_shape_application`
块，使每个 licensed candidate 都在按候选形状真实变形的网格上求值：一个只解
derived shape physics 的线性 `Stationary` 变形阶段被置于 Wave Optics 步骤之前，
重新生成的 sequence 按步骤限定求解范围，并在信任任何 condition solve 之前按变量
回读已求解形状：在网格顶点上求值声明的位移分量，由最大分量顶点测得变形后半径，
并与请求半径比较（相对容差由调用方声明）。gradient 路径会移除该阶段并重新生成
耦合 sequence，因此原生 adjoint receipts 不变。`1.4.0` contract 仍然可读。

Profile 只控制 COMSOL 自动化仿真工具以及未来自主探索工具的可见性。其他正交功能使用
独立、默认关闭的 Boolean 开关。它们可与任意 profile 组合，也可同时开启：

| 功能（高级 JSON 设置） | 增加的工具界面 |
| --- | --- |
| `lexical_docs.enabled=true` | 两个隔离的 SQLite FTS5/BM25 手册检索和精确页读取工具。 |
| `shared_server.enabled=true` | 需要显式确认及精确进程/listener/model 身份的受保护 shared Desktop/attached-Server 工作流。 |
| `model_manager.enabled=true` | 五个有界的 COMSOL Model Manager 工具（status、open、run、download、upload）。默认关闭，可与任意 profile 组合，且独立于 `shared_server.enabled`；附属的 `model_manager.upload_enabled` 设置额外控制是否允许上传调用方指定的派生副本，open、run、download 永不依赖它，overwrite 或 revision 冲突仍会被拒绝。 |
| `semantic_docs.enabled=true` | 三个隔离的实验性向量辅助手册检索工具。 |

调用 `capabilities` 可在不启动 COMSOL 的情况下获知当前 profile、精确注册工具、目标版本、禁用工具组和重启要求。其中有界的 `deployment_identity` 会报告当前代码来自源码树还是已安装包，并给出冻结的 profile/schema 与 catalog 哈希。因此即使版本号相同，也能在重启后识别旧安装或源码遮蔽，且不暴露本机路径。

在设置界面的“工具配置”页开启共享 Desktop/attached-Server 工作。该功能默认关闭，且不依赖
所选 profile。高级 JSON 等价值为 `shared_server.enabled=true`。用户必须手动启动 COMSOL Server，
让 Desktop 连接它，确认 endpoint，并显式确认每次 attach。旧 `comsol_connect` 仍是
experimental 兼容界面，不能替代受保护的 shared-session 生命周期。

来自 capabilities、求解器所有权、持久化任务和词法手册的控制面响应会附带紧凑的滚动 `control_plane` 数据。每种操作最多保留 256 个样本，报告 success/busy/timeout/error 计数和 p50/p95/max 延迟。完整日志及无界遥测不会内联返回。

### Windows 独立启动器

独立路线只面向 Windows 10/11 x64 和 COMSOL 6.4 release line。当前 licensed
acceptance 精确绑定 COMSOL 6.4.0.293。不支持 Windows Server、COMSOL 6.4 以下版本、
Linux 或 macOS。生成的 EXE 只接收一个运行参数：`--comsol-path <COMSOL 根目录>`。
它使用该目录内的 `comsolcompile.exe`、`comsolbatch.exe`、COMSOL 自带 Java、求解器和
许可证。

构建使用受支持 Windows 10/11 workstation 随系统提供并维护的 64 位
`.NET Framework 4.x` C# 编译器：
`%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe`。不需要另装现代
`.NET Runtime`、`.NET Desktop Runtime`、`.NET SDK`、Visual Studio，也不会下载
任何包或访问网络。若系统内置编译器缺失，`standalone_build` 会直接失败，不会安装或
下载替代组件。
微软的 [.NET Framework 平台表](https://learn.microsoft.com/zh-cn/dotnet/framework/install/guide-for-developers)
列出了 Windows 10 和 Windows 11 随系统提供的 4.x 版本。

在普通 Python MCP host 中选择 `basic_fem` profile，可使用 `standalone_build`、
`standalone_start`、`standalone_status`、`standalone_pause`、
`standalone_resume`、`standalone_tail` 和 `standalone_results`。构建与任务目录必须位于
配置的 ASCII 自有产物根目录内。MCP 在执行前核对包内源码身份、构建清单与 EXE 哈希。
EXE 生成后也可复制到目标机直接操作，此时不需要该 Python MCP host。
`standalone_start` 和 `standalone_resume` 有显式 `comsol_root` 时优先使用它。省略时读取共享
设置中的 `comsol.installation_root`。

## 推荐工作流

### 常规求解

1. 调用 `solver_status`。
2. 在连接、启动 COMSOL 或提交较重任务前调用 `solver_preflight`。
3. 使用会话/模型工具，或提交持久化分段扫描。

当检测到外部 MPh/COMSOL 所有者或有效租约时，服务器会拒绝继续启动。`solver_recover_stale_lease` 只有在进程身份信息证明租约过期时才移除它，绝不会终止不属于本服务器的进程。

### 平行板电容器端到端示例

源码仓库包含一个三维介质电容器示例。脚本根据实际测得的边界中心和法向识别两块
电极，分别设置接地与电势边界，并建立静电场稳态研究。默认只构建模型。加入
`--solve` 后，会将电场能量算出的电容与
`真空介电常数 × 相对介电常数 × 面积 ÷ 间距` 的解析结果比较。

```powershell
python -m recipes.parallel_plate_capacitor `
  --output-model D:\comsol_outputs\parallel_plate_capacitor.mph `
  --solve
```

该示例与需要许可证的端到端探针共用建模和验收函数。脚本会先检查求解器所有权，
清理 COMSOL 客户端并释放租约后才发布最终模型。除非显式加入
`--overwrite-output`，否则不会覆盖已有输出。

### 最小 Pressure Acoustics 示例

源码仓库包含一个具有物理意义的二维空气管道示例。上下边界为刚性壁，左侧施加
一帕压力，右侧压力为零。默认频率低于第一横向截止频率。默认只构建模型。加入
`--solve` 后，脚本会将管道中心的数值压力与一维 Helmholtz 解析解比较，误差通过后
才把收据标记为已验证。

```powershell
python -m recipes.acoustic_duct_2d `
  --output-model D:\comsol_outputs\minimal_acoustic_duct.mph `
  --solve
```

脚本会先执行新的求解器所有权预检，要求四条命名边界互不重复，保存到唯一暂存
模型，清理 COMSOL client 和租约后才发布最终 `.mph`。除非显式加入
`--overwrite-output`，否则绝不覆盖已有输出。

持久化扫描控制工具为 `job_submit`、`job_status`、`job_tail`、`job_cancel` 和 `job_resume`。每个任务在 ASCII-only runtime 目录中保存不可变规格、状态、CSV 日志、检查点和日志文件。恢复时只接受规格一致、数值有限且成功完成的行。只有 worker/相关进程清理和租约释放都得到验证后，取消才会进入终态。此协调机制仅适用于同一台主机上共享 runtime 目录的任务，不是分布式或跨主机取消。

拥有求解器的 staged sweep 仅在 client 和租约清理成功后报告 `completed`。
清理失败时保留结果行，并报告 `failed`、`cleanup_pending` 和 `owned_cleanup` 详情。

交互式 `mesh_convergence_study` 也会在 CSV 旁写入 manifest。对已有日志执行
resume 或 append 时必须提供 `source_model_path`。只有源文件哈希、study、表达式、
参数设置、mesh identity 以及每个 level 的精确 properties 全部一致，已完成 level
才会被跳过。CSV 或 checkpoint 持久化失败是该 level 的终止结果，不会再次求解。

三个 solver-free 预览工具把 COMSOL admission 或模型修改前的配置审查变成显式契约。
`simulation_configuration_validate` 只接受封闭的 typed 字段，包括 source/producer
identity、几何语义、层顺序、材料状态与损耗符号、入射与偏振、mesh dependency、
model-tree identity、solver termination、单位和 artifact chain。它统一受支持的单位并
返回内容绑定指纹。`simulation_configuration_diff` 把字段分为 exact、容差内、semantic、
label-only 或 unavailable，标签不会被提升为物理身份。`job_spec_preview` 复用
`job_submit` 的同一个 discriminated input validator，只报告受限的 point/stage 清单、
路径与资源检查、需求及提交时副作用，不做 admission、ownership、文件写入、进程创建或
solver 启动。

0.8.0 移除独立的热辐射和热材料分析工具。
需要这些旧工具的用户请在独立环境中保留 0.7.8。
COMSOL 材料操作和原生热到光执行能力继续保留。

需要许可证的持久化热到光重放任务使用
`job_type: "thermo_optomechanical_replay"`，并提供 ASCII JSON
`specification_path` 与精确 `specification_sha256`。提交前会用完整的封闭契约验证受限
manifest。这样既控制 core MCP discovery 大小，也不接受自由格式配置。manifest 绑定
不可变源模型、一个已验证的
热材料状态，以及精确的 Heat Transfer、Solid Mechanics、Moving Mesh、Wave Optics、
study、selection、parameter、mesh 和 expression tag，并要求调用方声明 resource 与
acceptance policy。五个哈希链 stage 依次保存 preflight、热-结构求解、状态证据、空间
frame 形变传递和精确光学重放。resume 会先验证孤立 evidence，再补写缺失 row，绝不
重复已完成 stage。验收分别保留 raw R/T/A、温度、应力、位移、能量、网格、波长、
zero-CTE、zero-temperature-rise、rollback、源完整性与 cleanup 证据，不把执行完成等同
于科学通过。最小支持路径禁止拓扑变化，并要求先通过 COMSOL 6.4 synthetic fixture 的
licensed gate，之后才可用于研究模型。

用 `model_contract.expressions.transfer_displacement_max` 声明传递 study 数据集中的网格传递位移表达式。
省略时，传递 stage 在同一数据集中读取已声明的 `displacement_max`。
表达式无法计算时，该 stage 失败。执行器不会用旧结构解代替传递结果。

### Wave Optics 超表面

使用 `wave_optics` profile，并遵循下面的有界流程：

```text
solver_status -> wave_optics_preflight -> wave_optics_reference_audit（可选） -> wave_optics_point_audit
```

`wave_optics_preflight` 只读且不求解，报告来源溯源、拓扑、周期/Floquet 选择、端口、波长关联、网格/研究元数据和明确的未知项。

`wave_optics_point_audit` 会在所有权和源文件哈希检查通过后，恰好求解一个指定波长。它写入运行中 manifest、一行经 `fsync` 的 CSV 和最终 manifest。原始证据可包括请求/实际波长、频率关联、调用方溯源的 R/T/A 与通量方向、闭合误差、损耗表达式、上方空气区域的有界场统计、网格状态以及源/配置/policy 哈希。

`wave_optics_reference_audit` 是实验性的 reference-power 工具。它创建新的溯源 clone，要求调用方精确声明材料和域，在 clone 中以无损空气替换组件材料，采样有界均匀区域，并且只有在 clone 清理得到证明后才允许参考方法证据通过。它不会修改源模型。licensed acceptance 仍须针对具体 COMSOL 版本和模型执行。

若调用方未提供版本化 validation policy，审计仅输出证据：不会宣称模型通过/失败，也不会建议开始长扫描。在有独立的入射场参考 artifact 前，S/P 标签和结构总场都会被明确限定其证据等级。

## 手册检索

`manual_search` 和 `manual_read_pages` 是正式的文档检索路径。它们使用离线 SQLite FTS5/BM25 索引、有截止时间的 worker 进程以及紧凑的来源/页码引用。此路径不会在 MCP 控制进程中导入 Torch 或 SentenceTransformer。由于安装 COMSOL 时可能不安装本地手册，这两个工具独立且默认关闭。

设置界面的“文档”页把手册关键词检索与语义检索分开。选择原始 PDF 根目录和仅含 ASCII 字符的 SQLite 目标路径后，点击“生成索引”。后台弹窗会显示阶段、当前 PDF、页数和百分比。新数据库在同目录临时文件中构建并验证，成功后才原子发布，取消或失败不会损坏原有索引。有效索引生成后再手动启用手册检索。

`semantic_docs.enabled` 是可选的隔离功能，不会干扰 COMSOL 控制。隔离 worker 向量检索只是英文诊断基线，并非多语言或生产质量声明：冻结基准中它提高了精确匹配召回率，却降低了改述/多概念召回，直接中文检索无命中，负查询没有正确弃答，长时间运行时内存也显著增长。基线模型及其索引资产已移除。替换模型需通过完整基准门槛后才能重新部署。常规工作请使用词法手册检索。

## ClientAPI 适配要点

本 Fork 已修复测试和真实 COMSOL 验证所覆盖的 clientapi 路径，包括：

- 使用 `tags()` 遍历、`feature().size()` 计数，替代直接 Model API 的索引和 `len()`。
- 物理场接口使用 `physics().create(tag, type, sdim_string)`。子 feature 使用整数实体维度。
- 使用 `getNumElem()` / `getNumVertex()` 检查网格，并显式创建 mesh sequence。
- 使用完整 study type 名称，以及 `model.java.study('std1').run()`。
- 对 Java 字符串、本地化标签、实数/NumPy/复数值和模型元数据进行 JSON 安全转换。
- 使用 `model.java.save(full_path)` 安全保存 Unicode 路径 `.mph` 文件，并正确清理 clientapi 模型克隆。
- 在正确组件中创建/复用材料和多物理场耦合。

静电场 helper 可创建 `ChargeConservation` 和材料节点，因为 COMSOL 6.3+/6.4 默认的 `fsp1` FreeSpace domain feature 使用真空介电常数，而不会使用材料的相对介电常数。

## 验证

源码 dependency/process-only 门槛、optimized-Python production guard、compileall、
hash-locked 隔离 non-editable wheel/install、licensed attached sweep/cancellation/recovery、
PID reuse 拒绝和 detach preservation receipt 均作为 release checks 维护。单元测试无副作用：
测试收集不会启动 COMSOL。integration probe 仅在显式请求时运行，并在全新的串行子进程中对
精确进程树进行清理。仓库专用测试、release fixture、gate 与 provenance 见
`development_kit/README.md`。普通 wheel/sdist 不包含该目录。

```bash
python -m pytest -q
python -m pytest -q -m integration development_kit/tests/integration
```

真实 COMSOL 验证包括：本地化 JSON 传输、Circle/Union 几何、DXF 导入、参数扫描属性、多物理场耦合、模型克隆清理、Unicode 路径保存、求解器所有权、持久化中断/重启/恢复/取消、profile 发现、Wave Optics 预检与单点审计，以及有界手册检索。

Python 3.14 licensed 平行板回归结果为 **1.8593794419540677 pF**。理论值为 **1.8593794406880002 pF**，COMSOL 精确版本为 **6.4.0.293**。

以下光谱、收敛与延续收据仅记录 0.7.8 的历史能力，不代表 0.8.0 验收，也不恢复已移除工具。

同一 COMSOL build 上的 licensed 自适应光谱验收采用中性的空气—介质—空气周期
port slab，网格为 4,798 个单元和 1,039 个顶点。通过验收的 10 行光谱得到插值
own peak **5.200823291715346 um**、**T = 0.9999455828498357**、
**FWHM = 0.4807802607560452 um** 和 **Q = 10.817464268472365**。
原始行范围为 **R = 0.000428181826928114 至 0.506857218704363**、
**T = 0.493142781295616 至 0.999571818173077**、
**max |A| = 2.985136902408465e-17**。最大功率闭合误差为
**2.103241887902518e-14**，最大波长同步误差为零。独立的 9 行边界 control
按声明扩展 window 后，以 `unresolved_at_declared_cap` 正常完成。其原始范围为
**R = 0.113752050554409 至 0.697262752330585**、
**T = 0.302737247669409 至 0.886247949445593**、
**max |A| = 1.695203805977834e-17**，最大闭合误差为
**2.903982508976606e-14**，波长同步误差为零。两次运行均保持源模型 SHA-256
不变，并释放 solver lease 和 client。

licensed convergence 验收使用三层中性 periodic-port slab mesh：单元/顶点数分别为
**2,386/560**、**4,798/1,039** 和 **13,904/2,752**。各层 own peak 为
**5.200438265718366**、**5.200823291715278**、**5.200959692754783 um**，拟合
peak T 分别为 **0.9999455861474655**、**0.9999455828498416**、
**0.9999455989864663**。governing medium-to-fine peak shift 为
**0.1364010395043668 nm**。30 行原始数据总体范围为
**R = 0.000426677111557779 至 0.506857218704365**、
**T = 0.493142781295614 至 0.999573322888467**、
**max |A| = 4.526776969362989e-17**。最大闭合误差为
**2.48772546066357e-14**，波长同步误差为零。独立 campaign 使用声明的
**0.001 nm** peak-shift tolerance，三层执行全部完成但 disposition 为 `residual`，
同时 amplitude gate 通过。两次 campaign 均保持全部 source hash 不变，结束后无
client、进程或 lease 残留。

licensed branch-continuation 验收使用两个不可变的中性 slab models，网格均为
4,798 个单元、1,039 个顶点，并在 **0 度和 10 度**对 periodic parent 与两个
ports 完成精确 incidence readback。两个 own peaks 分别为
**5.200823291715293** 和 **5.195931563688228 um**，实测 shift 为
**4.891728027065 nm**，位于调用方 guard window 内。peak T 分别为
**0.9999455828498354** 和 **0.9999448178081717**。FWHM 分别为
**0.4807802607560502** 和 **0.4836621446746728 um**。Q 分别为
**10.817464268472143** 和 **10.742894851080779**。20 行原始数据总体范围为
**R = 0.000422180032088570 至 0.512242443246136**、
**T = 0.487757556753878 至 0.999577819967919**、
**max |A| = 3.015050383793419e-17**。最大闭合误差为
**1.33935578502046e-14**，波长同步误差为零。独立的 18 行 boundary control
消耗了唯一一次声明的 expansion，并以 `unresolved_at_declared_cap` 完成。它没有
生成额外 request，也没有宣称 branch disappearance。两个 campaigns 均保持 source
hash 不变，结束后无 client、进程或 lease 残留。

波长扫描中的 `record_wavelength_controls` 记录请求值、参数 `wl` 的求值和 `c_const/ewfd.freq`，但不比较它们。若 study 不跟随模型参数，必须显式配置 study step。接受光谱前，应按声明的容差检查三者一致。作业显示 `completed` 或记录显示 `ok`，不代表波长已经验证一致。

## 环境要求与安装

- COMSOL Multiphysics 6.4。licensed acceptance 固定于 build 6.4.0.293
- Python 3.14（标准 GIL 版本，不要使用 Windows Store 版本）
- MPh `>=1.3.1,<1.5`、MCP SDK `>=2.2.0,<2.3`、`pydantic` 和 `psutil>=5.9.0`
- 已验证配置中使用 COMSOL 自带的 Java 21 runtime

Python 3.14 仍是主构建、发布与生产 lane。标准 Windows x64 Python 3.15 是
正式支持 lane（调用方决定，2026-10-26/30）：其 CI 通道运行完整
solver-free 套件与 package/install、stdio 门禁，目前固定 3.15.2 最终版
候选版。缺少 cp315 wheel 时源码构建 JPype 1.7.1、PyYAML 6.0.3 和
Pydantic 2.13.5 所需的精确 core 2.46.5。需要 MSVC/Windows SDK、
JDK/Ant 和 Rust。此通道排除实验性 `semantic-docs` extra，不代表已经
通过 licensed COMSOL 验收，也不承诺 free-threaded Python 支持。

本项目仍在积极开发，依赖范围与锁定版本可能随时调整，不保证较长的弃用过渡期。
更新现有部署前，请先在隔离环境中验证 Python、COMSOL/MPh/JPype 及所需可选
extra 的适配性。全部检查通过后，再替换当前安装并重启 MCP host。

```bash
git clone https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated.git
cd COMSOL_Multiphysics_MCP_6_4_Calibrated
python -m pip install .

# 安装后，普通用户首选设置界面：
comsol-mcp-settings

# 推荐离线手册索引；输出目录必须只含 ASCII 字符。
python -m pip install ".[manuals]"
python -m comsol_mcp.knowledge.lexical_manual build --index D:\comsol_docs_fts\manuals.sqlite3
```

如需可选的隔离语义检索（sentence-transformers，不含 ChromaDB）：

```powershell
python -m pip install ".[semantic-docs]"
# 优先使用设置界面的“文档”页；开发者/agent 自动化可编辑 JSON：
#   manuals.root = "D:/COMSOL64/Multiphysics/doc"
#   lexical_docs.enabled = true
#   lexical_docs.index_path = "D:/comsol_docs_fts/manuals.sqlite3"
#   semantic_docs.enabled = true
#   semantic_docs.root = "D:/comsol_semantic"
#   semantic_docs.model_path = "D:/comsol_semantic/models/<model>/<revision>"
```

若 Windows 用户目录含非 ASCII 字符，请避免 editable install。源码变化后运行 `python -m pip install . --no-deps`，并重启 MCP host。服务器不会热加载 `comsol_mcp/tools/`。

MCP 客户端配置示例：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "comsol": {
      "type": "local",
      "command": ["D:\\path\\to\\python-env\\Scripts\\comsol-mcp.exe"],
      "environment": {
        "COMSOL_MCP_SETTINGS_PATH": "D:\\path\\to\\COMSOL_Multiphysics_MCP\\settings.json"
      }
    }
  }
}
```

普通用户在设置界面选择 `core` 即使用紧凑默认 profile。开发者和 agent 可在 JSON 中设置
等价的 `profile.name`。客户端示例见
`config/claude-code-mcp.example.json`、`config/codex-mcp.example.toml`、
`config/hermes-mcp.example.yaml` 和 `config/opencode-mcp.example.json`。

## 与上游 Fork 的区别

这是面向 COMSOL 6.4.0.293 standalone/clientapi 的兼容性和可靠性 Fork，而非上游项目的通用替代品。其他 COMSOL build 在独立验收前均为 unknown。它保留上游项目的基础能力，但为 agent 驱动的 COMSOL 工作流提供了更窄、更安全的执行界面。

| 方面 | 上游定位 | 本 Fork |
| --- | --- | --- |
| COMSOL API 目标 | 假定直接使用 `com.comsol.model.Model` API。 | 适配 MPh 1.3.1 standalone 的 `model.java` clientapi 包装层，包括不同的方法重载、tag、列表和 Java 字符串传输。 |
| 工具界面 | 默认提供较宽的功能发现面。 | 默认采用紧凑 `core`。较大的构建和兼容界面须显式选择 profile。 |
| 求解器并发 | 没有同主机所有权协议。 | 通过进程感知租约、外部客户端检测、状态、预检和过期租约恢复来防止冲突。不会终止不属于本服务器的进程。 |
| 长任务 | 以交互式/当前进程工作流为主。 | 独立扫描和 validation matrix，保留不可变规格、持久化证据行、校验恢复和已验证的取消清理。 |
| Wave Optics | 只有通用工具。 | 提供周期性超表面专用的预检和单点证据审计，原始证据与调用方 policy 分离。 |
| 手册检索 | 无有界手册检索。 | 提供默认关闭、可独立启用的有界词法手册检索。实验性语义检索被隔离且明确未晋级。旧式进程内 ChromaDB 路径已移除。 |
| Windows 路径 | 不特别保证 Unicode 保存路径。 | 通过 clientapi Java 保存 Unicode `.mph`。原生/持久化 runtime 和索引使用 ASCII-only 根目录。 |

若在 MPh standalone 下使用上游工具时遇到 `No matching overloads`、`Operation_cannot_be_created_in_this_context` 或 client-list 索引错误，请使用本 Fork。只有确实需要宽泛旧接口兼容时才选择 `full`。

## 许可证

本仓库采用 [MIT License](LICENSE)。COMSOL、授权手册、第三方模型、论文和数据集
不因本仓库许可证而被重新授权。0.8.0 移除外部 `mmapy` 执行路径。
历史可选 wheel 采用 GPL-3.0-or-later，本包不分发它。
感谢 Krister Svanberg 提出 MMA/GCMMA，以及 Arjen Deetman 提供 Python 实现。

## 相关论文与引用

下方论文介绍的是上游 COMSOL-MCP 项目，并非本维护型 Fork。此处仅用于
致谢和标明来源：

> Naiyin Zhang and Junchao Wang, “COMSOL-MCP: An open-source model context
> protocol interface for AI-assisted multiphysics simulation,” *Neurocomputing*,
> vol. 703, article 134481, 2026.

出版商页面：[ScienceDirect](https://linkinghub.elsevier.com/retrieve/pii/S0925231226018795)

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

上述论文中的方法、架构、功能和评估均指向上游项目。本 Fork 请通过 GitHub 的
**Cite this repository** 菜单引用 [`CITATION.cff`](CITATION.cff)，并在可复现性声明中
注明本 Fork 的精确 release 或 commit、仓库链接和访问日期。

感谢 Naiyin Zhang 与 Junchao Wang 创建 COMSOL-MCP、发表相关论文，并在 upstream
[commit `99172f8`](https://github.com/wjc9011/COMSOL_Multiphysics_MCP/commit/99172f8f43c6753c2442c406cd5c6055ea8c5bef)
中贡献引用指引。本维护型 fork 已根据自身仓库与 release evidence 边界进行了适配。
