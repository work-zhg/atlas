"""会话 Pod 里的 bridge：ACP client + 上游协议（atlas.host.v1）服务端（代码设计 §5）。

    agent/      对下：agent 进程与 ACP（AgentProcess · AcpClient · AgentSupervisor）
    session/    核心：会话与轮次的状态机、计时、取消、权限（HostSession · Turn · PermissionBroker）
    upstream/   对上：WebSocket、握手鉴权、序号与补发（UpstreamServer · Outbox）
    app.py      装配与启动顺序；入口是 ``python -m atlas_bridge``

不认识 atlas_server：与 server 的唯一接口是上游协议（Bridge 设计 §3.3）。
"""
