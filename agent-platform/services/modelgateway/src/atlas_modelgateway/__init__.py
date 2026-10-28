"""模型管理平台（agent-platform/doc/design/model-design.html）：模型网关用 LiteLLM，平台只管配置。

    模型       登记（只落基本信息，地址、凭据、规格价格交给 LiteLLM）；公有 / 私有；
               和 LiteLLM 里直接配置的模型合并展示，可以纳入平台
    授权       私有模型授权给用户、部门（用户中心数据权限，数据编码 Model）
    API Key    用户 Key（本人申请）、客户端 Key（应用创建，管理员分配模型）；
               有效模型同步到 LiteLLM，定期对账
    内部接口   应用按客户端类型 + 客户端编码创建、获取 Key 和可用模型

★ 不在调用路径上：应用拿着 Key 直连 LiteLLM；调用日志、花费、预算限流都在 LiteLLM。
★ 包名、库名、用户中心应用编码沿用自研网关时期的 modelgateway / model-gateway：已有的授权不用迁移。
"""
