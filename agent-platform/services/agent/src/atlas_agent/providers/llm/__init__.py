"""LLM 网关的对接层 —— 换网关只改这里。

factory        ModelSpec → BaseChatModel（base_url / api_key / 请求头 / 兼容补丁）
compat         对当前网关实测怪癖的补丁（换网关要整体重测）
gateway        GET /v1/models 目录拉取（没接模型网关时的可用性巡检）
modelgateway   模型网关（doc/model-design.html）：模型目录、主体能用什么、调用端点

★ 接了模型网关时，参数约束（effort / adaptive thinking / temperature）来自模型目录
  （ModelSpec.caps），由网关的能力探测得出；没接时照旧用 spec.py 里那批「网关实测 400」
  的写死规则和 model_catalog 表。
"""
