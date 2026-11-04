"""沙箱 · 基座 · 沙箱内执行（c4-model/agent/sandbox/code-sandbox-base-execution.html）。

    ports.py    SandboxExec、FileSession：调用方只传 sandbox_uuid
    execd.py    ExecdChannel、ExecdFileSession：execd 的协议细节都在这里翻译
    ledger.py   ActivityLedger：按沙箱记运行中的命令和最近活动（Redis）
    values.py   CommandSpec、LogChunk、CommandStatus、ResourceUsage、SandboxActivity 与几种错误

★ 只经生命周期的 access 拿地址和令牌：令牌只在这里和生命周期之间流转，都在基座里。
★ 不管重放、重连次数、卡死判定：那些是命令执行领域的策略。
"""
