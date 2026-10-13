"""Agent 工程能力评测（doc/agent-eval-design.html）。

固定模型、换 harness：同一批任务、同一个模型，比较不同被测方 / 配置做成了多少、花了多少、栽在哪。

    tasks.py      统一任务模型与加载（bench/agent-tasks/<套件>/<任务>/）
    platforms/    被测方适配器（P1：Atlas 原生 / ACP，只用公开 API）
    verify.py     校验：在隔离容器里对最终工作区跑任务自带的校验脚本
    trajectory.py 归档轨迹 → 统一轨迹 → 过程指标
    runner.py     variant × task × trial 矩阵、预算、审批策略、断点续跑
    report.py     成功率 / pass^k / 成本 / 失败构成 / 配对比较
"""
