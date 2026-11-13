/**
 * 通用界面零件：按原型（agent-platform/doc/prototype/mobile.html）的样式。
 * 点按区域不小于 44×44；颜色只取 theme。
 */
import * as Icons from "lucide-react-native";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
  type ReactNode,
} from "react";
import {
  ActivityIndicator,
  Modal,
  Pressable,
  StyleSheet,
  Text,
  View,
  type StyleProp,
  type TextStyle,
  type ViewStyle,
} from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";
import Animated, { FadeIn, FadeOut, SlideInDown } from "react-native-reanimated";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { C, F, R, avatarColor } from "@/theme";

// ── 图标

export const I = {
  menu: Icons.Menu,
  newChat: Icons.SquarePen,
  back: Icons.ChevronLeft,
  right: Icons.ChevronRight,
  down: Icons.ChevronDown,
  up: Icons.ArrowUp,
  stop: Icons.Square,
  plus: Icons.Plus,
  folder: Icons.Folder,
  file: Icons.File,
  fileText: Icons.FileText,
  image: Icons.Image,
  code: Icons.Code,
  table: Icons.Table,
  globe: Icons.Globe,
  check: Icons.Check,
  x: Icons.X,
  share: Icons.Share2,
  download: Icons.Download,
  copy: Icons.Copy,
  refresh: Icons.RefreshCw,
  like: Icons.ThumbsUp,
  dislike: Icons.ThumbsDown,
  terminal: Icons.Terminal,
  pencil: Icons.Pencil,
  trash: Icons.Trash2,
  alert: Icons.CircleAlert,
  warn: Icons.TriangleAlert,
  list: Icons.List,
  help: Icons.CircleHelp,
  shield: Icons.Shield,
  book: Icons.BookOpen,
  todo: Icons.ListTodo,
  wrench: Icons.Wrench,
  laptop: Icons.Laptop,
  monitor: Icons.Monitor,
  sort: Icons.ArrowDownUp,
  gauge: Icons.Gauge,
  external: Icons.ExternalLink,
  clock: Icons.Clock,
  offline: Icons.WifiOff,
  eye: Icons.Eye,
  lock: Icons.Lock,
  search: Icons.Search,
  chat: Icons.MessageSquare,
  logout: Icons.LogOut,
  bookmark: Icons.Bookmark,
  plug: Icons.Plug,
  spark: Icons.Sparkles,
  brain: Icons.Brain,
  camera: Icons.Camera,
  images: Icons.Images,
  clip: Icons.Paperclip,
  server: Icons.Server,
  more: Icons.Ellipsis,
} as const;
export type IconName = keyof typeof I;

export function Icon({
  name,
  size = 20,
  color = C.ink2,
  strokeWidth = 1.8,
}: {
  name: IconName;
  size?: number;
  color?: string;
  strokeWidth?: number;
}) {
  const Cmp = I[name];
  return <Cmp size={size} color={color} strokeWidth={strokeWidth} />;
}

// ── 头像、标签

export function Avatar({
  name,
  avatarKey,
  color,
  size = 28,
  square = false,
}: {
  name: string;
  avatarKey?: string | null;
  color?: string;
  size?: number;
  square?: boolean;
}) {
  return (
    <View
      style={{
        width: size,
        height: size,
        borderRadius: square ? size * 0.3 : size / 2,
        backgroundColor: color ?? avatarColor(avatarKey),
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <Text style={{ color: "#fff", fontWeight: "700", fontSize: Math.round(size * 0.44) }}>
        {(name.trim().charAt(0) || "?").toUpperCase()}
      </Text>
    </View>
  );
}

export type Tone = "default" | "primary" | "success" | "warning" | "danger" | "blue";
const TONE: Record<Tone, { bg: string; fg: string }> = {
  default: { bg: C.line2, fg: C.ink2 },
  primary: { bg: C.primaryWeak, fg: C.primaryInk },
  success: { bg: C.successWeak, fg: C.success },
  warning: { bg: C.warningWeak, fg: C.warning },
  danger: { bg: C.dangerWeak, fg: C.danger },
  blue: { bg: C.secondaryWeak, fg: C.secondaryInk },
};

export function Tag({ children, tone = "default" }: { children: ReactNode; tone?: Tone }) {
  const t = TONE[tone];
  return (
    <View style={[s.tag, { backgroundColor: t.bg }]}>
      <Text style={[s.tagText, { color: t.fg }]} numberOfLines={1}>
        {children}
      </Text>
    </View>
  );
}

// ── 按钮

export function IconButton({
  name,
  onPress,
  color = C.ink2,
  size = 22,
  badge,
  disabled,
  label,
}: {
  name: IconName;
  onPress?: () => void;
  color?: string;
  size?: number;
  badge?: number;
  disabled?: boolean;
  label: string;
}) {
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityLabel={label}
      hitSlop={4}
      style={({ pressed }) => [s.iconBtn, pressed && { backgroundColor: C.hover }, disabled && { opacity: 0.4 }]}
    >
      <Icon name={name} size={size} color={color} />
      {badge ? (
        <View style={s.badge}>
          <Text style={s.badgeText}>{badge > 99 ? "99+" : badge}</Text>
        </View>
      ) : null}
    </Pressable>
  );
}

export function Button({
  children,
  onPress,
  kind = "default",
  icon,
  disabled,
  loading,
  small,
  style,
}: {
  children: ReactNode;
  onPress?: () => void;
  kind?: "default" | "primary" | "danger" | "solidDanger";
  icon?: IconName;
  disabled?: boolean;
  loading?: boolean;
  small?: boolean;
  style?: StyleProp<ViewStyle>;
}) {
  const fg = kind === "primary" || kind === "solidDanger" ? "#fff" : kind === "danger" ? C.danger : C.ink;
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled || loading}
      style={({ pressed }) => [
        s.btn,
        small && s.btnSm,
        kind === "primary" && { backgroundColor: C.primary, borderColor: C.primary },
        kind === "solidDanger" && { backgroundColor: C.danger, borderColor: C.danger },
        pressed && { opacity: 0.85 },
        (disabled || loading) && { opacity: 0.45 },
        style,
      ]}
    >
      {loading ? <ActivityIndicator size="small" color={fg} /> : icon ? <Icon name={icon} size={17} color={fg} /> : null}
      <Text style={[s.btnText, small && { fontSize: F.md }, { color: fg }]}>{children}</Text>
    </Pressable>
  );
}

// ── 顶栏

export function Header({
  left,
  title,
  subtitle,
  onTitlePress,
  right,
  line = false,
}: {
  left?: ReactNode;
  title?: ReactNode;
  subtitle?: ReactNode;
  onTitlePress?: () => void;
  right?: ReactNode;
  line?: boolean;
}) {
  return (
    <View style={[s.header, line && s.headerLine]}>
      <View style={s.headerSide}>{left}</View>
      <Pressable style={s.headerTitle} onPress={onTitlePress} disabled={!onTitlePress}>
        {typeof title === "string" ? (
          <Text style={s.headerTitleText} numberOfLines={1}>
            {title}
          </Text>
        ) : (
          title
        )}
        {subtitle ? (
          typeof subtitle === "string" ? (
            <Text style={s.headerSub} numberOfLines={1}>
              {subtitle}
            </Text>
          ) : (
            subtitle
          )
        ) : null}
      </Pressable>
      <View style={[s.headerSide, { justifyContent: "flex-end" }]}>{right}</View>
    </View>
  );
}

// ── 提示条、空状态、加载

export function Banner({
  tone,
  icon,
  children,
  action,
}: {
  tone: "info" | "warn" | "err";
  icon?: IconName;
  children: ReactNode;
  action?: { label: string; onPress: () => void };
}) {
  const t = tone === "info" ? { bg: C.primaryWeak, fg: C.primaryInk } : tone === "warn" ? { bg: C.warningWeak, fg: C.warningInk } : { bg: C.dangerWeak, fg: C.dangerInk };
  return (
    <View style={[s.banner, { backgroundColor: t.bg }]}>
      {icon && <Icon name={icon} size={16} color={t.fg} />}
      <Text style={[s.bannerText, { color: t.fg }]}>{children}</Text>
      {action && (
        <Pressable onPress={action.onPress} hitSlop={10}>
          <Text style={[s.bannerAct, { color: t.fg }]}>{action.label}</Text>
        </Pressable>
      )}
    </View>
  );
}

export function Empty({
  icon,
  title,
  children,
  iconBg,
  iconColor,
  footer,
}: {
  icon: IconName;
  title: string;
  children?: ReactNode;
  iconBg?: string;
  iconColor?: string;
  footer?: ReactNode;
}) {
  return (
    <View style={s.empty}>
      <View style={[s.emptyIcon, iconBg ? { backgroundColor: iconBg } : null]}>
        <Icon name={icon} size={28} color={iconColor ?? C.ink3} />
      </View>
      <Text style={s.emptyTitle}>{title}</Text>
      {children ? <Text style={s.emptyText}>{children}</Text> : null}
      {footer}
    </View>
  );
}

export function Loading({ style }: { style?: StyleProp<ViewStyle> }) {
  return (
    <View style={[{ padding: 32, alignItems: "center" }, style]}>
      <ActivityIndicator color={C.primary} />
    </View>
  );
}

export function Spin({ size = 14 }: { size?: number }) {
  return <ActivityIndicator size={size > 16 ? "large" : "small"} color={C.primary} style={{ width: size, height: size, transform: [{ scale: size / 20 }] }} />;
}

// ── 底部面板

export function Sheet({
  open,
  onClose,
  title,
  subtitle,
  right,
  tall = false,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  subtitle?: ReactNode;
  right?: ReactNode;
  /** 占屏幕 82%（文件、智能体这类长列表） */
  tall?: boolean;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const insets = useSafeAreaInsets();
  return (
    <Modal visible={open} transparent animationType="fade" onRequestClose={onClose} statusBarTranslucent navigationBarTranslucent>
      <KeyboardAvoidingView behavior="padding" style={{ flex: 1 }}>
        <Pressable style={s.mask} onPress={onClose} accessibilityLabel="关闭" />
        <Animated.View entering={SlideInDown.duration(220)} style={[s.sheet, tall && { height: "82%" }, { paddingBottom: insets.bottom + 4 }]}>
          <View style={s.grab} />
          {(title || right) && (
            <View style={s.sheetHead}>
              <View style={{ flex: 1, minWidth: 0 }}>
                {typeof title === "string" ? (
                  <Text style={s.sheetTitle} numberOfLines={1}>
                    {title}
                  </Text>
                ) : (
                  title
                )}
                {subtitle ? (
                  typeof subtitle === "string" ? (
                    <Text style={s.sheetSub} numberOfLines={1}>
                      {subtitle}
                    </Text>
                  ) : (
                    subtitle
                  )
                ) : null}
              </View>
              {right}
              <IconButton name="x" label="关闭" onPress={onClose} />
            </View>
          )}
          <View style={tall ? { flex: 1 } : { flexShrink: 1 }}>{children}</View>
          {footer}
        </Animated.View>
      </KeyboardAvoidingView>
    </Modal>
  );
}

export function SheetRow({
  icon,
  title,
  sub,
  onPress,
  danger,
  checked,
  disabled,
  left,
  right,
  titleStyle,
}: {
  icon?: IconName;
  title: ReactNode;
  sub?: ReactNode;
  onPress?: () => void;
  danger?: boolean;
  checked?: boolean;
  disabled?: boolean;
  left?: ReactNode;
  right?: ReactNode;
  titleStyle?: StyleProp<TextStyle>;
}) {
  const fg = danger ? C.danger : C.ink;
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled || !onPress}
      style={({ pressed }) => [s.row, pressed && { backgroundColor: C.hover }, disabled && { opacity: 0.45 }]}
    >
      {left ?? (icon ? <Icon name={icon} size={20} color={danger ? C.danger : C.ink2} /> : null)}
      <View style={{ flex: 1, minWidth: 0 }}>
        {typeof title === "string" ? <Text style={[s.rowTitle, { color: fg }, titleStyle]}>{title}</Text> : title}
        {sub ? typeof sub === "string" ? <Text style={s.rowSub}>{sub}</Text> : sub : null}
      </View>
      {right}
      {checked ? <Icon name="check" size={20} color={C.primary} /> : null}
    </Pressable>
  );
}

export function SheetGroup({ children }: { children: ReactNode }) {
  return <Text style={s.group}>{children}</Text>;
}

// ── 轻提示

const ToastCtx = createContext<(msg: string) => void>(() => undefined);
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [msg, setMsg] = useState<{ text: string; id: number } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const show = useCallback((text: string) => {
    if (timer.current) clearTimeout(timer.current);
    setMsg({ text, id: Date.now() });
    timer.current = setTimeout(() => setMsg(null), 2000);
  }, []);
  const value = useMemo(() => show, [show]);
  return (
    <ToastCtx.Provider value={value}>
      {children}
      {msg && (
        <Animated.View key={msg.id} entering={FadeIn.duration(150)} exiting={FadeOut.duration(150)} style={s.toastWrap} pointerEvents="none">
          <View style={s.toast}>
            <Text style={s.toastText}>{msg.text}</Text>
          </View>
        </Animated.View>
      )}
    </ToastCtx.Provider>
  );
}

export type PressableProps = ComponentProps<typeof Pressable>;

const s = StyleSheet.create({
  tag: { height: 22, paddingHorizontal: 8, borderRadius: R.xs, justifyContent: "center", flexShrink: 0 },
  tagText: { fontSize: F.xs, fontWeight: "600" },
  iconBtn: { width: 44, height: 44, borderRadius: R.md, alignItems: "center", justifyContent: "center" },
  badge: {
    position: "absolute",
    top: 5,
    right: 3,
    minWidth: 18,
    height: 18,
    paddingHorizontal: 4,
    borderRadius: 9,
    backgroundColor: C.primary,
    borderWidth: 2,
    borderColor: C.surface,
    alignItems: "center",
    justifyContent: "center",
  },
  badgeText: { color: "#fff", fontSize: 10, fontWeight: "700" },
  btn: {
    height: 44,
    borderRadius: R.md,
    borderWidth: 1,
    borderColor: C.line,
    backgroundColor: C.surface,
    paddingHorizontal: 16,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
  },
  btnSm: { height: 36, borderRadius: 10, paddingHorizontal: 12 },
  btnText: { fontSize: F.body, fontWeight: "600" },
  header: { height: 52, flexDirection: "row", alignItems: "center", paddingHorizontal: 6, backgroundColor: C.surface },
  headerLine: { borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: C.line },
  headerSide: { minWidth: 44, flexDirection: "row", alignItems: "center" },
  headerTitle: { flex: 1, minWidth: 0, alignItems: "center", paddingHorizontal: 4 },
  headerTitleText: { fontSize: F.title, fontWeight: "700", color: C.ink },
  headerSub: { fontSize: F.xs, color: C.ink3, marginTop: 1 },
  banner: { flexDirection: "row", alignItems: "flex-start", gap: 8, paddingHorizontal: 16, paddingVertical: 10 },
  bannerText: { flex: 1, fontSize: F.sm, lineHeight: 19 },
  bannerAct: { fontSize: F.sm, fontWeight: "700", textDecorationLine: "underline" },
  empty: { alignItems: "center", paddingVertical: 56, paddingHorizontal: 32 },
  emptyIcon: { width: 64, height: 64, borderRadius: 18, backgroundColor: C.line2, alignItems: "center", justifyContent: "center", marginBottom: 14 },
  emptyTitle: { fontSize: F.title, fontWeight: "700", color: C.ink, marginBottom: 4, textAlign: "center" },
  emptyText: { fontSize: F.md, color: C.ink3, textAlign: "center", lineHeight: 21 },
  mask: { position: "absolute", top: 0, right: 0, bottom: 0, left: 0, backgroundColor: C.mask },
  sheet: {
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    maxHeight: "90%",
    backgroundColor: C.surface,
    borderTopLeftRadius: 20,
    borderTopRightRadius: 20,
  },
  grab: { width: 36, height: 5, borderRadius: 3, backgroundColor: "#d6d9e0", alignSelf: "center", marginTop: 8, marginBottom: 2 },
  sheetHead: { flexDirection: "row", alignItems: "center", gap: 8, paddingLeft: 20, paddingRight: 8, paddingTop: 6, paddingBottom: 8 },
  sheetTitle: { fontSize: 17, fontWeight: "700", color: C.ink },
  sheetSub: { fontSize: 12.5, color: C.ink3, marginTop: 1 },
  row: { flexDirection: "row", alignItems: "center", gap: 12, minHeight: 52, paddingHorizontal: 8, paddingVertical: 8, borderRadius: R.md },
  rowTitle: { fontSize: F.body, color: C.ink },
  rowSub: { fontSize: 12.5, color: C.ink3, lineHeight: 18, marginTop: 1 },
  group: { fontSize: F.xs, color: C.ink4, paddingTop: 12, paddingBottom: 4, paddingHorizontal: 8 },
  toastWrap: { position: "absolute", left: 0, right: 0, bottom: 140, alignItems: "center" },
  toast: { backgroundColor: "rgba(15,23,42,0.92)", paddingHorizontal: 16, paddingVertical: 10, borderRadius: R.md, maxWidth: "82%" },
  toastText: { color: "#fff", fontSize: F.md, textAlign: "center" },
});
