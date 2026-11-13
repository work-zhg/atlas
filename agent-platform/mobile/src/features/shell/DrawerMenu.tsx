/**
 * 侧边菜单：只有三样 —— 新对话、工作空间、最近对话。
 * 智能体管理、知识库、技能、MCP、模型、个性化、系统都不放进手机（底部账户里有「打开电脑版」）。
 *
 * ★ 最近对话的状态点：绿色 = 运行中，橙色 = 等你确认（审批或提问）。手机上最常做的事就是「回来批一下」。
 * ★ 本机 CLI 对话后面有电脑图标。
 */
import type { Drawer } from "expo-router/drawer";
import { useRouter } from "expo-router";
import * as Linking from "expo-linking";
import { useMemo, useState, type ComponentProps } from "react";
import { FlatList, Pressable, StyleSheet, Text, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import type { Thread } from "@atlas/chat-ui/src/api/types";
import { useMe, useThreads } from "@/api/queries";
import { Avatar, Icon, Loading, Sheet, SheetRow, type IconName } from "@/components/ui";
import { useSession } from "@/lib/session";
import { C, F, R } from "@/theme";

/** 抽屉传给自定义内容的 props（expo-router 没有单独导出这个类型） */
type DrawerContentComponentProps = Parameters<NonNullable<ComponentProps<typeof Drawer>["drawerContent"]>>[0];

const RUNNING = new Set(["queued", "running", "suspended"]);

function stateOf(t: Thread): "wait" | "run" | null {
  const r = t.last_run;
  if (!r || !RUNNING.has(r.status)) return null;
  return r.needs_approval ? "wait" : "run";
}

function groupOf(iso: string): string {
  const d = new Date(iso);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const day = 86_400_000;
  if (d.getTime() >= today.getTime()) return "今天";
  if (d.getTime() >= today.getTime() - day) return "昨天";
  if (d.getTime() >= today.getTime() - 6 * day) return "7 天内";
  if (d.getTime() >= today.getTime() - 29 * day) return "30 天内";
  return "更早";
}

type Row = { kind: "group"; key: string; label: string } | { kind: "thread"; key: string; thread: Thread };

export function DrawerMenu({ state, navigation }: DrawerContentComponentProps) {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const { server, logout } = useSession();
  const meQ = useMe();
  const threadsQ = useThreads();
  const [q, setQ] = useState("");
  const [account, setAccount] = useState(false);

  // 当前在哪一页（高亮用）
  const route = state.routes[state.index];
  const current = route?.name;
  const currentThread = (route?.params as { threadId?: string } | undefined)?.threadId;

  const rows = useMemo<Row[]>(() => {
    const all = threadsQ.data?.pages.flatMap((p) => p.data) ?? [];
    const k = q.trim().toLowerCase();
    const list = k ? all.filter((t) => (t.title || "新对话").toLowerCase().includes(k)) : all;
    const out: Row[] = [];
    let last = "";
    for (const t of list) {
      const g = groupOf(t.updated_at);
      if (g !== last) {
        out.push({ kind: "group", key: `g:${g}`, label: g });
        last = g;
      }
      out.push({ kind: "thread", key: t.id, thread: t });
    }
    return out;
  }, [threadsQ.data, q]);

  const go = (fn: () => void) => {
    navigation.closeDrawer();
    fn();
  };
  const nav = (icon: IconName, label: string, on: boolean, onPress: () => void) => (
    <Pressable style={[s.nav, on && s.navOn]} onPress={onPress}>
      <Icon name={icon} size={20} color={on ? C.primaryInk : C.ink2} />
      <Text style={[s.navText, on && { color: C.primaryInk }]}>{label}</Text>
    </Pressable>
  );

  return (
    <View style={{ flex: 1, paddingTop: insets.top + 8, backgroundColor: C.surface }}>
      <View style={s.search}>
        <Icon name="search" size={18} color={C.ink4} />
        <TextInput style={s.searchInput} value={q} onChangeText={setQ} placeholder="搜索对话" placeholderTextColor={C.ink4} />
      </View>
      {nav("newChat", "新对话", current === "index", () => go(() => router.navigate("/")))}
      {nav("folder", "工作空间", current === "workspace", () => go(() => router.navigate("/workspace")))}
      <View style={s.divider} />
      {threadsQ.isPending ? (
        <Loading />
      ) : (
        <FlatList
          data={rows}
          keyExtractor={(r) => r.key}
          style={{ flex: 1 }}
          contentContainerStyle={{ paddingHorizontal: 8, paddingBottom: 12 }}
          onEndReached={() => threadsQ.hasNextPage && !threadsQ.isFetchingNextPage && void threadsQ.fetchNextPage()}
          onEndReachedThreshold={0.5}
          ListEmptyComponent={<Text style={s.empty}>{q ? "没有找到" : "还没有对话"}</Text>}
          renderItem={({ item: r }) => {
            if (r.kind === "group") return <Text style={s.group}>{r.label}</Text>;
            const t = r.thread;
            const st = stateOf(t);
            const on = current === "chat/[threadId]" && currentThread === t.id;
            return (
              <Pressable
                style={({ pressed }) => [s.item, on && s.itemOn, pressed && { backgroundColor: C.hover }]}
                onPress={() => go(() => router.navigate({ pathname: "/chat/[threadId]", params: { threadId: t.id } }))}
              >
                {st && <View style={[s.dot, st === "run" ? s.dotRun : s.dotWait]} />}
                <Text style={[s.itemText, on && { color: C.ink, fontWeight: "700" }]} numberOfLines={1}>
                  {t.title || "新对话"}
                </Text>
                {t.device ? <Icon name="laptop" size={15} color={C.ink4} /> : null}
              </Pressable>
            );
          }}
        />
      )}
      <Pressable style={[s.foot, { paddingBottom: insets.bottom + 10 }]} onPress={() => setAccount(true)}>
        <Avatar name={meQ.data?.name ?? "我"} color={C.primary2} size={34} />
        <View style={{ flex: 1 }}>
          <Text style={{ fontWeight: "700", color: C.ink, fontSize: F.body }}>{meQ.data?.name ?? ""}</Text>
          {meQ.data?.dept ? <Text style={{ fontSize: F.xs, color: C.ink3 }}>{meQ.data.dept}</Text> : null}
        </View>
        <Icon name="more" size={20} color={C.ink3} />
      </Pressable>
      <Sheet open={account} onClose={() => setAccount(false)} title={meQ.data?.name ?? "账户"} subtitle={server.replace(/^https?:\/\//, "")}>
        <View style={{ paddingHorizontal: 12, paddingBottom: 8 }}>
          <SheetRow
            icon="monitor"
            title="打开电脑版"
            sub="智能体、知识库、个性化等在电脑版里"
            onPress={() => {
              setAccount(false);
              void Linking.openURL(`${server}/atlas`);
            }}
          />
          <SheetRow
            icon="logout"
            title="退出登录"
            danger
            onPress={() => {
              setAccount(false);
              void logout();
            }}
          />
        </View>
      </Sheet>
    </View>
  );
}

const s = StyleSheet.create({
  search: { flexDirection: "row", alignItems: "center", gap: 8, marginHorizontal: 12, marginBottom: 6, height: 40, borderRadius: R.md, backgroundColor: C.hover, paddingHorizontal: 12 },
  searchInput: { flex: 1, fontSize: F.input, color: C.ink, paddingVertical: 0 },
  nav: { flexDirection: "row", alignItems: "center", gap: 12, height: 48, marginHorizontal: 8, paddingHorizontal: 12, borderRadius: R.md },
  navOn: { backgroundColor: C.primaryWeak },
  navText: { fontSize: 15.5, fontWeight: "600", color: C.ink },
  divider: { height: StyleSheet.hairlineWidth, backgroundColor: C.line, marginTop: 8 },
  group: { fontSize: F.xs, color: C.ink4, paddingTop: 14, paddingBottom: 4, paddingHorizontal: 12 },
  item: { flexDirection: "row", alignItems: "center", gap: 8, minHeight: 44, paddingHorizontal: 12, borderRadius: R.md },
  itemOn: { backgroundColor: "#eef0f6" },
  itemText: { flex: 1, fontSize: F.body, color: C.ink2 },
  dot: { width: 8, height: 8, borderRadius: 4 },
  dotRun: { backgroundColor: C.success },
  dotWait: { backgroundColor: C.warning },
  empty: { textAlign: "center", color: C.ink4, paddingVertical: 24, fontSize: F.sm },
  foot: { flexDirection: "row", alignItems: "center", gap: 10, paddingHorizontal: 16, paddingTop: 10, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: C.line },
});
