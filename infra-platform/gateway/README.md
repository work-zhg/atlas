# API 网关（atlas-gateway）

服务器上唯一对外的入口：宿主机上由 systemd 管的 OpenResty，占 80 / 443。设计见
`c4-model/apigateway/design-api-gateway.html`。

OpenResty 跑在固定版本的官方镜像里（`openresty/openresty:1.27.1.2-bookworm-fat`，host 网络，systemd 服务
`atlas-gateway` 管 `docker run`），和端到端测试是同一个镜像。没用 apt 包：官方 apt 源的签名密钥是 SHA1 绑定的，
Debian 13 的 apt 拒收。

- **routes.yaml** 是站点和对外服务的唯一来源：每个对外的后端或前端只登记名字、种类、子路径、认证模式和转发档位，不写地址。
- **render**（`python -m atlas_gateway`）按硬规则校验后渲染成 Nginx 配置；违反任何一条就不生成。
- **Lua**（`src/atlas_gateway/lua/atlas_gw/`）只做三件事：本地验 Ed25519 访问 JWT、校 CSRF、封登录失败太多的 IP。转发、SSE、WebSocket、大文件、证书都是 Nginx 原生指令。

## 外部路径怎么对到服务

外部路径第一段 = 名字 = 内部域名第一段：

| 外部 | 发往（内部 Ingress，Host） | 上游看到的路径 |
|---|---|---|
| `/atlas/v1/threads` | `atlas.internal.techstz.com` | `/v1/threads`（后端去掉 `/<名>`） |
| `/atlas-ui/threads` | `atlas-ui.internal.techstz.com` | `/atlas-ui/threads`（前端原样，basePath = `/<名>-ui`） |

网关只连一个上游：集群里内部 Ingress 的固定 ClusterIP（`GATEWAY_INTERNAL_INGRESS`）。

## 常用命令

```bash
# 校验路由表（CI 里 test_rules.py 也会跑）
uv run python -m atlas_gateway --check --env ATLAS_PUBLIC_HOST=atlas.example.com --env GATEWAY_INTERNAL_INGRESS=10.96.0.20:80

# 看渲染结果
uv run python -m atlas_gateway --out /tmp/gw --allow-missing-certs \
  --env ATLAS_PUBLIC_HOST=atlas.example.com --env GATEWAY_INTERNAL_INGRESS=10.96.0.20:80

# 测试：test_rules.py 不要 Docker；test_e2e_*.py、test_lua_units.py 在 Docker 里起真网关（没有 Docker 自动跳过）
uv run pytest -q infra-platform/gateway/tests
```

部署在服务器上用 `deploy/server/gateway.sh`（install / cert / 渲染并 reload / status / unban）。

## 本机运维端口（127.0.0.1:8081）

| 地址 | 用途 |
|---|---|
| `GET /readyz` | 公钥环是否就绪（一把公钥都没有时 503，auth 路由也回 503 KEYS_UNAVAILABLE） |
| `GET /bans` | 登录失败封禁表 |
| `DELETE /bans/<ip>` | 提前解封 |

## 依赖用户中心的部分

- 访问 JWT：Ed25519（`alg=EdDSA`、`kid`），载荷 `iss=atlas-usercenter`、`aud=atlas-gateway`、`sub`（用户 uuid）、`tid`（租户 uuid）、`mcp`（须先改密）、`exp`。
- 公钥集：`GET /api/v1/auth/jwks`（经内部 Ingress，Host = `uc.internal…`），OKP 公钥。换钥时先发布、过一个刷新周期（5 分钟）再用新私钥签。
- Cookie：`uc_access`（访问 JWT，Path=/）、`uc_csrf`（可读，写请求带进 `X-UC-CSRF`）、`uc_refresh`（Path=/uc/api/v1/auth）。
- 登录接口 `/api/v1/auth/login`、`/api/v1/auth/token`：凭据不对一律 401（网关按 IP 数）。
