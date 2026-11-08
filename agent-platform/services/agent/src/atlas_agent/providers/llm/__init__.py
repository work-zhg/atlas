"""LLM 的对接层（server 一侧）。

gateway          GET /v1/models 目录拉取（没接模型管理平台时的可用性巡检）
model_platform   模型管理平台（agent-platform/doc/design/model-design.html）：智能体的客户端 Key、
                 可用模型、调用端点（LiteLLM + Key）、Run 开始的检查

★ 模型对象怎么造（ModelSpec → 聊天模型、网关兼容补丁、拒绝的说明）在 atlas_engine.runtime：
  框架类型只在 engine 里出现。思考、档位、温度等参数约束按 contracts/spec.py 里的规则。
"""
