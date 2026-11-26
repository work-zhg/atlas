-- JwtVerifier：本地验访问 JWT，不做任何网络请求（c4-model/apigateway/code-auth.html）。
--   只认 alg=EdDSA（不接受 none，也不接受拿公钥当 HMAC 密钥的 HS256），kid 必须在公钥环里；
--   验「头.载荷」的签名；核对 iss、aud、exp（允许时钟偏差）、nbf；sub、tid 必须是 UUID（它们会写进请求头）。
--   过期 → TOKEN_EXPIRED，其他不通过 → TOKEN_INVALID。
local cjson = require "cjson.safe"
local b64 = require "ngx.base64"
local ed = require "atlas_gw.ed25519"
local keyring = require "atlas_gw.keyring"

local _M = {}

local UUID = "^%x%x%x%x%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%x%x%x%x%x%x%x%x$"

local function json_part(seg)
  local raw = b64.decode_base64url(seg)
  if not raw then
    return nil
  end
  local v = cjson.decode(raw)
  if type(v) ~= "table" then
    return nil
  end
  return v
end

local function audience_ok(aud, want)
  if aud == want then
    return true
  end
  if type(aud) == "table" then
    for _, a in ipairs(aud) do
      if a == want then
        return true
      end
    end
  end
  return false
end

-- 返回 claims，或 nil, "TOKEN_INVALID" | "TOKEN_EXPIRED"
function _M.verify(raw, s)
  if type(raw) ~= "string" or #raw > s.max_len then
    return nil, "TOKEN_INVALID"
  end
  local h64, p64, sig64 = raw:match("^([%w_%-]+)%.([%w_%-]+)%.([%w_%-]+)$")
  if not h64 then
    return nil, "TOKEN_INVALID"
  end
  local header = json_part(h64)
  if not header or header.alg ~= "EdDSA" or type(header.kid) ~= "string" or header.crit ~= nil then
    return nil, "TOKEN_INVALID"
  end
  local key = keyring.key(header.kid)
  if not key then
    keyring.on_unknown_kid()
    return nil, "TOKEN_INVALID"
  end
  local sig = b64.decode_base64url(sig64)
  if not sig or #sig ~= 64 or not ed.verify(key, h64 .. "." .. p64, sig) then
    return nil, "TOKEN_INVALID"
  end
  -- 签名对得上，载荷就一个字都没被改过；再看内容
  local claims = json_part(p64)
  if not claims or claims.iss ~= s.issuer or not audience_ok(claims.aud, s.audience) then
    return nil, "TOKEN_INVALID"
  end
  if type(claims.exp) ~= "number" then
    return nil, "TOKEN_INVALID"
  end
  local now = ngx.time()
  if type(claims.nbf) == "number" and now + s.clock_skew < claims.nbf then
    return nil, "TOKEN_INVALID"
  end
  if type(claims.sub) ~= "string" or not claims.sub:match(UUID)
    or type(claims.tid) ~= "string" or not claims.tid:match(UUID) then
    return nil, "TOKEN_INVALID"
  end
  if now > claims.exp + s.clock_skew then
    return nil, "TOKEN_EXPIRED"
  end
  return claims
end

return _M
