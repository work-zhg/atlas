"""领域词汇层 —— run 是什么，用什么词说。

spec           AgentSpec：一次 run 的配置词汇（schemas 翻译进来、build 装配、
               runner 读 limits），含 spec_for_subagent 派生
events         TraceEvent / EventType：事件流契约，server↔web 的公共语言
translator     LangChain 消息 → 文本/用量/工具调用 的中立表示
tool_registry  内置工具目录：名字、可用性、中间件工厂的唯一来源

★ 纯计算层：禁碰 sqlalchemy / fastapi / redis（import-linter contract +
  test_engine_purity 双守卫）—— 「run 一轮」必须始终能用假模型脱离
  基础设施测试。IO 归 services / repositories / providers。
"""
