"""沙箱组件（c4-model/agent/sandbox/code-sandbox.html）。

三层七个领域，依赖只往下：

    业务      exec（命令执行）· native（原生沙箱）· session_pod（会话 Pod）
    通用供给  provisioning：按人管理沙箱，一个业务至多绑一个沙箱
    基座      base.lifecycle（模板、建删查、生命周期日志）
              · base.execution（execd：命令、文件、活动）
              · base.network（出站策略、Credential Vault）

上层只拿 sandbox_uuid，不碰 OpenSandbox 的 id、地址和令牌。

★ 包本身不导入子模块：用到哪一层就从哪一层导入，免得只读值对象时把数据库和 HTTP 客户端拖进来。
"""
