# atlas-bench：RAG 评测

设计：[doc/rag-eval-design.html](../doc/rag-eval-design.html)。只通过各平台的公开 HTTP 接口评测，不 import 任何 atlas 包。

## 准备

`.env` 里需要：

| 变量 | 说明 |
| --- | --- |
| `ATLAS_BENCH_UID` | 评测身份：用户中心里的普通用户 `atlas-bench`（禁止登录），授「评测」角色（`kb:create` · `agent:create` · `chat:use`）。请求头 `X-Uid` 写它、`X-Caller-App: bench`（内网免令牌，doc/identity-propagation-design.html §12） |
| `ATLAS_BENCH_TENANT_ID` | 它所在的租户；单租户部署可不填（用户中心按 uid 推） |
| `ATLAS_BENCH_LLM_API_KEY` | 生成与裁判模型的密钥（OpenAI 兼容接口）；不设则依次用 `MEMORY_LLM_API_KEY`、`LITELLM_KEY` |
| `ATLAS_BENCH_LLM_BASE_URL` | 默认 `https://api.deepseek.com/v1` |
| `ATLAS_BENCH_ATLAS_KB_URL` | 默认 `http://127.0.0.1:18050`：内网接口不对宿主暴露，先 `kubectl -n atlas-system port-forward svc/atlas-knowledge 18050:8050`（`make smoke` / `make regress` 自动开关，见 `deploy/local/forward.sh`） |

## 使用

```bash
uv run python -m atlas_bench datasets                          # 可用数据集
uv run python -m atlas_bench run agent-platform/bench/experiments/demo.yaml   # 入库 → 检索 → 生成 → 判分 → 报告
uv run python -m atlas_bench run agent-platform/bench/experiments/demo.yaml --limit 5          # 只跑前 5 题
uv run python -m atlas_bench run agent-platform/bench/experiments/demo.yaml --resume <运行目录>  # 断点续跑
uv run python -m atlas_bench score <运行目录>                   # 离线重新判分（不重跑平台）
uv run python -m atlas_bench cleanup                           # 删除评测在 Atlas 上建的库（bench: 前缀）
```

结果在 `var/bench/runs/<时间>-<实验名>/`：`raw/`（原始结果）、`scores/`（逐题分数）、`summary.json`、`report.md` / `report.html`。
同一（语料, 入库配置）只入库一次，后续运行直接复用；裁判与 RAGAS 结果缓存在 `var/bench/cache.sqlite`。

## 数据集格式

`<数据集>/manifest.yaml` + 文档 + `questions.jsonl`，每行一题：

```json
{"id": "d06", "split": "test", "tags": ["换说法", "表格"], "question": "去上海出差，酒店一晚最多能报多少钱？",
 "reference": "上海属于一线城市，出差住宿每晚上限 600 元。",
 "evidence": [{"doc_id": "差旅报销制度.md", "quote": "| 一线城市 | 北京、上海、广州、深圳 | 600 元 |"}],
 "unanswerable": false}
```

- `evidence.quote` 必须逐字出现在文档里（测试会校验）；用原文片段判断是否命中，不依赖平台的分段 ID。
- `reference` 写成完整的句子：RAGAS 按陈述判断，过短的参考答案（如只写「每周三」）会让判分失真。
- 仓库自带的 `demo-zh` 是走通流程用的样例集（虚构公司制度，20 题）；公开评测集由导入器转成同一格式后放在 `var/bench/datasets/`。

## 智能体评测（doc/agent-eval-design.html）

固定模型、换 harness：同一批任务、同一个模型，比较不同智能体配置（原生 / ACP、开关某项能力）做成了多少、花了多少、栽在哪。

```bash
uv run python -m atlas_bench agent tasks                                   # 任务套件
uv run python -m atlas_bench agent run agent-platform/bench/experiments/agent-demo.yaml   # 建智能体 → 每题新会话 → 校验 → 报告
uv run python -m atlas_bench agent run agent-platform/bench/experiments/agent-demo.yaml --limit 2      # 只跑前 2 题
uv run python -m atlas_bench agent run agent-platform/bench/experiments/agent-demo.yaml --resume <运行目录>
uv run python -m atlas_bench agent report <运行目录>                         # 重新汇总（不重跑）
uv run python -m atlas_bench agent cleanup                                 # 删除评测建的智能体（bench- 前缀）
```

- 身份：同一个评测用户 `ATLAS_BENCH_UID`，需要 `agent:create` + `chat:use`；地址 `ATLAS_BENCH_ATLAS_API_URL`（默认 `http://127.0.0.1:18000`，`kubectl -n atlas-system port-forward svc/atlas-server 18000:8000`）。
- 任务格式：`agent-platform/bench/agent-tasks/<套件>/<任务>/` 下 `task.yaml`（指令、类别、预算）、`files/`（初始工作区，经 `PUT /v1/threads/{id}/files` 上传）、
  `verifier/verify.py`（校验，Agent 看不到）、`solution/`（参考答案，只给单测用）。
- 校验：最终工作区拷进 `python:3.12-slim` 容器（无网络）跑校验脚本；退出码 0 = 通过，最后一行 `SCORE=x` 给部分得分。需要本机 docker。
- 审批：勾了 Shell 的智能体每条命令都要审批，评测按 `approve_all` 自动批准（次数与等待计入指标）。
- 结果：`var/bench/runs/<时间>-<实验名>/` 下 `trials/<配置>.jsonl`（逐次结果）、`raw/`（原始轨迹）、`summary.json`、`report.md` / `report.html`。
- 自带的 `demo` 是走通流程用的 7 题（代码修复、重构、数据处理、多步小工具）；Terminal-Bench 等公开任务集的导入器是下一步。

## 已实现 / 待做

已实现：统一数据格式、Atlas 适配器（入库复用、多种检索方式共用一个库）、断点续跑、出处匹配 + ranx 检索指标、
统一生成、CRAG 计分与拒答（裁判模型）、RAGAS 忠实度与事实正确性、错题归因、bootstrap 置信区间与配对比较、报告。

待做（见设计 §16）：公开评测集导入器（CRUD-RAG、MultiHop-RAG、C-MTEB、RGB…）、RAGAS 提示词中文化与裁判校准、
其余 RAGAS 指标、Dify / RAGFlow 适配器、两两盲评、人工复核工具、私有文档出题、K8s Job。
