"""LLM 网关的对接层 —— 换网关（LiteLLM → Higress）只改这里。

factory   ModelSpec → BaseChatModel（base_url / api_key / 兼容补丁）
compat    对当前网关实测怪癖的补丁（换网关要整体重测）
gateway   GET /v1/models 目录拉取

★ spec.py 里那批「网关实测 400」的参数约束（effort / adaptive thinking /
  temperature）也是对着当前网关测的 —— 换网关时与 compat 一起重跑实测，
  权威结果落 model_catalog 表。
"""
