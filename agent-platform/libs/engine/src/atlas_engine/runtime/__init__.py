"""原生运行时：把一段跑成 TraceEvent 流（c4-model/agent/native-runtime/code-native-runtime.html）。

server 用它只有两步：先把这一段的能力收成 Capabilities（IO 都在 server），再交给这里
**装配**（assemble）、**驱动**（runner.run）。

    assemble       spec + 能力 → 图。装哪些中间件、什么顺序、系统提示词怎么拼，只在这里定
    runner         驱动图，产出 TraceEvent 流；永不向调用方抛异常（失败也是事件）
    context        上下文流水线（槽位 ④）按 spec 和端口拼出来
    summarize      压缩用的摘要器
    tools          server 交来的 ToolSpec → 框架的工具
    messages       LangChain 消息 ⇄ 存储格式（存储格式本身在 contracts.messages）
    models         ModelSpec → 聊天模型、单轮调用；compat 是对网关怪癖的补丁
    observe        模型调用的观察者（遥测挂在这里，不碰框架的回调）
    failures       模型平台的拒绝 → 给用户看的说明
    translator     消息内容 → 文本 / 用量 / 工具调用 的中立表示
    tool_registry  内置工具目录：名字、可用性、中间件工厂的唯一来源
    skill_events   技能加载的事件

★ 装配入口是 runtime.assemble.assemble（不在包级转出：函数名和子模块同名，转出会遮住模块）。
★ 这是 engine 里唯一面向 langchain / langgraph 的一层：server 不 import 框架，也不 import
  kernel，只认 contracts 和这里（import-linter）。kernel 里 server 要用的几样（自动模式判断器的
  数据、提问的答案解析）由这里转出。
★ 纯计算层：不碰 DB、Redis、HTTP 框架、部署配置 —— 「run 一轮」必须能用假模型脱离
  基础设施测试。IO 归 server。
"""

from atlas_engine.kernel.middleware.ask_user import resolve_answers
from atlas_engine.kernel.middleware.auto_judge import Instruction, JudgeSetup
from atlas_engine.runtime.assemble import Capabilities, JudgePort
from atlas_engine.runtime.context import ContextLimits, ContextPorts
from atlas_engine.runtime.messages import Transcript
from atlas_engine.runtime.models import Completion, build_chat_model, complete
from atlas_engine.runtime.summarize import Summarizer

__all__ = [
    "Capabilities",
    "Completion",
    "ContextLimits",
    "ContextPorts",
    "Instruction",
    "JudgePort",
    "JudgeSetup",
    "Summarizer",
    "Transcript",
    "build_chat_model",
    "complete",
    "resolve_answers",
]
