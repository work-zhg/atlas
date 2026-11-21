"""LLM 的对接层（server 一侧）。

model_platform   模型管理平台（agent-platform/doc/design/model-design.html）：智能体的客户端 Key、
                 可用模型、调用端点（模型网关 + Key）、Run 开始的检查

★ 模型对象怎么造（ModelSpec → 聊天模型、兼容补丁、拒绝的说明）在 atlas_engine.runtime：
  框架类型只在 engine 里出现。
"""
