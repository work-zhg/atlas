/**
 * 客户端 id 生成。
 *
 * ★ 不能直接用 `crypto.randomUUID()` —— 它**只在安全上下文可用**
 *   （HTTPS 或 localhost）。用 http 访问一个域名时它是 undefined，
 *   调用点会抛 TypeError。
 *
 *   踩过一次，而且症状完全不指向真因：把前端从 localhost 挪到
 *   http://<域名> 之后，聊天框点发送**毫无反应** —— 请求根本没发出去，
 *   网络面板是空的，后端日志也是空的，看起来像"输入框坏了"。
 *
 *   `crypto.getRandomValues` 没有这个限制（不限安全上下文），所以优先用它
 *   自己拼一个 v4；再退一步才用 Math.random —— 幂等键要的是唯一，
 *   不是密码学强度。
 */

function fromGetRandomValues(): string | null {
  const c = globalThis.crypto;
  if (!c?.getRandomValues) return null;

  const bytes = new Uint8Array(16);
  c.getRandomValues(bytes);
  // RFC 4122：version 4 + variant 10xx。
  // `?? 0` 只是为了满足 noUncheckedIndexedAccess —— 长度固定 16，下标必然存在。
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;

  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function fromMathRandom(): string {
  // 最后的兜底。不做密码学保证，只保证同一个浏览器里不重复。
  const rand = () => Math.floor(Math.random() * 0x100000000).toString(16).padStart(8, "0");
  return `${Date.now().toString(16)}-${rand()}-${rand()}`;
}

/** 唯一 id。可用时走 randomUUID，否则逐级回落。 */
export function randomId(): string {
  const c = globalThis.crypto;
  if (typeof c?.randomUUID === "function") return c.randomUUID();
  return fromGetRandomValues() ?? fromMathRandom();
}

/**
 * 发送消息的幂等键（§11.2）。
 *
 * 每次**用户发起**的发送生成一个新键：网络抖动导致的重发不会变成两条消息，
 * 而用户真正的两次发送必须是两个 run。
 */
export function newIdempotencyKey(): string {
  return randomId();
}
