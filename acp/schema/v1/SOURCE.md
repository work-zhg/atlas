# ACP v1 schema（第三方，原样收录）

- 来源：https://github.com/agentclientprotocol/agent-client-protocol
- 标签：`schema-v1.23.0`（2026-09-18 发布）
- 路径：`schema/v1/schema.json`、`schema/v1/meta.json`
- 许可证：Apache-2.0（版权归原作者所有）
- SHA-256：
  - `schema.json`：`3c17bd6385d90cf672d8a661fddc359d73422cf8b8ce6865213d25cfd4c0eca7`
  - `meta.json`：`061edb6efa8fb2aa2792459a86ec7268de5fe665bba48b2ffe7939df01481f88`

**不要手改。** 升级 ACP 时：换标签重新下载这两个文件、更新上面的标签与校验和，
再运行 `uv run python acp/scripts/generate_v1.py` 重新生成 `atlas_acp/v1/_generated.py`。
