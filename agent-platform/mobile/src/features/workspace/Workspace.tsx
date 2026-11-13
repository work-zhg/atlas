/**
 * 工作空间（只看）：一个人的全部文件，普通的目录树（同网页 /workspace，不按对话分）。
 *
 * ★ 手机上只看不改：浏览、搜索、预览、分享、保存，以及在文件夹里新建对话；
 *   上传、新建文件夹、重命名、移动、删除留在电脑上。
 * ★ 根目录在侧边菜单里（左上是菜单）；子目录逐层推进来（左上是返回），Android 返回键回上一级。
 * ★ 目录在前；按名称排时数字按数值排，也可以按修改时间。隐藏文件（.atlas 等）不显示。
 */
import { useNavigation, useRouter } from "expo-router";
import { DrawerActions } from "expo-router/react-navigation";
import { Directory, File, Paths } from "expo-file-system";
import * as Sharing from "expo-sharing";
import { useMemo, useState } from "react";
import { FlatList, Pressable, RefreshControl, ScrollView, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { relativeTime } from "@atlas/chat-ui/src/lib/format";
import { apiUrl, toApiError } from "@atlas/chat-ui/src/lib/http";
import { previewFileUrl, usePreviewSession, useWorkspaceDir, useWorkspaceOverview } from "@/api/queries";
import { Empty, Header, Icon, IconButton, Loading, Sheet, SheetRow, useToast } from "@/components/ui";
import { openWithOtherApp, saveToDevice, shareFile } from "@/features/files/actions";
import { baseName, dirName, FileIcon, fmtSize } from "@/features/files/look";
import { useSession } from "@/lib/session";
import { C, F } from "@/theme";

interface Entry {
  name: string;
  path: string;
  isDir: boolean;
  size: number;
  time?: string | null;
  /** 文件夹：里面有几个文件 */
  files?: number;
}

export function Workspace({ dir }: { dir: string }) {
  const router = useRouter();
  const navigation = useNavigation();
  const insets = useSafeAreaInsets();
  const toast = useToast();
  const { client } = useSession();
  const dirQ = useWorkspaceDir(dir);
  const overviewQ = useWorkspaceOverview(!dir);
  const previewQ = usePreviewSession("ws");
  const [sort, setSort] = useState<"name" | "time">("name");
  const [menu, setMenu] = useState<Entry | null>(null);
  const [busy, setBusy] = useState(false);

  const entries = useMemo<Entry[]>(() => {
    const d = dirQ.data;
    if (!d) return [];
    const cmp = (a: Entry, b: Entry) =>
      sort === "time" ? (b.time ?? "").localeCompare(a.time ?? "") : a.name.localeCompare(b.name, "zh-CN", { numeric: true });
    const dirs = d.dirs.map((x) => ({ name: x.name, path: x.path, isDir: true, size: x.size, time: x.modified_at, files: x.files }));
    const files = d.files.map((x) => ({ name: x.name, path: x.path, isDir: false, size: x.size, time: x.modified_at }));
    return [...dirs.sort(cmp), ...files.sort(cmp)];
  }, [dirQ.data, sort]);

  const segs = dir ? dir.split("/") : [];
  const ov = overviewQ.data;
  const quotaPct = ov && ov.quota_bytes > 0 ? Math.min(100, (ov.used_bytes / ov.quota_bytes) * 100) : null;

  const open = (e: Entry) =>
    e.isDir ? router.push({ pathname: "/folder", params: { dir: e.path } }) : router.push({ pathname: "/preview", params: { src: "ws", path: e.path } });

  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setMenu(null);
    setBusy(true);
    try {
      const r = await fn();
      if (ok) toast(typeof r === "string" ? `${ok}：${r}` : ok);
    } catch (e) {
      const msg = (e as Error).message;
      if (!/cancel/i.test(msg)) toast(msg);
    } finally {
      setBusy(false);
    }
  };
  const fileUrl = (path: string) => (previewQ.data ? previewFileUrl(previewQ.data.base_url, path) : undefined);
  /** 文件夹打包下载：走接口（要登录），下到缓存再交给分享面板 */
  const shareZip = async (path: string) => {
    const res = await fetch(apiUrl("/v1/workspace/zip", { path }, client.apiBase), { headers: client.headers?.() ?? {} });
    if (!res.ok) throw new Error((await toApiError(res)).message);
    const cache = new Directory(Paths.cache, "files");
    if (!cache.exists) cache.create({ intermediates: true });
    const target = new File(cache, `${baseName(path) || "工作空间"}.zip`);
    await target.write(new Uint8Array(await res.arrayBuffer()));
    await Sharing.shareAsync(target.uri, { mimeType: "application/zip" });
  };

  const header = dir ? (
    <Header
      left={<IconButton name="back" label="返回" onPress={() => (router.canGoBack() ? router.back() : router.navigate("/workspace"))} />}
      title={segs[segs.length - 1]}
      subtitle={segs.length > 1 ? segs.slice(0, -1).join(" / ") : "工作空间"}
      right={<IconButton name="search" label="搜索" onPress={() => router.push("/search")} />}
    />
  ) : (
    <Header
      left={<IconButton name="menu" label="菜单" onPress={() => navigation.dispatch(DrawerActions.openDrawer())} />}
      title="工作空间"
      right={<IconButton name="search" label="搜索" onPress={() => router.push("/search")} />}
    />
  );

  return (
    <View style={{ flex: 1, backgroundColor: C.surface, paddingTop: insets.top }}>
      {header}
      {dir ? (
        <ScrollView horizontal showsHorizontalScrollIndicator={false} style={s.crumbs} contentContainerStyle={{ alignItems: "center", paddingHorizontal: 10 }}>
          <Pressable onPress={() => router.navigate("/workspace")} hitSlop={6}>
            <Text style={s.crumb}>工作空间</Text>
          </Pressable>
          {segs.map((seg, i) => (
            <View key={i} style={{ flexDirection: "row", alignItems: "center" }}>
              <Text style={s.crumbSep}>/</Text>
              <Pressable
                hitSlop={6}
                disabled={i === segs.length - 1}
                onPress={() => router.push({ pathname: "/folder", params: { dir: segs.slice(0, i + 1).join("/") } })}
              >
                <Text style={[s.crumb, i === segs.length - 1 && s.crumbCur]}>{seg}</Text>
              </Pressable>
            </View>
          ))}
        </ScrollView>
      ) : null}
      {dirQ.isPending ? (
        <Loading />
      ) : dirQ.isError ? (
        <Empty icon="alert" title="打不开">{(dirQ.error as Error).message}</Empty>
      ) : (
        <FlatList
          data={entries}
          keyExtractor={(e) => `${e.isDir ? "d" : "f"}:${e.path}`}
          refreshControl={<RefreshControl refreshing={dirQ.isRefetching} onRefresh={() => void dirQ.refetch()} colors={[C.primary]} />}
          contentContainerStyle={{ paddingBottom: insets.bottom + (dir ? 96 : 24) }}
          ListHeaderComponent={
            <>
              {!dir && quotaPct !== null && ov ? (
                <View style={s.quota}>
                  <View style={{ flexDirection: "row" }}>
                    <Text style={{ fontSize: F.sm, fontWeight: "700", color: C.ink }}>{`已用 ${fmtSize(ov.used_bytes)}`}</Text>
                    <Text style={{ marginLeft: "auto", fontSize: F.xs, color: C.ink3 }}>{`共 ${fmtSize(ov.quota_bytes)}`}</Text>
                  </View>
                  <View style={s.bar}>
                    <View style={[s.barFill, { width: `${quotaPct}%` }]} />
                  </View>
                </View>
              ) : null}
              {entries.length > 0 && (
                <View style={s.sortRow}>
                  <Text style={{ fontSize: F.xs, color: C.ink3 }}>{`${entries.length} 项${dirQ.data?.truncated ? "（只列出了一部分）" : ""}`}</Text>
                  <Pressable style={s.sortBtn} onPress={() => setSort(sort === "name" ? "time" : "name")} hitSlop={8}>
                    <Icon name="sort" size={15} />
                    <Text style={{ fontSize: F.sm, color: C.ink2 }}>{sort === "name" ? "按名称" : "按修改时间"}</Text>
                  </Pressable>
                </View>
              )}
            </>
          }
          ListEmptyComponent={
            <Empty icon="folder" title={dir ? "这个文件夹是空的" : "还没有文件"}>
              {dir ? "在这里新建对话，智能体生成的文件会放进来" : "开始一个对话，智能体生成的文件都会出现在这里"}
            </Empty>
          }
          renderItem={({ item: e }) => (
            <Pressable style={({ pressed }) => [s.row, pressed && { backgroundColor: C.hover }]} onPress={() => open(e)}>
              <FileIcon path={e.path} dir={e.isDir} />
              <View style={s.rowMain}>
                <Text style={s.name} numberOfLines={1}>
                  {e.name}
                </Text>
                <Text style={s.meta} numberOfLines={1}>
                  {[e.isDir ? `${e.files ?? 0} 个文件` : fmtSize(e.size), e.time ? relativeTime(e.time) : ""].filter(Boolean).join(" · ")}
                </Text>
              </View>
              <IconButton name="more" label="更多" color={C.ink3} onPress={() => setMenu(e)} />
            </Pressable>
          )}
        />
      )}
      {dir ? (
        <Pressable style={[s.fab, { bottom: insets.bottom + 20 }]} onPress={() => router.navigate({ pathname: "/", params: { dir } })}>
          <Icon name="chat" size={18} color="#fff" />
          <Text style={s.fabText}>在这里新建对话</Text>
        </Pressable>
      ) : null}
      {busy && (
        <View style={s.busy}>
          <Loading />
        </View>
      )}
      <Sheet open={Boolean(menu)} onClose={() => setMenu(null)} title={menu?.name} subtitle={menu ? dirName(menu.path) || "工作空间" : undefined}>
        {menu && (
          <View style={{ paddingHorizontal: 12, paddingBottom: 8 }}>
            {menu.isDir ? (
              <>
                <SheetRow
                  icon="chat"
                  title="在这个目录新建对话"
                  sub="新对话以它为工作目录，建好后不能换"
                  onPress={() => {
                    setMenu(null);
                    router.navigate({ pathname: "/", params: { dir: menu.path } });
                  }}
                />
                <SheetRow icon="share" title="打包分享（zip）" onPress={() => void run(() => shareZip(menu.path))} />
              </>
            ) : (
              <>
                <SheetRow icon="eye" title="预览" onPress={() => { setMenu(null); open(menu); }} />
                <SheetRow icon="share" title="分享" sub="发给微信、邮件等，不生成公开链接" onPress={() => { const u = fileUrl(menu.path); if (u) void run(() => shareFile(u, menu.path)); }} />
                <SheetRow icon="external" title="用其他应用打开" onPress={() => { const u = fileUrl(menu.path); if (u) void run(() => openWithOtherApp(u, menu.path)); }} />
                <SheetRow icon="download" title="保存到手机" onPress={() => { const u = fileUrl(menu.path); if (u) void run(() => saveToDevice(u, menu.path), "已保存到"); }} />
              </>
            )}
            <Text style={s.foot}>重命名、移动、删除、上传请到电脑上</Text>
          </View>
        )}
      </Sheet>
    </View>
  );
}

const s = StyleSheet.create({
  crumbs: { flexGrow: 0, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: C.line, paddingBottom: 6 },
  crumb: { fontSize: 13.5, color: C.ink3, paddingHorizontal: 6, paddingVertical: 4 },
  crumbCur: { color: C.ink, fontWeight: "700" },
  crumbSep: { color: C.ink4, fontSize: 13.5 },
  quota: { marginHorizontal: 16, marginTop: 8, marginBottom: 4, padding: 14, borderRadius: 14, backgroundColor: C.surface2, borderWidth: 1, borderColor: C.line2 },
  bar: { height: 6, borderRadius: 3, backgroundColor: C.line, overflow: "hidden", marginTop: 8 },
  barFill: { height: 6, borderRadius: 3, backgroundColor: C.primary },
  sortRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", paddingHorizontal: 16, paddingTop: 8, paddingBottom: 2 },
  sortBtn: { flexDirection: "row", alignItems: "center", gap: 4, paddingVertical: 6 },
  row: { flexDirection: "row", alignItems: "center", gap: 12, minHeight: 64, paddingLeft: 16, paddingRight: 4 },
  rowMain: { flex: 1, minWidth: 0, alignSelf: "stretch", justifyContent: "center", borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: C.line2 },
  name: { fontSize: F.body, color: C.ink },
  meta: { fontSize: 12.5, color: C.ink3, marginTop: 1 },
  fab: {
    position: "absolute",
    right: 16,
    height: 50,
    paddingHorizontal: 20,
    borderRadius: 25,
    backgroundColor: C.primary,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    elevation: 6,
    shadowColor: C.primary,
    shadowOpacity: 0.35,
    shadowRadius: 12,
    shadowOffset: { width: 0, height: 8 },
  },
  fabText: { color: "#fff", fontWeight: "700", fontSize: F.body },
  foot: { fontSize: F.xs, color: C.ink4, textAlign: "center", paddingVertical: 10 },
  busy: { position: "absolute", top: 0, right: 0, bottom: 0, left: 0, backgroundColor: "rgba(255,255,255,0.6)", justifyContent: "center" },
});
