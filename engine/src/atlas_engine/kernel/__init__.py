"""kernel —— Atlas 的 agent 装配层：图 + 全部中间件。

它不发明执行循环 —— 循环来自 langchain.agents；kernel 提供的是一套可插拔的
能力层（文件系统 · 子智能体 · 技能 · 压缩 · 审批 · 限额），以中间件的形式叠加
到那个循环上，由 graph.py 按固定顺序装配成一张图。

★ kernel 是**内部层，不是公共契约**。31 模块 / 9765 行仍在持续重写与吸收中，
  模块路径随时可能变 —— server 要的协议与结果类型一律走 atlas_engine.contracts
  （那里的 re-export 才是承诺面）。守卫见 tests/test_engine_purity.py。

★ middleware/summarization.py 是唯一没吃透的大块，动它之前先读这段。
  2174 行、占 kernel 22%，是全包里**唯一基本保持引入原样**的模块 —— 其余要么
  自研、要么已重写或深度改造过，唯独它的行为没有人真正走通过一遍。
  而压缩恰恰是 bug 最隐蔽的地方：token 计数偏差、切分边界算错、
  tool_call 与 tool_result 配对被切断 —— 这些都不报错，表现是「模型莫名其妙
  忘事」，在事件流上看不出任何异常（与 §13.2「不静默降级」同款的失败形态）。
  它排在重写队列的前面；在那之前，改这个文件的人要自己补覆盖测试。

★ 这里刻意**不导出 __version__**。它原先来自 _version.py —— 那个模块 217 行
  全部为了算一个字符串喂给 LangSmith 的 `lc_versions` 元数据（含一整套
  editable 安装探测）。而本仓不用 LangSmith（见 middleware/subagents.py），
  链路追踪走的是 server 侧的 OTel。

  去掉之后版本号只有一个来源：engine/pyproject.toml。全仓没有任何代码读
  kernel.__version__，留一个永远对不上的副本只会制造「改哪个才算数」的疑问。
"""

from atlas_engine.kernel.graph import (
    DeepAgentState,
    create_deep_agent,
)
from atlas_engine.kernel.middleware.filesystem import (
    FilesystemMiddleware,
    FilesystemPermission,
    FsToolName,
)
from atlas_engine.kernel.middleware.subagents import (
    SubAgent,
    SubAgentMiddleware,
)
from atlas_engine.kernel.profiles.harness.harness_profiles import (
    HarnessProfile,
    HarnessProfileConfig,
    register_harness_profile,
)

__all__ = [
    "DeepAgentState",
    "FilesystemMiddleware",
    "FilesystemPermission",
    "FsToolName",
    "HarnessProfile",
    "HarnessProfileConfig",
    "SubAgent",
    "SubAgentMiddleware",
    "create_deep_agent",
    "register_harness_profile",
]
