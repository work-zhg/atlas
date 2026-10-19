"""server 侧的上游协议对接：与新 bridge（atlas.host.v1）通信（代码设计 §11）。

kind="acp" 的 agent 都经这里执行：HostRuntime 连会话 Pod 里的 bridge，translate 把 ACP 内容
翻成平台事件（server 里唯一允许导入 atlas_acp 的地方）。
"""
