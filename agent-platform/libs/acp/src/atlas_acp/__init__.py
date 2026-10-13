"""ACP（Agent Client Protocol）的模型与辅助（代码设计 §3）。

    v1    由 ACP 官方 schema 生成的模型（_generated.py · methods.py），外加少量手写辅助：
          AgentCaps（能力位解读）、update_kind / tool_status（从原始 update 读控制信号）

使用者：bridge（ACP client 与控制信号）、server 的 atlas_server.host.translate（内容翻译）。
上游协议 atlas_host 不得导入本包 —— 它运送 ACP 内容，但不理解它（Bridge 设计 §3 D2）。

零重依赖（仅 pydantic）：本包会装进 Pod 镜像，多一个依赖就是每个会话 Pod 多一层字节与一个
CVE 面。守卫见 import-linter 契约与 acp/tests/test_package_boundary.py 的子进程探测。
"""
