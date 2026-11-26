-- 最小的 HTTP GET（公钥环拉 JWKS 用）：cosocket 直连内部 Ingress，Host 写内部域名，HTTP/1.0、读到关连接为止。
-- 不引第三方库：只在定时器里每几分钟调一次，响应是几百字节的 JSON。
local _M = {}

local MAX_BODY = 256 * 1024

local function read_chunked(sock)
  local parts, total = {}, 0
  while true do
    local line, err = sock:receive("*l")
    if not line then
      return nil, "读分块长度失败：" .. tostring(err)
    end
    local size = tonumber(line:match("^%x+"), 16)
    if not size then
      return nil, "分块长度不对：" .. line
    end
    if size == 0 then
      return table.concat(parts)
    end
    total = total + size
    if total > MAX_BODY then
      return nil, "响应太大"
    end
    local data, rerr = sock:receive(size)
    if not data then
      return nil, "读分块失败：" .. tostring(rerr)
    end
    parts[#parts + 1] = data
    sock:receive("*l") -- 分块末尾的 CRLF
  end
end

-- 返回 {status=, body=} 或 nil, err
function _M.get(addr, port, host, path, timeout_ms)
  local sock = ngx.socket.tcp()
  sock:settimeout(timeout_ms or 3000)
  local ok, err = sock:connect(addr, port)
  if not ok then
    return nil, "连不上 " .. addr .. ":" .. port .. "：" .. tostring(err)
  end
  local req = "GET " .. path .. " HTTP/1.0\r\nHost: " .. host
    .. "\r\nAccept: application/json\r\nUser-Agent: atlas-gateway\r\nConnection: close\r\n\r\n"
  local _, serr = sock:send(req)
  if serr then
    sock:close()
    return nil, "发请求失败：" .. serr
  end
  local line, lerr = sock:receive("*l")
  if not line then
    sock:close()
    return nil, "读状态行失败：" .. tostring(lerr)
  end
  local status = tonumber(line:match("^HTTP/%d%.%d (%d%d%d)"))
  if not status then
    sock:close()
    return nil, "状态行不对：" .. line
  end
  local length, chunked
  while true do
    local h, herr = sock:receive("*l")
    if not h then
      sock:close()
      return nil, "读响应头失败：" .. tostring(herr)
    end
    if h == "" then
      break
    end
    local name, value = h:match("^([^:]+):%s*(.-)%s*$")
    if name then
      name = name:lower()
      if name == "content-length" then
        length = tonumber(value)
      elseif name == "transfer-encoding" and value:lower():find("chunked", 1, true) then
        chunked = true
      end
    end
  end
  local body, berr
  if chunked then
    body, berr = read_chunked(sock)
  elseif length then
    if length > MAX_BODY then
      sock:close()
      return nil, "响应太大"
    end
    body, berr = sock:receive(length)
  else
    body, berr = sock:receive("*a")
    if body and #body > MAX_BODY then
      body, berr = nil, "响应太大"
    end
  end
  sock:close()
  if not body then
    return nil, "读响应体失败：" .. tostring(berr)
  end
  return { status = status, body = body }
end

return _M
