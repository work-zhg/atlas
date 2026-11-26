-- Ed25519 验签：LuaJIT FFI 直接调 OpenSSL（OpenResty 自带、nginx 已链接的那份 libcrypto）。
-- 只用到三个函数：按 32 字节原始公钥建 EVP_PKEY、EVP_DigestVerifyInit、EVP_DigestVerify（OpenSSL ≥ 1.1.1）。
local ffi = require "ffi"
local ffi_gc = ffi.gc

local _M = {}

ffi.cdef [[
typedef struct atlas_gw_evp_pkey_st atlas_gw_EVP_PKEY;
typedef struct atlas_gw_evp_md_ctx_st atlas_gw_EVP_MD_CTX;
atlas_gw_EVP_PKEY *EVP_PKEY_new_raw_public_key(int type, void *e, const unsigned char *pub, size_t len);
void EVP_PKEY_free(atlas_gw_EVP_PKEY *pkey);
atlas_gw_EVP_MD_CTX *EVP_MD_CTX_new(void);
void EVP_MD_CTX_free(atlas_gw_EVP_MD_CTX *ctx);
int EVP_DigestVerifyInit(atlas_gw_EVP_MD_CTX *ctx, void *pctx, const void *type, void *e, atlas_gw_EVP_PKEY *pkey);
int EVP_DigestVerify(atlas_gw_EVP_MD_CTX *ctx, const unsigned char *sig, size_t siglen,
                     const unsigned char *tbs, size_t tbslen);
void ERR_clear_error(void);
]]

-- nginx 进程里已经有 libcrypto 的符号就直接用；没有（个别发行版静态链接又没导出）再按名字载入
local C = ffi.C
if not pcall(function()
  return C.EVP_PKEY_new_raw_public_key
end) then
  C = ffi.load("crypto")
end

local EVP_PKEY_ED25519 = 1087 -- NID_ED25519

-- 32 字节原始公钥 → EVP_PKEY（随 GC 释放）；不合法返回 nil
function _M.public_key(raw)
  if type(raw) ~= "string" or #raw ~= 32 then
    return nil, "公钥要是 32 字节"
  end
  local pk = C.EVP_PKEY_new_raw_public_key(EVP_PKEY_ED25519, nil, raw, 32)
  if pk == nil then
    C.ERR_clear_error()
    return nil, "OpenSSL 不接受这把公钥"
  end
  return ffi_gc(pk, C.EVP_PKEY_free)
end

-- 验「头.载荷」的签名（64 字节）。失败时清掉 OpenSSL 的错误队列，免得串到别的连接的 TLS 报错里
function _M.verify(pk, msg, sig)
  if #sig ~= 64 then
    return false
  end
  local ctx = C.EVP_MD_CTX_new()
  if ctx == nil then
    return false
  end
  local ok = C.EVP_DigestVerifyInit(ctx, nil, nil, nil, pk) == 1
    and C.EVP_DigestVerify(ctx, sig, #sig, msg, #msg) == 1
  C.EVP_MD_CTX_free(ctx)
  if not ok then
    C.ERR_clear_error()
  end
  return ok
end

return _M
