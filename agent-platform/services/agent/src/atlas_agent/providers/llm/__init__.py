"""LLM 的对接层 —— 换网关只改这里。

factory          ModelSpec → BaseChatModel（base_url / api_key / 请求头 / 兼容补丁）
compat           对当前网关实测怪癖的补丁（换网关要整体重测）
gateway          GET /v1/models 目录拉取（没接模型管理平台时的可用性巡检）
model_platform   模型管理平台（agent-platform/doc/design/model-design.html）：智能体的客户端 Key、
                 可用模型、调用端点（LiteLLM + Key）、Run 开始的检查
failures         LiteLLM 的拒绝 → 给用户看的说明（纯函数，runner 用）

★ 思考、档位、温度等参数约束一律按 spec.py 里的规则和 model_catalog 表（平台不登记能力）。
"""
