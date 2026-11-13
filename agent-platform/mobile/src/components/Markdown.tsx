/**
 * Markdown（回答正文、计划、预览）。
 *
 * ★ 用 useMarkdown 拿元素、自己放进 View：默认的 <Markdown> 是一个 FlatList，放进对话列表里会嵌套滚动。
 * ★ 代码块：深色底、横向滑动、右上角复制（同原型）。表格本身可以横向滑动，不撑破屏幕。
 */
import * as Clipboard from "expo-clipboard";
import { memo, useMemo, type ReactNode } from "react";
import { ScrollView, StyleSheet, Text, View, type TextStyle, type ViewStyle } from "react-native";
import { Renderer, useMarkdown, type MarkedStyles } from "react-native-marked";

import { C, F, MONO } from "@/theme";
import { IconButton, useToast } from "./ui";

class AtlasRenderer extends Renderer {
  constructor(private readonly onCopy: (text: string) => void) {
    super({ selectable: true });
  }

  override code(text: string, language?: string, _containerStyle?: ViewStyle, _textStyle?: TextStyle): ReactNode {
    return (
      <View key={this.getKey()} style={s.code}>
        <View style={s.codeHead}>
          <Text style={s.codeLang}>{language || "代码"}</Text>
          <IconButton name="copy" label="复制代码" size={16} color={C.ink4} onPress={() => this.onCopy(text)} />
        </View>
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ padding: 12 }}>
          <View>
            <Text selectable style={s.codeText}>
              {text}
            </Text>
          </View>
        </ScrollView>
      </View>
    );
  }
}

const styles: MarkedStyles = {
  text: { fontSize: F.body, lineHeight: 25, color: C.ink },
  paragraph: { marginBottom: 10, paddingVertical: 0 },
  strong: { fontWeight: "700" },
  h1: { fontSize: 21, fontWeight: "700", color: C.ink, marginTop: 6, marginBottom: 6, lineHeight: 29, borderBottomWidth: 0 },
  h2: { fontSize: 17, fontWeight: "700", color: C.ink, marginTop: 14, marginBottom: 6, lineHeight: 25, borderBottomWidth: 0 },
  h3: { fontSize: 16, fontWeight: "700", color: C.ink, marginTop: 12, marginBottom: 6, lineHeight: 24 },
  h4: { fontSize: F.body, fontWeight: "700", color: C.ink, marginTop: 10, marginBottom: 4 },
  codespan: { fontFamily: MONO, fontSize: 13, backgroundColor: C.line2, color: C.ink },
  link: { color: C.primary, textDecorationLine: "none" },
  li: { fontSize: F.body, lineHeight: 25, color: C.ink },
  list: { marginBottom: 10 },
  blockquote: { borderLeftWidth: 3, borderLeftColor: C.line, paddingLeft: 10, marginBottom: 10 },
  table: { borderWidth: 0, marginBottom: 12 },
  tableRow: { borderBottomWidth: StyleSheet.hairlineWidth, borderColor: C.line },
  tableCell: { paddingVertical: 8, paddingHorizontal: 10 },
  hr: { backgroundColor: C.line, height: StyleSheet.hairlineWidth, marginVertical: 12 },
};
const asideStyles: MarkedStyles = {
  ...styles,
  text: { ...styles.text, color: C.ink3, fontSize: F.md, lineHeight: 22 },
  li: { ...styles.li, color: C.ink3, fontSize: F.md, lineHeight: 22 },
};

/**
 * aside：过程性发言（后面还跟着工具、委派的那段话），弱化显示；最后一段是结论，正常显示。
 */
export const Markdown = memo(function Markdown({ children, aside = false }: { children: string; aside?: boolean }) {
  const toast = useToast();
  const renderer = useMemo(
    () => new AtlasRenderer((text) => void Clipboard.setStringAsync(text).then(() => toast("已复制"))),
    [toast],
  );
  const elements = useMarkdown(children, { renderer, styles: aside ? asideStyles : styles, colorScheme: "light" });
  return <View>{elements}</View>;
});

const s = StyleSheet.create({
  code: { backgroundColor: C.code, borderRadius: 12, marginBottom: 12, overflow: "hidden" },
  codeHead: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", backgroundColor: C.codeHead, paddingLeft: 12, height: 36 },
  codeLang: { fontSize: F.xs, color: C.ink4 },
  codeText: { fontFamily: MONO, fontSize: 12.5, lineHeight: 20, color: C.codeInk },
});
