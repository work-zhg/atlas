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
uv run python -m atlas_bench import cmteb EcomRetrieval        # 导入 C-MTEB 检索子集到 var/bench/datasets/cmteb-ecom
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
- 按段落标注的检索集（C-MTEB）清单写 `evidence_match: passage`：出处是整段语料，检索结果归一化后与它相等才算命中
  （默认 `quote` 按包含 / 拼接 / 模糊判断，短标题会被别的段包含而误命中）。
- C-MTEB（`importers/cmteb.py`）：语料、查询全量，nDCG@10 可对照公开榜单。每个文件打包 1000 段、段间空一行，
  `experiments/cmteb.yaml` 用通用分段按 `\n\n` 切 → 一段一个分段；超过 4000 字的段截断。只评检索。

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
- 校验（`verifier.mode`）：
  - `container`（默认）：最终工作区拷进 `python:3.12-slim` 容器（无网络）跑校验脚本；退出码 0 = 通过，最后一行 `SCORE=x` 给部分得分。需要本机 docker。
  - `sandbox`：在 Agent 用过的沙箱里原地校验（要看环境状态的题）。Agent 结束后才把 `verifier/` 解到沙箱的 `dest`（默认 `/tests`），
    在 `cwd` 里执行 `command`，再读 `reward_file`（默认 `/logs/verifier/reward.txt`，1 = 通过，同 Harbor）。
    经 `POST /v1/threads/{id}/sandbox/exec`，只对**正在运行的评测档沙箱**生效；Agent 一条命令没执行过（没有沙箱）记不通过。
- 审批：勾了 Shell 的智能体每条命令都要审批，评测按 `approve_all` 自动批准（次数与等待计入指标）。
- 提问（`ask_user_question`，doc/ask-user-question-design.html）：不按审批策略处理。任务带 `user.brief`（用户心里的完整需求，
  Agent 看不到）时由模拟用户作答 —— 实验配置的 `user_model` 扮演用户，只依据 brief 回答；没带的任务按推荐项回答。
  模拟用户出错时退回按推荐，记在 `simulated_user_errors`。
- 需求澄清：`clarify` 套件是 3 对「一句话的模糊题 / 写清楚的清晰题」（tag `ambiguous` / `clear`），
  `experiments/clarify.yaml` 比较「能提问 / 能提问 + 澄清清单 / 不能提问」；报告的「需求澄清」一节给出
  模糊题的提问率与成功率、清晰题的误问率与成功率。
- 结果：`var/bench/runs/<时间>-<实验名>/` 下 `trials/<配置>.jsonl`（逐次结果）、`raw/`（原始轨迹）、`summary.json`、`report.md` / `report.html`。
- 自带的 `demo` 是走通流程用的 7 题（代码修复、重构、数据处理、多步小工具）。

### Terminal-Bench 2

```bash
uv run python -m atlas_bench import tbench                  # 第一批 15 题 → var/bench/agent-tasks/tbench（另有 images.txt、SOURCE.yaml）
uv run python -m atlas_bench import tbench regex-log fix-git # 指定题目；--ref 换仓库提交（默认固定一个）
deploy/server/images.sh pull $(cat var/bench/agent-tasks/tbench/images.txt)   # 在服务器上预拉题目镜像
uv run python -m atlas_bench agent run agent-platform/bench/experiments/tbench.yaml
```

- 前提：部署了评测档沙箱（`OPENSANDBOX_BENCH=1 deploy/local/opensandbox.sh`，root、根可写，镜像前缀默认放行 `alexgshaw/`）；
  连不上 GitHub 的部署要给评测档沙箱配出网代理（112：`deploy/server/bench-egress.sh`，否则校验卡在下载 uv）；
  经模型管理平台时，新建的评测智能体要先分配模型（`agent run … --prepare-only` 列出 id）。
  每题的 `agent_patch` 把智能体的沙箱换成题目镜像、`profile: bench`；校验是 `sandbox` 模式。
- 下载走 `HTTPS_PROXY`；下载过的题目缓存在 `var/bench/cache/tbench/<提交>/`。
- 与官方榜单的差异（`importers/tbench.py`）：资源上限用平台的沙箱配置；Atlas 的命令从 `/workspace` 开始，
  指令前加了一行起始目录；时间预算含沙箱启动；校验时现场联网装 pytest。

## 已实现 / 待做

已实现：统一数据格式、Atlas 适配器（入库复用、多种检索方式共用一个库）、断点续跑、出处匹配 + ranx 检索指标、
统一生成、CRAG 计分与拒答（裁判模型）、RAGAS 忠实度与事实正确性、错题归因、bootstrap 置信区间与配对比较、报告。

待做（见设计 §16）：公开评测集导入器（CRUD-RAG、MultiHop-RAG、RGB…；C-MTEB 已做）、RAGAS 提示词中文化与裁判校准、
其余 RAGAS 指标、Dify / RAGFlow 适配器、两两盲评、人工复核工具、私有文档出题、K8s Job。
