/**
 * 输入框：首页与对话页共用（同网页 chat-ui 的 Composer）。
 *
 * ★ 回车换行、点按钮发送：手机键盘上回车发送容易误发。字号 16，iOS 聚焦时不会放大页面。
 * ★ 添加的文件只收集，不在这里上传：首页发第一句话时会话还没建。都交给 onSend；它失败时保留文字和文件，不用重打。
 * ★ Android 没有网页那种「拍照 / 照片 / 文件」统一选择器，「+」弹一个三项的面板（原型里网页版直接调系统的）。
 */
import { File } from "expo-file-system";
import * as ImagePicker from "expo-image-picker";
import { useEffect, useState, type ReactNode } from "react";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { Icon, Sheet, SheetRow, useToast } from "@/components/ui";
import { C, F } from "@/theme";

export interface Attachment {
  name: string;
  /** expo-file-system 的 File 就是 Blob，直接当上传的请求体 */
  body: Blob;
  isImage: boolean;
}

export function Composer({
  placeholder,
  running,
  onSend,
  onCancel,
  toolbar,
  context,
  note,
  disabled,
  fill,
}: {
  /** 从外面填进输入框（首页的快捷问题）；seq 变了才填 */
  fill?: { text: string; seq: number };
  placeholder: string;
  running: boolean;
  onSend: (text: string, files: Attachment[]) => Promise<void>;
  onCancel?: () => void;
  /** 「+」之后的胶囊（执行前确认、模型） */
  toolbar?: ReactNode;
  /** 输入框上方的标签（工作目录、电脑） */
  context?: ReactNode;
  /** 输入框下方的一行小字 */
  note?: string;
  disabled?: boolean;
}) {
  const toast = useToast();
  const [text, setText] = useState("");
  const [files, setFiles] = useState<Attachment[]>([]);
  const [sending, setSending] = useState(false);
  const [picking, setPicking] = useState(false);
  const canSend = !disabled && !sending && (text.trim().length > 0 || files.length > 0);
  useEffect(() => {
    if (fill) setText(fill.text);
  }, [fill]);

  const send = async () => {
    if (!canSend) return;
    setSending(true);
    try {
      await onSend(text.trim(), files);
      setText("");
      setFiles([]);
    } catch {
      // 调用方已经提示了原因；文字和文件留着
    } finally {
      setSending(false);
    }
  };

  const fromCamera = async () => {
    setPicking(false);
    const perm = await ImagePicker.requestCameraPermissionsAsync();
    if (!perm.granted) return toast("没有相机权限：到系统设置里打开");
    const r = await ImagePicker.launchCameraAsync({ mediaTypes: ["images"], quality: 0.8 });
    if (r.canceled) return;
    setFiles((fs) => [...fs, ...r.assets.map((a) => ({ name: a.fileName || `photo-${Date.now()}.jpg`, body: new File(a.uri), isImage: true }))]);
  };
  const fromLibrary = async () => {
    setPicking(false);
    const r = await ImagePicker.launchImageLibraryAsync({ mediaTypes: ["images"], allowsMultipleSelection: true, quality: 0.8 });
    if (r.canceled) return;
    setFiles((fs) => [...fs, ...r.assets.map((a) => ({ name: a.fileName || `image-${Date.now()}.jpg`, body: new File(a.uri), isImage: true }))]);
  };
  const fromFiles = async () => {
    setPicking(false);
    const r = await File.pickFileAsync({ multipleFiles: true });
    if (r.canceled) return;
    setFiles((fs) => [...fs, ...r.result.map((f) => ({ name: f.name, body: f, isImage: false }))]);
  };

  return (
    <View>
      {context ? <View style={s.context}>{context}</View> : null}
      {files.length > 0 && (
        <View style={s.atts}>
          {files.map((f, i) => (
            <View key={`${f.name}:${i}`} style={s.att}>
              <Icon name={f.isImage ? "image" : "file"} size={15} color={C.ink3} />
              <Text style={s.attName} numberOfLines={1}>
                {f.name}
              </Text>
              <Pressable hitSlop={8} onPress={() => setFiles((fs) => fs.filter((_, j) => j !== i))} accessibilityLabel="去掉这个文件">
                <Icon name="x" size={15} color={C.ink3} />
              </Pressable>
            </View>
          ))}
        </View>
      )}
      <View style={s.box}>
        <TextInput
          style={s.input}
          value={text}
          onChangeText={setText}
          placeholder={placeholder}
          placeholderTextColor={C.ink4}
          multiline
          editable={!disabled}
          maxLength={20000}
        />
        <View style={s.bar}>
          <Pressable style={s.add} onPress={() => setPicking(true)} disabled={disabled} accessibilityLabel="添加文件">
            <Icon name="plus" size={18} />
          </Pressable>
          {/* 胶囊占剩下的宽度、放不下就缩（文字省略），发送按钮始终在框内 */}
          <View style={s.tools}>{toolbar}</View>
          {running ? (
            <Pressable style={[s.send, { backgroundColor: C.ink }]} onPress={onCancel} accessibilityLabel="停止">
              <Icon name="stop" size={13} color="#fff" strokeWidth={3} />
            </Pressable>
          ) : (
            <Pressable style={[s.send, !canSend && { backgroundColor: "#c7cbd6" }]} onPress={() => void send()} disabled={!canSend} accessibilityLabel="发送">
              <Icon name="up" size={18} color="#fff" strokeWidth={2.4} />
            </Pressable>
          )}
        </View>
      </View>
      {note ? <Text style={s.note}>{note}</Text> : null}
      <Sheet open={picking} onClose={() => setPicking(false)} title="添加文件">
        <View style={{ paddingHorizontal: 12, paddingBottom: 8 }}>
          <SheetRow icon="camera" title="拍照" onPress={() => void fromCamera()} />
          <SheetRow icon="images" title="照片" onPress={() => void fromLibrary()} />
          <SheetRow icon="file" title="文件" sub="从手机里选" onPress={() => void fromFiles()} />
        </View>
      </Sheet>
    </View>
  );
}

/** 输入框底栏里的胶囊（执行前确认、模型） */
export function Pill({ icon, label, onPress, disabled }: { icon?: "shield" | "list" | "brain"; label: string; onPress?: () => void; disabled?: boolean }) {
  return (
    <Pressable style={[s.pill, disabled && { opacity: 0.5 }]} onPress={onPress} disabled={disabled || !onPress}>
      {icon && <Icon name={icon} size={14} color={C.ink2} />}
      <Text style={s.pillText} numberOfLines={1}>
        {label}
      </Text>
      {onPress && <Icon name="down" size={14} color={C.ink3} />}
    </Pressable>
  );
}

/** 输入框上方的标签（工作目录、电脑） */
export function ContextChip({
  icon,
  label,
  tone,
  onPress,
  onRemove,
}: {
  icon: "folder" | "laptop" | "monitor";
  label: string;
  tone: "dir" | "dev";
  onPress?: () => void;
  onRemove?: () => void;
}) {
  const t = tone === "dir" ? { bg: "#fef3c7", fg: C.warningInk } : { bg: C.secondaryWeak, fg: C.secondaryInk };
  return (
    <Pressable style={[s.chip, { backgroundColor: t.bg }]} onPress={onPress} disabled={!onPress}>
      <Icon name={icon} size={15} color={t.fg} />
      <Text style={[s.chipText, { color: t.fg }]} numberOfLines={1}>
        {label}
      </Text>
      {onRemove ? (
        <Pressable hitSlop={10} onPress={onRemove} accessibilityLabel="去掉">
          <Icon name="x" size={15} color={t.fg} />
        </Pressable>
      ) : onPress ? (
        <Icon name="down" size={14} color={t.fg} />
      ) : null}
    </Pressable>
  );
}

const s = StyleSheet.create({
  context: { flexDirection: "row", flexWrap: "wrap", gap: 6, marginBottom: 8 },
  atts: { flexDirection: "row", flexWrap: "wrap", gap: 6, marginBottom: 8 },
  att: { flexDirection: "row", alignItems: "center", gap: 6, height: 34, paddingLeft: 10, paddingRight: 8, borderRadius: 10, backgroundColor: C.hover, maxWidth: 220 },
  attName: { fontSize: F.sm, color: C.ink2, flexShrink: 1 },
  box: {
    borderWidth: 1,
    borderColor: "#dfe2ea",
    borderRadius: 24,
    paddingLeft: 14,
    paddingRight: 8,
    paddingTop: 8,
    paddingBottom: 8,
    backgroundColor: C.surface,
    elevation: 2,
    shadowColor: "#101828",
    shadowOpacity: 0.06,
    shadowRadius: 10,
    shadowOffset: { width: 0, height: 2 },
  },
  input: { fontSize: F.input, lineHeight: 22, color: C.ink, minHeight: 32, maxHeight: 132, paddingVertical: 4, paddingHorizontal: 0, textAlignVertical: "top" },
  bar: { flexDirection: "row", alignItems: "center", gap: 6, marginTop: 4 },
  tools: { flex: 1, flexDirection: "row", alignItems: "center", gap: 6 },
  add: { width: 34, height: 34, borderRadius: 17, borderWidth: 1, borderColor: C.line, alignItems: "center", justifyContent: "center" },
  send: { width: 36, height: 36, borderRadius: 18, backgroundColor: C.primary, alignItems: "center", justifyContent: "center" },
  note: { fontSize: 11.5, color: C.ink4, textAlign: "center", marginTop: 6 },
  pill: { flexDirection: "row", alignItems: "center", gap: 4, height: 32, paddingHorizontal: 10, borderRadius: 16, borderWidth: 1, borderColor: C.line, maxWidth: 160, flexShrink: 1 },
  pillText: { fontSize: F.sm, color: C.ink2, fontWeight: "600", flexShrink: 1 },
  chip: { flexDirection: "row", alignItems: "center", gap: 6, height: 32, paddingLeft: 10, paddingRight: 10, borderRadius: 16, maxWidth: "100%" },
  chipText: { fontSize: F.sm, flexShrink: 1 },
});
