"""模型网关：所有模型调用的入口（agent-platform/doc/design/model-gateway-design.html）。

八个领域见 c4-model/modelgateway/code-gateway-domains.html。这个包的 __init__ 不引入任何领域，
供应商包只依赖 kernel。
"""
