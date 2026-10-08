/**
 * 文件 → 渲染器（doc/detail/file-preview.html §04）。
 *
 * ★ 按扩展名选，不按服务端的 Content-Type：类型判断只在这一处做，前后端
 *   不会出现两种答案。扩展名不认识的归 "unknown"，由内容判定（前 8 KiB 有
 *   没有 NUL）决定按文本显示还是不支持。
 */
export type PreviewKind = "html" | "markdown" | "text" | "image" | "pdf" | "empty" | "unknown";

const BY_EXT: Record<string, PreviewKind> = {
  html: "html",
  htm: "html",
  md: "markdown",
  markdown: "markdown",
  png: "image",
  jpg: "image",
  jpeg: "image",
  gif: "image",
  webp: "image",
  svg: "image",
  ico: "image",
  pdf: "pdf",
};

const TEXT_EXT = new Set(
  (
    "txt log json jsonl yaml yml toml ini cfg conf csv tsv xml css scss less js mjs cjs ts tsx jsx " +
    "py rb go java kt rs c h cpp hpp cs php swift sh bash zsh sql graphql proto vue svelte " +
    "dockerfile makefile gitignore env lock"
  ).split(" "),
);

export function extOf(path: string): string {
  const name = path.slice(path.lastIndexOf("/") + 1).toLowerCase();
  const dot = name.lastIndexOf(".");
  // 没有扩展名的（Dockerfile、Makefile）用整个文件名当扩展名
  return dot > 0 ? name.slice(dot + 1) : name;
}

export function previewKind(path: string, size: number | undefined): PreviewKind {
  if (size === 0) return "empty";
  const ext = extOf(path);
  return BY_EXT[ext] ?? (TEXT_EXT.has(ext) ? "text" : "unknown");
}

/** 文本类的读取上限（Range），超出只显示开头并提示下载 */
export const TEXT_MAX_BYTES = 1024 * 1024;
/** 图片超过这个大小不渲染 —— 一张大图能把标签页内存撑爆 */
export const IMAGE_MAX_BYTES = 20 * 1024 * 1024;
