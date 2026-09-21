.PHONY: up down sync lint types arch test migrate serve check \
        web-install web-dev web-build web-check web-gen dev

# ---------- 后端 ----------
up:      ; docker compose up -d
down:    ; docker compose down
sync:    ; uv sync --all-packages
lint:    ; uv run ruff check . && uv run ruff format --check .
types:   ; uv run mypy engine/src server/src
arch:    ; uv run lint-imports
test:    ; uv run pytest -q
migrate: ; cd server && uv run alembic upgrade head
serve:   ; uv run uvicorn atlas_server.main:app --reload --port 8000
check: lint arch test

# ---------- 前端 ----------
web-install: ; cd web && pnpm install
web-dev:     ; cd web && pnpm dev
web-build:   ; cd web && pnpm build
web-check:   ; cd web && pnpm typecheck

# 对真实后端验证 SSE 帧解析、事件归约与断线续传（需要 make serve 在跑，会真实计费）。
# tsc 只能保证字段名拼对，保证不了"后端真的这么发"—— 改 events/sse/run-reducer 后必跑。
web-verify:  ; cd web && pnpm verify:stream

# API 类型从运行中的后端生成。★ 生成结果要提交 ——
# 否则新克隆的仓库在后端没起来时无法通过类型检查。
web-gen:     ; cd web && pnpm gen:api

# 前后端一起起。后端 :8000，前端 :3000（浏览器直连后端，靠 CORS，不走代理）
dev:
	@echo "后端 → http://127.0.0.1:8000   前端 → http://127.0.0.1:3000"
	@$(MAKE) -j2 serve web-dev

# 会话 Pod 的镜像（bridge + ACP adapter）。
#
# ★ 用 buildctl 而不是 docker build：这套环境里没有 docker 守护进程，
#   而 BuildKit 的独立二进制自带 runc，装了就能用。导出 OCI 归档之后
#   直接进 k3s 的 containerd —— 镜像是本地构建的，registry 上没有，
#   所以 Pod 那边必须靠 import 而不是 pull。
BRIDGE_IMAGE ?= atlas-acp-bridge:0.1.0
bridge-image:
	./scripts/build_bridge_image.sh $(BRIDGE_IMAGE)
