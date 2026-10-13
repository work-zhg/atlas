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

#: 本 run 所在的会话（子 run 是子会话）。gen_ai.conversation.id 统一用**根会话**，
#: 这里保留真实所在，不丢失「它在子会话里」这个事实。
THREAD_ID = "atlas.thread_id"
#: 委派树：子 run 指回父 run（langfuse-integration-design §4 P3）
PARENT_RUN_ID = "atlas.parent_run_id"
#: 这一段是不是挂起后的续跑（一个 run 可以分多段执行，同属一条 trace）
RESUMED = "atlas.resumed"

# ── 整轮累计用量（只给人看，不参与计费）──────────────────────────────
#
# ★ 根 span 上的整轮累计用量**不能**用 gen_ai.usage.*：Langfuse 可能把「带模型名与
#   用量的 span」当成一次模型调用，Native 的费用就会被算两遍（设计 §8.2 的规则：
#   只有 Generation 带 gen_ai.usage.*）。
ATLAS_USAGE_INPUT = "atlas.usage.input_tokens"
ATLAS_USAGE_OUTPUT = "atlas.usage.output_tokens"
ATLAS_USAGE_CACHE_READ = "atlas.usage.cache_read_tokens"
ATLAS_USAGE_CACHE_CREATION = "atlas.usage.cache_creation_tokens"
ATLAS_USAGE_REASONING = "atlas.usage.reasoning_tokens"
#: 合成 Generation 的用量口径：turn = 一整轮（ACP 看不到 CLI 内部的单次调用）
USAGE_SCOPE = "atlas.usage.scope"

# ── 内容类属性（§08，默认不采集，见 telemetry/content.py 的档位）──────────
INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"

# ── 遥测后端的专有属性：Langfuse ───────────────────────────────────────
#
# ★ 后端专有的属性名**只在这里出现**（设计 §5.6-5）。Langfuse 升级、或换后端，
#   只改这一段。其余模块只引用常量，不写字面量。
# ★ 只用于 OTel 标准里没有对应写法的概念：会话、用户、trace 名称 / 标签 / 元数据 /
#   输入输出、observation 类型。模型名、用量、工具名仍走 gen_ai.*（标准优先）。
LF_SESSION_ID = "langfuse.session.id"
LF_USER_ID = "langfuse.user.id"
LF_ENVIRONMENT = "langfuse.environment"
LF_TRACE_NAME = "langfuse.trace.name"
LF_TRACE_TAGS = "langfuse.trace.tags"
LF_TRACE_INPUT = "langfuse.trace.input"
LF_TRACE_OUTPUT = "langfuse.trace.output"
#: 前缀：langfuse.trace.metadata.<key>
LF_TRACE_METADATA = "langfuse.trace.metadata."
LF_OBSERVATION_TYPE = "langfuse.observation.type"
LF_OBSERVATION_INPUT = "langfuse.observation.input"
LF_OBSERVATION_OUTPUT = "langfuse.observation.output"

#: langfuse.observation.type 的取值。★ 显式指定，不靠后端按「有没有模型名 / 用量」推断。
OBS_AGENT = "agent"
OBS_GENERATION = "generation"
OBS_TOOL = "tool"
#: 瞬时事件。★ Langfuse 不展示 OTel 的 span event（V8 实测：既不成为 observation，也不进
#: 元数据），所以瞬时事件另发一个零时长的 event 类型 span；根 span 上的 event 照留给 Jaeger。
OBS_EVENT = "event"

#: 全部内容类属性。Collector 发往 Jaeger 的管道要删掉的就是这些（deploy/local/otel/）。
CONTENT_ATTRIBUTES = (
    INPUT_MESSAGES,
    OUTPUT_MESSAGES,
    LF_TRACE_INPUT,
    LF_TRACE_OUTPUT,
    LF_OBSERVATION_INPUT,
    LF_OBSERVATION_OUTPUT,
)
