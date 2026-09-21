"""OTel GenAI 语义约定的属性名与取值（可观测性设计 §03）。

★ 用标准约定，不自定义字段。只有四层共用同一套约定，Server / 网关 /
  Bridge / CLI 的 span 才能拼成一条链路，也才能被 Langfuse 这类现成后端
  直接识别 —— 自定义就意味着每个后端都要写一份适配。

★ 为什么集中成一个模块：GenAI 约定目前仍在 `_incubating` 命名空间下，
  属性名会随版本变化（§11 第 5 项）。散在十来个调用点里的话，升级要靠
  grep 字符串；集中在这里，改一处。

属性名取自 opentelemetry-semantic-conventions 0.65b0（设计文档已核对过
实测来源，此处照抄，不凭记忆书写）。
"""

from __future__ import annotations

# ── 操作与模型 ────────────────────────────────────────────────────────
OPERATION_NAME = "gen_ai.operation.name"
PROVIDER_NAME = "gen_ai.provider.name"
REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
FINISH_REASONS = "gen_ai.response.finish_reasons"
#: 映射我们的会话（thread）—— §06 规定 session 维度贯穿全部四层。
CONVERSATION_ID = "gen_ai.conversation.id"

# ── 用量 ──────────────────────────────────────────────────────────────
#
# ★ 缓存与推理 token 是**独立字段**，计价也不同。把它们并进 input_tokens
#   会让成本核算系统性偏离，长会话尤其明显（§03 的红线）。
#   translator.normalize_usage 的输出本来就是分开的，这里一一对应即可。
USAGE_INPUT = "gen_ai.usage.input_tokens"
USAGE_OUTPUT = "gen_ai.usage.output_tokens"
USAGE_CACHE_READ = "gen_ai.usage.cache_read.input_tokens"
USAGE_CACHE_CREATION = "gen_ai.usage.cache_creation.input_tokens"
USAGE_REASONING = "gen_ai.usage.reasoning.output_tokens"

# ── Agent 与工具 ──────────────────────────────────────────────────────
AGENT_ID = "gen_ai.agent.id"
AGENT_NAME = "gen_ai.agent.name"
TOOL_NAME = "gen_ai.tool.name"
TOOL_CALL_ID = "gen_ai.tool.call.id"

# ── operation.name 的取值 ─────────────────────────────────────────────
OP_INVOKE_AGENT = "invoke_agent"
OP_CHAT = "chat"
OP_EXECUTE_TOOL = "execute_tool"

# ── 平台自有维度（§06：这几个正是会话管理里已有的对象主键，不另造）──────
#
# ★ run_id 没有对应的标准属性 —— GenAI 约定只到 conversation 一级。
#   §06 明确它走自定义属性，所以这里是**有意**偏离标准的唯一一处。
RUN_ID = "atlas.run_id"
AGENT_KIND = "atlas.agent.kind"  # native | acp
#: 委派出去的子 run。父 span 上带它，好从父跳到子的 trace。
SUBAGENT_NAME = "atlas.subagent.name"

# ── 本轮不采集的内容类属性（§08，默认关）─────────────────────────────
#
# 列在这里是为了让「我们知道有这些、且刻意没用」变成可 grep 的事实，
# 而不是一个看不见的遗漏。开启内容采集时才会用到它们。
INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"
