"use client";

import ReactMarkdown from "react-markdown";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import remarkGfm from "remark-gfm";

/**
 * ★ 模型输出一律视为不可信内容（文档 §14）。
 *
 * react-markdown 默认不渲染原始 HTML，但只要将来有人手滑加了
 * rehype-raw，防线就没了 —— 所以显式挂 rehype-sanitize，把安全策略
 * 写在代码里而不是依赖某个默认值。
 *
 * 白名单在 defaultSchema 基础上收紧：
 *   · 不放行 img（模型可以借外链图片做像素追踪）
 *   · a 强制 target=_blank + rel=noopener，且只允许 http/https/mailto
 */
const schema = {
  ...defaultSchema,
  tagNames: (defaultSchema.tagNames ?? []).filter((t) => t !== "img"),
  attributes: {
    ...defaultSchema.attributes,
    a: [...(defaultSchema.attributes?.a ?? []), "target", "rel"],
  },
  protocols: {
    ...defaultSchema.protocols,
    href: ["http", "https", "mailto"],
  },
};

export function Markdown({ children, className }: { children: string; className?: string }) {
  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeSanitize, schema]]}
        components={{
          a: ({ href, children: inner }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {inner}
            </a>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
