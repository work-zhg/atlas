/**
 * 文件预览（全屏一页）。src = "ws"（工作空间）或会话 id（这个会话的工作目录；本机会话是电脑上的项目目录）。
 *
 * ★ 内容一律从预览站点读（GET /v1/previews/{令牌}/…），令牌在路径里，WebView / 图片带不了身份头也能看。
 * ★ 用哪种方式显示查 chat-ui 的 previewKind（与网页同一张表）。
 * ★ PDF 用原生渲染（react-native-pdf）：Android 的 WebView 不能直接显示 PDF。
 * ★ 图片放进一个可缩放的网页里：双指缩放、双击放大由系统处理。
 * ★ HTML 在 WebView 里运行：站点响应头带 CSP sandbox（不透明 origin），页面拿不到登录信息。
 * ★ 会话还在跑时文件可能在变：每 10 秒核对一次版本，变了提示「已更新」，点了才刷新（同网页 §09）。
 */
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import { useMemo, useState } from "react";
import { FlatList, Pressable, ScrollView, StyleSheet, Switch, Text, View } from "react-native";
import Pdf from "react-native-pdf";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import WebView from "react-native-webview";

import { useThread } from "@atlas/chat-ui/src/api/threads";
import { relativeTime } from "@atlas/chat-ui/src/lib/format";
import { extOf, IMAGE_MAX_BYTES, previewKind, TEXT_MAX_BYTES } from "@atlas/chat-ui/src/lib/previewKind";
import { previewFileUrl, useDeviceDir, usePreviewSession, useThreadFiles, useWorkspaceDir } from "@/api/queries";
import { Markdown } from "@/components/Markdown";
import { Banner, Button, Empty, Header, IconButton, Loading, Sheet, SheetRow, useToast } from "@/components/ui";
import { C, F, MONO } from "@/theme";
import { openWithOtherApp, saveToDevice, shareFile } from "./actions";
import { baseName, dirName, fmtSize, lookOf } from "./look";

interface Meta {
  size?: number;
  /** 版本：变了就是文件被改了 */
  version?: string;
  time?: string;
}

/** 文件的大小和版本：从所在目录的列表里取（工作空间 / 会话文件 / 电脑上的目录） */
function useMeta(src: string, path: string, isDevice: boolean, running: boolean): Meta | undefined {
  const isWs = src === "ws";
  const wsDir = useWorkspaceDir(isWs ? dirName(path) : "");
  const threadFiles = useThreadFiles(!isWs && !isDevice ? src : undefined, running);
  const devDir = useDeviceDir(src, dirName(path), !isWs && isDevice);
  if (isWs) {
    const f = wsDir.data?.files.find((x) => x.path === path);
    return f ? { size: f.size, version: f.etag ?? f.modified_at ?? undefined, time: f.modified_at ?? undefined } : undefined;
  }
  if (isDevice) {
    const f = devDir.data?.entries.find((x) => x.name === baseName(path));
    return f ? { size: f.size, version: `${f.size}:${f.modified_at}` } : undefined;
  }
  const f = threadFiles.data?.data.find((x) => x.path === path);
  return f ? { size: f.size, version: f.etag ?? f.modified_at ?? undefined, time: f.modified_at ?? undefined } : undefined;
}

interface PreviewText {
  text: string;
  truncated: boolean;
  total: number;
  binary: boolean;
}

/** 读文件开头的 maxBytes 字节（Range）。小文件 S3 返回 200 而不是 206，两种都接受（同网页） */
async function fetchPreviewText(url: string, maxBytes: number): Promise<PreviewText> {
  const res = await fetch(url, { headers: { Range: `bytes=0-${maxBytes - 1}` } });
  if (!res.ok) throw new Error(res.status === 410 ? "预览已过期，返回再打开一次" : res.status === 404 ? "文件已不存在" : `读取失败（${res.status}）`);
  const bytes = new Uint8Array(await res.arrayBuffer());
  const range = res.headers.get("Content-Range");
  const total = range ? Number(range.split("/")[1]) : bytes.length;
  const binary = bytes.subarray(0, 8192).includes(0);
  let text = binary ? "" : new TextDecoder("utf-8").decode(bytes);
  const truncated = total > bytes.length;
  if (truncated && text.endsWith("�")) text = text.slice(0, -1);
  return { text, truncated, total: Number.isFinite(total) ? total : bytes.length, binary };
}

export function Preview({ src, path }: { src: string; path: string }) {
  const router = useRouter();
  const toast = useToast();
  const insets = useSafeAreaInsets();
  const isWs = src === "ws";
  const threadQ = useThread(isWs ? undefined : src);
  const device = threadQ.data?.device ?? undefined;
  const isDevice = Boolean(device);
  const running = Boolean(threadQ.data?.active_run_id);
  const sessionQ = usePreviewSession(src);
  const meta = useMeta(src, path, isDevice, running && !isWs);
  // 打开时的版本；之后版本变了就提示
  const [seen, setSeen] = useState<string | undefined>();
  if (seen === undefined && meta?.version) setSeen(meta.version);
  const updated = Boolean(seen && meta?.version && meta.version !== seen);
  const [reloadKey, setReloadKey] = useState(0);

  const url = sessionQ.data ? previewFileUrl(sessionQ.data.base_url, path) : undefined;
  const kind = previewKind(path, meta?.size);
  const [mode, setMode] = useState<"view" | "source">("view");
  const [wrap, setWrap] = useState(false);
  const [more, setMore] = useState(false);
  const [busy, setBusy] = useState(false);

  const needText = kind === "text" || kind === "unknown" || ((kind === "markdown" || kind === "html") && (kind === "markdown" || mode === "source"));
  const textQ = useQuery({
    queryKey: ["preview-text", url, reloadKey],
    queryFn: () => fetchPreviewText(url!, TEXT_MAX_BYTES),
    enabled: Boolean(url) && needText,
    staleTime: Infinity,
  });

  const act = async (fn: () => Promise<unknown>, ok?: string) => {
    if (!url) return;
    setMore(false);
    setBusy(true);
    try {
      const r = await fn();
      if (ok) toast(typeof r === "string" ? `${ok}：${r}` : ok);
    } catch (e) {
      const msg = (e as Error).message;
      // 用户在系统面板里取消了，不算错误
      if (!/cancel/i.test(msg)) toast(msg);
    } finally {
      setBusy(false);
    }
  };

  const metaText = isDevice
    ? `${device!.name} · 只读${meta?.size !== undefined ? ` · ${fmtSize(meta.size)}` : ""}`
    : [fmtSize(meta?.size), meta?.time ? relativeTime(meta.time) : ""].filter(Boolean).join(" · ");
  const offline = isDevice && device!.status !== "online" && device!.status !== "unknown";
  const [look0, look1, look2] = lookOf(path);

  let body: React.ReactNode;
  if (offline) {
    body = <Empty icon="offline" title={`${device!.name} 不在线`}>文件在你的电脑上，平台不保存副本。电脑连上后再打开</Empty>;
  } else if (sessionQ.isError) {
    body = <Empty icon="alert" title="打不开">{(sessionQ.error as Error).message}</Empty>;
  } else if (!url) {
    body = <Loading />;
  } else if (kind === "empty") {
    body = <Empty icon="file" title="空文件">这个文件没有内容</Empty>;
  } else if (kind === "image") {
    body =
      meta?.size !== undefined && meta.size > IMAGE_MAX_BYTES ? (
        <Empty icon="image" title="图片太大，不在这里显示">{`有 ${fmtSize(meta.size)}，可以分享或用其他应用打开`}</Empty>
      ) : (
        <WebView
          key={reloadKey}
          style={{ flex: 1, backgroundColor: "#0f1115" }}
          originWhitelist={["*"]}
          source={{ html: imageHtml(url), baseUrl: url }}
          setBuiltInZoomControls
          setDisplayZoomControls={false}
        />
      );
  } else if (kind === "pdf") {
    body = <PdfView url={url} key={reloadKey} />;
  } else if (kind === "html" && mode === "view") {
    body = <WebView key={reloadKey} style={{ flex: 1 }} source={{ uri: url }} scalesPageToFit setBuiltInZoomControls setDisplayZoomControls={false} />;
  } else if (textQ.isPending) {
    body = <Loading />;
  } else if (textQ.isError) {
    body = <Empty icon="alert" title="读不出来">{(textQ.error as Error).message}</Empty>;
  } else if (textQ.data && (kind === "unknown" && textQ.data.binary)) {
    body = (
      <Empty icon={look0} iconBg={look1} iconColor={look2} title="这种文件不能在线预览" footer={
        <View style={{ flexDirection: "row", gap: 8, marginTop: 18 }}>
          <Button icon="share" onPress={() => void act(() => shareFile(url, path))}>分享</Button>
          <Button kind="primary" icon="external" onPress={() => void act(() => openWithOtherApp(url, path))}>用其他应用打开</Button>
        </View>
      }>
        {`${extOf(path).toUpperCase()} 文件可以用手机上的其他应用打开`}
      </Empty>
    );
  } else if (textQ.data) {
    const d = textQ.data;
    body = (
      <View style={{ flex: 1 }}>
        {d.truncated && (
          <Banner tone="warn" icon="alert" action={{ label: "分享", onPress: () => void act(() => shareFile(url, path)) }}>
            {`文件有 ${fmtSize(d.total)}，只显示前 1 MB`}
          </Banner>
        )}
        {kind === "markdown" && mode === "view" ? (
          <ScrollView contentContainerStyle={{ padding: 20, paddingBottom: insets.bottom + 40 }}>
            <Markdown>{d.text}</Markdown>
          </ScrollView>
        ) : (
          <CodeView text={d.text} wrap={wrap} bottom={insets.bottom} />
        )}
      </View>
    );
  }

  const bar =
    kind === "markdown" || kind === "html" ? (
      <View style={s.pvbar}>
        <View style={s.segm}>
          {(
            [
              ["view", kind === "html" ? "运行" : "预览"],
              ["source", "源码"],
            ] as const
          ).map(([v, label]) => (
            <Pressable key={v} style={[s.segmItem, mode === v && s.segmOn]} onPress={() => setMode(v)}>
              <Text style={[s.segmText, mode === v && { color: C.ink, fontWeight: "700" }]}>{label}</Text>
            </Pressable>
          ))}
        </View>
        {kind === "html" && <Text style={s.barHint}>沙箱运行</Text>}
      </View>
    ) : kind === "text" ? (
      <View style={s.pvbar}>
        <Text style={s.barHint}>{extOf(path).toUpperCase()}</Text>
        <View style={{ flex: 1 }} />
        <Text style={s.barLabel}>自动换行</Text>
        <Switch value={wrap} onValueChange={setWrap} trackColor={{ true: C.primary, false: "#cbd0da" }} thumbColor="#fff" />
      </View>
    ) : null;

  return (
    <View style={{ flex: 1, backgroundColor: C.surface, paddingTop: insets.top }}>
      <Header
        line
        left={<IconButton name="back" label="返回" onPress={() => router.back()} />}
        title={baseName(path)}
        subtitle={metaText || undefined}
        right={<IconButton name="more" label="更多" onPress={() => setMore(true)} disabled={!url || busy} />}
      />
      {bar}
      {updated && (
        <Banner
          tone="info"
          icon="refresh"
          action={{
            label: "刷新",
            onPress: () => {
              setSeen(meta?.version);
              setReloadKey((k) => k + 1);
            },
          }}
        >
          文件已更新
        </Banner>
      )}
      <View style={{ flex: 1 }}>{body}</View>
      {busy && (
        <View style={s.busy}>
          <Loading />
        </View>
      )}
      <Sheet open={more} onClose={() => setMore(false)} title={baseName(path)}>
        <View style={{ paddingHorizontal: 12, paddingBottom: 8 }}>
          <SheetRow icon="share" title="分享" sub="把文件发给微信、邮件等" onPress={() => void act(() => shareFile(url!, path))} />
          <SheetRow icon="external" title="用其他应用打开" onPress={() => void act(() => openWithOtherApp(url!, path))} />
          <SheetRow icon="download" title="保存到手机" sub="选一个文件夹存进去" onPress={() => void act(() => saveToDevice(url!, path), "已保存到")} />
          {isWs && (
            <SheetRow
              icon="chat"
              title="在所在目录新建对话"
              onPress={() => {
                setMore(false);
                router.navigate({ pathname: "/", params: { dir: dirName(path) } });
              }}
            />
          )}
          {!isWs && !isDevice && threadQ.data && (
            <SheetRow
              icon="folder"
              title="在工作空间中查看"
              onPress={() => {
                setMore(false);
                const full = `${threadQ.data!.workspace_dir}/${path}`;
                router.push({ pathname: "/folder", params: { dir: dirName(full) } });
              }}
            />
          )}
        </View>
      </Sheet>
    </View>
  );
}

function imageHtml(url: string): string {
  const safe = url.replace(/"/g, "&quot;");
  return `<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=8,user-scalable=yes">
<style>html,body{margin:0;height:100%;background:#0f1115}body{display:flex;align-items:center;justify-content:center}img{max-width:100%;max-height:100%}</style>
</head><body><img src="${safe}"></body></html>`;
}

function PdfView({ url }: { url: string }) {
  const [page, setPage] = useState<{ cur: number; total: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  if (err) return <Empty icon="alert" title="PDF 打不开">{err}</Empty>;
  return (
    <View style={{ flex: 1, backgroundColor: "#e9ebf0" }}>
      <Pdf
        source={{ uri: url, cache: true }}
        style={{ flex: 1, backgroundColor: "#e9ebf0" }}
        trustAllCerts={false}
        onLoadComplete={(total) => setPage({ cur: 1, total })}
        onPageChanged={(cur, total) => setPage({ cur, total })}
        onError={(e) => setErr(String((e as Error)?.message ?? e))}
      />
      {page && (
        <View style={s.pageInd} pointerEvents="none">
          <Text style={{ color: "#fff", fontSize: F.sm }}>{`${page.cur} / ${page.total}`}</Text>
        </View>
      )}
    </View>
  );
}

/** 代码 / 文本：带行号、等宽；默认不换行（横向滑动），可以打开自动换行 */
function CodeView({ text, wrap, bottom }: { text: string; wrap: boolean; bottom: number }) {
  const lines = useMemo(() => text.replace(/\r\n/g, "\n").split("\n"), [text]);
  const gutter = String(lines.length).length * 8 + 16;
  // 不换行时按最长一行估一个宽度（等宽字体，中文按两个字符算）
  const width = useMemo(() => {
    let max = 0;
    for (const l of lines) {
      let w = 0;
      for (const ch of l.slice(0, 1000)) w += ch.charCodeAt(0) > 0x2e80 ? 2 : 1;
      if (w > max) max = w;
    }
    return gutter + Math.max(max, 20) * 7.6 + 24;
  }, [lines, gutter]);
  const list = (
    <FlatList
      data={lines}
      keyExtractor={(_, i) => String(i)}
      initialNumToRender={60}
      windowSize={11}
      contentContainerStyle={{ paddingTop: 8, paddingBottom: bottom + 40 }}
      renderItem={({ item, index }) => (
        <View style={s.line}>
          <Text style={[s.ln, { width: gutter }]}>{index + 1}</Text>
          <Text selectable style={[s.lc, !wrap && { flexShrink: 0 }]}>
            {item || " "}
          </Text>
        </View>
      )}
    />
  );
  if (wrap) return list;
  return (
    <ScrollView horizontal contentContainerStyle={{ width }} showsHorizontalScrollIndicator={false}>
      {list}
    </ScrollView>
  );
}

const s = StyleSheet.create({
  pvbar: { flexDirection: "row", alignItems: "center", gap: 8, paddingHorizontal: 12, paddingVertical: 6, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: C.line },
  segm: { flexDirection: "row", padding: 2, backgroundColor: C.hover, borderRadius: 10 },
  segmItem: { height: 32, paddingHorizontal: 14, borderRadius: 8, alignItems: "center", justifyContent: "center" },
  segmOn: { backgroundColor: C.surface, elevation: 1 },
  segmText: { fontSize: 13.5, color: C.ink3 },
  barHint: { fontSize: F.xs, color: C.ink3, marginLeft: "auto" },
  barLabel: { fontSize: F.sm, color: C.ink2 },
  line: { flexDirection: "row" },
  ln: { fontFamily: MONO, fontSize: 12.5, lineHeight: 22, color: C.ink4, textAlign: "right", paddingRight: 12 },
  lc: { flex: 1, fontFamily: MONO, fontSize: 12.5, lineHeight: 22, color: C.ink, paddingRight: 16 },
  pageInd: {
    position: "absolute",
    bottom: 24,
    alignSelf: "center",
    backgroundColor: "rgba(15,23,42,0.78)",
    paddingHorizontal: 14,
    paddingVertical: 5,
    borderRadius: 14,
  },
  busy: { position: "absolute", top: 0, right: 0, bottom: 0, left: 0, backgroundColor: "rgba(255,255,255,0.6)", justifyContent: "center" },
});
