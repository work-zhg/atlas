/** 文件的图标和颜色、大小和时间的写法（同原型）。用哪种方式预览查 chat-ui 的 previewKind（与网页同一张表）。 */
import { StyleSheet, View } from "react-native";

import { extOf, previewKind } from "@atlas/chat-ui/src/lib/previewKind";
import { Icon, type IconName } from "@/components/ui";

type Look = [IconName, string, string];
const LOOK: Record<string, Look> = {
  markdown: ["fileText", "#eef0ff", "#4f46e5"],
  image: ["image", "#e7f6ec", "#16a34a"],
  pdf: ["fileText", "#fdeaea", "#dc2626"],
  html: ["globe", "#fff1e6", "#ea580c"],
  code: ["code", "#e6f6fe", "#0369a1"],
  sheet: ["table", "#e6f7f1", "#0f766e"],
  doc: ["fileText", "#e8efff", "#1d4ed8"],
  other: ["file", "#f1f5f9", "#64748b"],
  dir: ["folder", "#fef3c7", "#d97706"],
};
const SHEET = new Set(["csv", "tsv", "xlsx", "xls"]);
const DOC = new Set(["docx", "doc", "pptx", "ppt"]);
const PLAIN = new Set(["txt", "log"]);

export function lookOf(path: string, isDir = false): Look {
  if (isDir) return LOOK.dir!;
  const e = extOf(path);
  if (SHEET.has(e)) return LOOK.sheet!;
  if (DOC.has(e)) return LOOK.doc!;
  const k = previewKind(path, 1);
  if (k === "text") return PLAIN.has(e) ? LOOK.other! : LOOK.code!;
  return LOOK[k] ?? LOOK.other!;
}

export function FileIcon({ path, dir = false, size = 40 }: { path: string; dir?: boolean; size?: number }) {
  const [name, bg, fg] = lookOf(path, dir);
  return (
    <View style={[s.box, { width: size, height: size, borderRadius: size * 0.25, backgroundColor: bg }]}>
      <Icon name={name} size={size * 0.5} color={fg} />
    </View>
  );
}

export function fmtSize(n: number | null | undefined): string {
  if (n === null || n === undefined) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

export const baseName = (p: string) => p.slice(p.lastIndexOf("/") + 1);
export const dirName = (p: string) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");

const s = StyleSheet.create({ box: { alignItems: "center", justifyContent: "center", flexShrink: 0 } });
