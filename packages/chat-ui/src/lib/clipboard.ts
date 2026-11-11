/**
 * 复制到剪贴板。
 *
 * ★ navigator.clipboard 只在安全上下文（https / localhost）里有：内网用 http 打开时它是
 *   undefined，裸调用会抛 TypeError，表现是「点了复制没反应」。这里先判断，没有就退到
 *   document.execCommand('copy')（不受安全上下文限制）。
 */
export async function copyText(text: string): Promise<boolean> {
  if (typeof navigator !== "undefined" && typeof navigator.clipboard?.writeText === "function") {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // 权限被拒等：退到下面的实现
    }
  }
  if (typeof document === "undefined") return false;
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try {
    return document.execCommand("copy");
  } finally {
    document.body.removeChild(ta);
  }
}
