/**
 * 对话的文件（从对话头部的文件按钮打开，底部弹出）。
 *
 * ★ 工作目录：按目录树显示，目录默认收起，空目录不显示（接口只列文件，目录从路径推出来，同网页抽屉）。
 * ★ 本机会话：分「改动 / 全部」；打开时经设备网关从电脑上现读，只读；电脑离线时看不到。
 * ★ 点文件进入全屏预览；返回时回到对话，面板还开着（由对话页处理）。
 */
import { useMemo, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import type { Thread } from "@atlas/chat-ui/src/api/types";
import { relativeTime } from "@atlas/chat-ui/src/lib/format";
import { useDeviceChanges, useDeviceDir, useThreadFiles } from "@/api/queries";
import { Button, Empty, Icon, Loading, Sheet } from "@/components/ui";
import { C, F, MONO } from "@/theme";
import { baseName, dirName, FileIcon, fmtSize } from "./look";

interface Node {
  name: string;
  path: string;
  isDir: boolean;
  size?: number;
  time?: string;
  children: Node[];
}

/** 文件路径列表 → 目录树（目录在前；名称里的数字按数值排） */
function buildTree(files: { path: string; size: number; modified_at?: string | null }[]): Node[] {
  const root: Node = { name: "", path: "", isDir: true, children: [] };
  for (const f of files) {
    const parts = f.path.split("/");
    let cur = root;
    parts.forEach((part, i) => {
      const path = parts.slice(0, i + 1).join("/");
      const isFile = i === parts.length - 1;
      let child = cur.children.find((c) => c.name === part && c.isDir === !isFile);
      if (!child) {
        child = { name: part, path, isDir: !isFile, children: [], ...(isFile ? { size: f.size, time: f.modified_at ?? undefined } : {}) };
        cur.children.push(child);
      }
      cur = child;
    });
  }
  const sort = (n: Node) => {
    n.children.sort((a, b) => (a.isDir === b.isDir ? a.name.localeCompare(b.name, "zh-CN", { numeric: true }) : a.isDir ? -1 : 1));
    n.children.forEach(sort);
  };
  sort(root);
  return root.children;
}

export function FilesSheet({
  open,
  onClose,
  thread,
  running,
  onOpenFile,
  onOpenWorkspace,
}: {
  open: boolean;
  onClose: () => void;
  thread: Thread;
  running: boolean;
  onOpenFile: (path: string) => void;
  onOpenWorkspace: () => void;
}) {
  const device = thread.device ?? undefined;
  if (device) {
    return (
      <Sheet open={open} onClose={onClose} title="电脑上的文件" subtitle={<Text style={s.subMono} numberOfLines={1}>{`${device.name} · ${device.project_dir}`}</Text>} tall>
        <DeviceFiles threadId={thread.id} online={device.status === "online"} enabled={open} onOpenFile={onOpenFile} />
      </Sheet>
    );
  }
  return (
    <Sheet
      open={open}
      onClose={onClose}
      title="这个对话的文件"
      subtitle={<Text style={s.subMono} numberOfLines={1}>{thread.workspace_dir}</Text>}
      tall
      footer={
        <View style={{ paddingHorizontal: 16, paddingTop: 8 }}>
          <Button icon="external" onPress={onOpenWorkspace}>
            在工作空间中打开
          </Button>
        </View>
      }
    >
      <WorkspaceFiles threadId={thread.id} running={running} onOpenFile={onOpenFile} />
    </Sheet>
  );
}

function WorkspaceFiles({ threadId, running, onOpenFile }: { threadId: string; running: boolean; onOpenFile: (p: string) => void }) {
  const filesQ = useThreadFiles(threadId, running);
  const tree = useMemo(() => buildTree(filesQ.data?.data ?? []), [filesQ.data]);
  const [openDirs, setOpenDirs] = useState<Set<string>>(new Set());
  if (filesQ.isPending) return <Loading />;
  if (tree.length === 0) return <Empty icon="folder" title="还没有文件">智能体写的文件会出现在这里</Empty>;
  const rows = (nodes: Node[], depth: number): React.ReactNode[] =>
    nodes.flatMap((n) => {
      if (n.isDir) {
        const isOpen = openDirs.has(n.path);
        return [
          <Pressable
            key={`d:${n.path}`}
            style={({ pressed }) => [s.row, { paddingLeft: 8 + depth * 22 }, pressed && { backgroundColor: C.hover }]}
            onPress={() =>
              setOpenDirs((cur) => {
                const next = new Set(cur);
                if (next.has(n.path)) next.delete(n.path);
                else next.add(n.path);
                return next;
              })
            }
          >
            <View style={{ transform: [{ rotate: isOpen ? "90deg" : "0deg" }] }}>
              <Icon name="right" size={16} color={C.ink4} />
            </View>
            <FileIcon path={n.path} dir size={32} />
            <Text style={s.name} numberOfLines={1}>
              {n.name}
            </Text>
            <Text style={s.meta}>{`${n.children.length} 项`}</Text>
          </Pressable>,
          ...(isOpen ? rows(n.children, depth + 1) : []),
        ];
      }
      return [
        <Pressable
          key={`f:${n.path}`}
          style={({ pressed }) => [s.row, { paddingLeft: 8 + depth * 22 + 26 }, pressed && { backgroundColor: C.hover }]}
          onPress={() => onOpenFile(n.path)}
        >
          <FileIcon path={n.path} size={32} />
          <View style={{ flex: 1, minWidth: 0 }}>
            <Text style={s.name} numberOfLines={1}>
              {n.name}
            </Text>
            <Text style={s.meta}>{[fmtSize(n.size), n.time ? relativeTime(n.time) : ""].filter(Boolean).join(" · ")}</Text>
          </View>
        </Pressable>,
      ];
    });
  return <ScrollView contentContainerStyle={{ paddingHorizontal: 8, paddingBottom: 12 }}>{rows(tree, 0)}</ScrollView>;
}

const GIT_COLOR: Record<string, string> = { M: C.warning, A: C.success, D: C.danger, R: C.primary };

function DeviceFiles({ threadId, online, enabled, onOpenFile }: { threadId: string; online: boolean; enabled: boolean; onOpenFile: (p: string) => void }) {
  const [tab, setTab] = useState<"changes" | "all">("changes");
  const changesQ = useDeviceChanges(threadId, enabled && online);
  return (
    <View style={{ flex: 1 }}>
      <View style={s.tabs}>
        {(
          [
            ["changes", `改动${changesQ.data ? ` ${changesQ.data.entries.length}` : ""}`],
            ["all", "全部"],
          ] as const
        ).map(([v, label]) => (
          <Pressable key={v} style={[s.tab, tab === v && s.tabOn]} onPress={() => setTab(v)}>
            <Text style={[s.tabText, tab === v && { color: C.ink, fontWeight: "700" }]}>{label}</Text>
          </Pressable>
        ))}
      </View>
      {!online ? (
        <Empty icon="offline" title="电脑不在线">文件在你的电脑上，平台不保存副本。电脑连上后再打开</Empty>
      ) : tab === "changes" ? (
        changesQ.isPending ? (
          <Loading />
        ) : !changesQ.data || changesQ.data.entries.length === 0 ? (
          <Empty icon="check" title={changesQ.data?.is_repo === false ? "这个目录不是 git 仓库" : "没有未提交的改动"}>
            看全部文件请切到「全部」
          </Empty>
        ) : (
          <ScrollView contentContainerStyle={{ paddingHorizontal: 8, paddingBottom: 12 }}>
            {changesQ.data.entries.map((e) => (
              <Pressable
                key={e.path}
                disabled={e.status === "D"}
                style={({ pressed }) => [s.row, pressed && { backgroundColor: C.hover }]}
                onPress={() => onOpenFile(e.path)}
              >
                <Text style={[s.git, { color: GIT_COLOR[e.status] ?? C.ink3 }]}>{e.status}</Text>
                <FileIcon path={e.path} size={32} />
                <View style={{ flex: 1, minWidth: 0 }}>
                  <Text style={[s.name, e.status === "D" && { textDecorationLine: "line-through", color: C.ink4 }]} numberOfLines={1}>
                    {baseName(e.path)}
                  </Text>
                  <Text style={s.meta} numberOfLines={1}>
                    {dirName(e.path) || "项目根目录"}
                  </Text>
                </View>
              </Pressable>
            ))}
          </ScrollView>
        )
      ) : (
        <ScrollView contentContainerStyle={{ paddingHorizontal: 8, paddingBottom: 12 }}>
          <DeviceDir threadId={threadId} path="" depth={0} onOpenFile={onOpenFile} />
        </ScrollView>
      )}
      <Text style={s.foot}>文件在你的电脑上，打开时现读，只读</Text>
    </View>
  );
}

/** 电脑上的一层目录，展开时才读下一层 */
function DeviceDir({ threadId, path, depth, onOpenFile }: { threadId: string; path: string; depth: number; onOpenFile: (p: string) => void }) {
  const q = useDeviceDir(threadId, path, true);
  const [openDirs, setOpenDirs] = useState<Set<string>>(new Set());
  if (q.isPending) return <Loading style={{ padding: 12 }} />;
  if (q.isError) return <Text style={[s.meta, { paddingLeft: 8 + depth * 22 }]}>{(q.error as Error).message}</Text>;
  const entries = [...(q.data?.entries ?? [])].sort((a, b) =>
    a.is_dir === b.is_dir ? a.name.localeCompare(b.name, "zh-CN", { numeric: true }) : a.is_dir ? -1 : 1,
  );
  return (
    <>
      {entries.map((e) => {
        const p = path ? `${path}/${e.name}` : e.name;
        if (e.is_dir) {
          const isOpen = openDirs.has(p);
          return (
            <View key={p}>
              <Pressable
                style={({ pressed }) => [s.row, { paddingLeft: 8 + depth * 22 }, pressed && { backgroundColor: C.hover }]}
                onPress={() =>
                  setOpenDirs((cur) => {
                    const next = new Set(cur);
                    if (next.has(p)) next.delete(p);
                    else next.add(p);
                    return next;
                  })
                }
              >
                <View style={{ transform: [{ rotate: isOpen ? "90deg" : "0deg" }] }}>
                  <Icon name="right" size={16} color={C.ink4} />
                </View>
                <FileIcon path={p} dir size={32} />
                <Text style={s.name} numberOfLines={1}>
                  {e.name}
                </Text>
              </Pressable>
              {isOpen && <DeviceDir threadId={threadId} path={p} depth={depth + 1} onOpenFile={onOpenFile} />}
            </View>
          );
        }
        return (
          <Pressable
            key={p}
            style={({ pressed }) => [s.row, { paddingLeft: 8 + depth * 22 + 26 }, pressed && { backgroundColor: C.hover }]}
            onPress={() => onOpenFile(p)}
          >
            <FileIcon path={p} size={32} />
            <View style={{ flex: 1, minWidth: 0 }}>
              <Text style={s.name} numberOfLines={1}>
                {e.name}
              </Text>
              <Text style={s.meta}>{fmtSize(e.size)}</Text>
            </View>
          </Pressable>
        );
      })}
      {q.data?.truncated && <Text style={[s.meta, { paddingLeft: 8 + depth * 22 }]}>只列出了一部分</Text>}
    </>
  );
}

const s = StyleSheet.create({
  subMono: { fontFamily: MONO, fontSize: 11.5, color: C.ink3, marginTop: 2 },
  row: { flexDirection: "row", alignItems: "center", gap: 10, minHeight: 50, paddingRight: 8, borderRadius: 10 },
  name: { flex: 1, fontSize: 14.5, color: C.ink },
  meta: { fontSize: 12, color: C.ink3 },
  git: { width: 20, textAlign: "center", fontFamily: MONO, fontWeight: "700", fontSize: F.xs },
  tabs: { flexDirection: "row", gap: 4, padding: 2, backgroundColor: C.hover, borderRadius: 10, marginHorizontal: 16, marginBottom: 8 },
  tab: { flex: 1, height: 34, borderRadius: 8, alignItems: "center", justifyContent: "center" },
  tabOn: { backgroundColor: C.surface, elevation: 1 },
  tabText: { fontSize: F.md, color: C.ink3 },
  foot: { fontSize: F.xs, color: C.ink4, textAlign: "center", paddingVertical: 8 },
});
