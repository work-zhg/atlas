-- 测试桩：扮演内部 Ingress 后面的各个服务（按 Host 区分）。
local cjson = require "cjson"

local _M = {}

local STATE = "/state"

local function json(status, value)
  ngx.status = status
  ngx.header["Content-Type"] = "application/json"
  ngx.say(cjson.encode(value))
end

-- 回显：网关到底转了什么过来（路径、Host、头、请求体大小）
local function echo()
  ngx.req.read_body()
  local body = ngx.req.get_body_data()
  local len = body and #body or 0
  if not body then
    local f = ngx.req.get_body_file()
    if f then
      local h = io.open(f, "rb")
      len = h:seek("end")
      h:close()
    end
  end
  json(200, {
    method = ngx.req.get_method(),
    uri = ngx.var.request_uri,
    host = ngx.var.http_host,
    headers = ngx.req.get_headers(100),
    body_len = len,
  })
end

local function jwks()
  local f = io.open(STATE .. "/uc-jwks.json", "rb")
  if not f then
    return json(404, { detail = "还没有公钥" })
  end
  local text = f:read("*a")
  f:close()
  ngx.header["Content-Type"] = "application/json"
  ngx.print(text)
end

local function login()
  ngx.req.read_body()
  local body = ngx.req.get_body_data() or ""
  if body:find("good", 1, true) then
    ngx.header["Set-Cookie"] = "uc_access=tok; Path=/; HttpOnly"
    return json(200, { ok = true })
  end
  return json(401, { code = "INVALID_CREDENTIALS" })
end

local function sse()
  ngx.header["Content-Type"] = "text/event-stream"
  ngx.print("data: 1\n\n")
  ngx.flush(true)
  ngx.sleep(1.5)
  ngx.print("data: 2\n\n")
  ngx.flush(true)
end

local function websocket()
  local server = require "resty.websocket.server"
  local wb, err = server:new({ timeout = 5000, max_payload_len = 65535 })
  if not wb then
    ngx.log(ngx.ERR, "ws: ", err)
    return ngx.exit(444)
  end
  local data, typ = wb:recv_frame()
  if typ == "text" then
    wb:send_text("echo:" .. data .. ":" .. (ngx.var.http_x_uid or ""))
  end
  wb:send_close()
end

function _M.handle()
  local host = (ngx.var.http_host or ""):match("^[^.]+") or ""
  local uri = ngx.var.uri
  if host == "uc" and uri == "/api/v1/auth/jwks" then
    return jwks()
  end
  if host == "uc" and (uri == "/api/v1/auth/login" or uri == "/api/v1/auth/token") then
    return login()
  end
  if uri:match("/events$") then
    return sse()
  end
  if uri:match("/browser/view$") and ngx.var.http_upgrade then
    return websocket()
  end
  return echo()
end

return _M
