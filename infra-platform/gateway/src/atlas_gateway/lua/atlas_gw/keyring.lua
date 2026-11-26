-- KeyRing：用户中心的验签公钥（c4-model/apigateway/code-auth.html）。
--   启动时先读上次成功的 JWKS 缓存文件放进共享内存 gw_jwks（宿主机重启时网关常比 K8s 里的用户中心先起来，
--   读缓存就能马上验签）；worker 0 随后经内部 Ingress 拉：没拉成功前每 retry 秒一次，之后每 refresh 秒。
--   拉到新内容就更新共享内存（版本号 +1）并写回缓存文件。
--   每个 worker 按版本把公钥解析成 EVP_PKEY 缓存。遇到不认识的 kid：本次 401，并提前拉一次（kid_refetch_gap 秒内只拉一次）——
--   不在请求里同步拉，否则一个坏令牌就能打出大量对用户中心的请求。
--   一把公钥都没有时 ready() 为假：auth 路由返回 503，/readyz 不就绪。
local cjson = require "cjson.safe"
local b64 = require "ngx.base64"
local ed = require "atlas_gw.ed25519"
local http = require "atlas_gw.http"

local dict = ngx.shared.gw_jwks
local _M = {}
local conf
local parsed = { version = -1, keys = {} } -- 每个 worker 自己的 EVP_PKEY 缓存

-- JWKS 文本 → {kid = 32 字节原始公钥}；只收 Ed25519 的签名公钥
local function parse(text)
  local doc = cjson.decode(text)
  if type(doc) ~= "table" or type(doc.keys) ~= "table" then
    return nil, "不是 JWKS"
  end
  local out, n = {}, 0
  for _, k in ipairs(doc.keys) do
    if type(k) == "table" and k.kty == "OKP" and k.crv == "Ed25519" and type(k.kid) == "string"
      and type(k.x) == "string" and (k.use == nil or k.use == "sig") and (k.alg == nil or k.alg == "EdDSA") then
      local raw = b64.decode_base64url(k.x)
      if raw and #raw == 32 then
        out[k.kid] = raw
        n = n + 1
      end
    end
  end
  if n == 0 then
    return nil, "里面没有 Ed25519 公钥"
  end
  return out
end

-- 校验后放进共享内存；内容变了返回 "changed"
local function store(text)
  local keys, err = parse(text)
  if not keys then
    return nil, err
  end
  if dict:get("jwks") == text then
    return true
  end
  dict:set("jwks", text)
  dict:incr("version", 1, 0)
  return true, "changed"
end

local function write_cache(text)
  local tmp = conf.cache .. ".tmp"
  local f, err = io.open(tmp, "wb")
  if not f then
    ngx.log(ngx.WARN, "atlas_gw keyring: 写不了缓存文件 ", tmp, "：", err)
    return
  end
  f:write(text)
  f:close()
  local ok, rerr = os.rename(tmp, conf.cache)
  if not ok then
    ngx.log(ngx.WARN, "atlas_gw keyring: 换不了缓存文件：", rerr)
  end
end

-- init_by_lua（master）：记下设置，读缓存文件
function _M.init(c)
  conf = c
  local f = io.open(conf.cache, "rb")
  if not f then
    return
  end
  local text = f:read("*a")
  f:close()
  local ok, err = store(text)
  if not ok then
    ngx.log(ngx.WARN, "atlas_gw keyring: 缓存文件不能用（", err, "），等拉取")
  end
end

-- 拉一次（各 worker 之间用 gw_jwks 里的锁去重）
function _M.refresh()
  if not dict:add("fetching", true, math.ceil(conf.timeout_ms / 1000) + 2) then
    return nil, "别的 worker 正在拉"
  end
  local res, err = http.get(conf.addr, conf.port, conf.host, conf.path, conf.timeout_ms)
  local ok, changed
  if not res then
    ok, changed = nil, err
  elseif res.status ~= 200 then
    ok, changed = nil, "用户中心返回 " .. res.status
  else
    ok, changed = store(res.body)
    if ok then
      dict:set("fetched_at", ngx.now())
      if changed == "changed" then
        write_cache(res.body)
        ngx.log(ngx.NOTICE, "atlas_gw keyring: 公钥集已更新")
      end
    end
  end
  dict:delete("fetching")
  return ok, changed
end

-- init_worker_by_lua：只有 worker 0 定时拉
function _M.start()
  if ngx.worker.id() ~= 0 then
    return
  end
  local tick
  tick = function(premature)
    if premature then
      return
    end
    local ok, err = _M.refresh()
    if not ok and err ~= "别的 worker 正在拉" then
      ngx.log(ngx.WARN, "atlas_gw keyring: 拉公钥集失败：", err)
    end
    -- 拉成功前（包括只有缓存文件时）每 retry 秒一次，之后每 refresh 秒
    local delay = dict:get("fetched_at") and conf.refresh or conf.retry
    local tok, terr = ngx.timer.at(delay, tick)
    if not tok then
      ngx.log(ngx.ERR, "atlas_gw keyring: 定时器失败：", terr)
    end
  end
  ngx.timer.at(0, tick)
end

-- 遇到不认识的 kid：后台提前拉一次，间隔内只拉一次
function _M.on_unknown_kid()
  if dict:add("kid_refetch", true, conf.kid_refetch_gap) then
    ngx.timer.at(0, function(premature)
      if not premature then
        _M.refresh()
      end
    end)
  end
end

function _M.key(kid)
  local v = dict:get("version") or 0
  if v ~= parsed.version then
    local keys = {}
    local text = dict:get("jwks")
    local raw = text and parse(text) or {}
    for k, bytes in pairs(raw) do
      local pk, err = ed.public_key(bytes)
      if pk then
        keys[k] = pk
      else
        ngx.log(ngx.WARN, "atlas_gw keyring: 公钥 ", k, " 不能用：", err)
      end
    end
    parsed = { version = v, keys = keys }
  end
  return parsed.keys[kid]
end

function _M.ready()
  return dict:get("jwks") ~= nil
end

function _M.status()
  return {
    ready = _M.ready(),
    version = dict:get("version") or 0,
    fetched_at = dict:get("fetched_at"),
  }
end

return _M
