# CFD 默认求解器配方（CFDAutoSolverSuggestion 反编译蒸馏，6.3）

> 来源：com.comsol.cfd.util.solversuggestion.CFDAutoSolverSuggestion（1148 行全可读）
> 用途：收敛排障时对照"官方这时候怎么配"；也解释了自动求解器为何长这样

## 时间步进（瞬态）
- rtol = **0.005**（比默认 0.01/0.001 更松）；atolglobalfactor=0.05
- BDF **maxorder=2** + **非线性控制器 ON** + predictor=constant
- 部分接口（aa/ab 标志）用 **genalpha**（rhoinf=0.5）+ 最大步长=网格尺度表达式；否则 BDF
- Backward Euler 初始步长分数 bwinitstepfrac=0.01

## 非线性：全耦合 fullycoupled
- 层流单相（t 标志）：auto 阻尼 initstep=0.01 / minstep=1e-6 / **maxiter=50**
- RANS 单相：const 阻尼 **damp=0.8**，maxiter=**150**（3D:300）
- 带 RANS 模型时 maxiter 提到 200-400

## 非线性：伪时间推进（cflcmp，全耦合/分离都有）
- 初始 CFL=**5** → 目标 CFL=**10000**（PID 调节增长）
- PID 参数：**P=0.65 / D=0.15 / I=0.15**；目标误差估计 cfltol=**0.1**
- **Anderson 加速**：CFL>**9000** 后启用（阈值因子 1）

## 分离解法 segregated
- 单相：maxsegiter=200（3D:400）
- **两相流（B 标志）**：maxsegiter=**10**（更少但更贵的步）+ ntolfact=**0.5** + **分离 Anderson（segaacc：维数 5、混合 0.9、延迟 1）**
- 两相分离子步：subdamp=**0.8**、Jacobian 更新 **once**（2D 层流例外 onevery）
- 两相也有 subCFL 伪推进（同 PID/CFL 参数，segcflaa 同 9000 阈值）

## 稳态
- stol=1e-6（仅特定接口组合返回非空——多数 CFD 稳态也走伪时间配方）

## 线性求解器
- 委托 VarGrpNLSettings（TransportAutoSolverUtil 组装，按变量组给预条件子/迭代法）

## 对你 EulerEuler 的直接启示
- 你的两相模型默认就该是"分离+segaacc+subCFL 伪推进"配方；若手动改过 fullycoupled 或关掉 segaacc，收敛行为会显著偏离官方设计点
- 收敛卡住时对照三个旋钮：maxsegiter(10)、subdamp(0.8)、初始 CFL(5)
