/**
 * 对话页（同网页 chat-ui 的 ThreadConversation + Atlas 的 ChatWorkbench）。
 *
 * ★ 订阅**会话**的事件流（useThreadStream）：切到后台再回来、断线重连都靠它按序号续传。
 * ★ 哪几条消息要画、实时区让不让位，规则在 chat-ui 的 lib/conversation.ts，两端共用。
 * ★ 本机 CLI 会话：头部和设备栏显示电脑在线状态；断线时不报错、不设超时，提示「连上后接着显示」；
 *   电脑离线且没在跑时，输入框换成说明。
 */
import { useFocusEffect, useNavigation, useRouter } from "expo-router";
import { DrawerActions } from "expo-router/react-navigation";
import { useCallback, useEffect, useMemo, useRef, useState, type ComponentRef, type ReactNode } from "react";
import { Modal, Pressable, ScrollView, StyleSheet, Text, TextInput, View, type NativeScrollEvent, type NativeSyntheticEvent } from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";
import { SafeAreaView, useSafeAreaInsets } from "react-native-safe-area-context";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { qk } from "@atlas/chat-ui/src/api/keys";
import { useCancelRun, useSendMessage } from "@atlas/chat-ui/src/api/runs";
import { flattenMessages, useMessages, useThread } from "@atlas/chat-ui/src/api/threads";
import type { Thread } from "@atlas/chat-ui/src/api/types";
import { useThreadStream } from "@atlas/chat-ui/src/chat/useThreadStream";
import { useAtlasHttp } from "@atlas/chat-ui/src/client";
import { planConversation } from "@atlas/chat-ui/src/lib/conversation";
import type { ApprovalMode } from "@atlas/chat-ui/src/lib/events";
import { compactNumber, durationMs } from "@atlas/chat-ui/src/lib/format";
import { newIdempotencyKey } from "@atlas/chat-ui/src/lib/id";
import { isStreamDone, type RunState, type SubagentRun, type ToolCall } from "@atlas/chat-ui/src/lib/run-reducer";
import { uploadThreadFile, useDeleteThread, useDeviceChanges, useRenameThread, useThreadFiles, withAttachments } from "@/api/queries";
import { Avatar, Banner, Button, Header, Icon, IconButton, Loading, Sheet, SheetRow, Tag, useToast } from "@/components/ui";
import { FilesSheet } from "@/features/files/FilesSheet";
import { useSession } from "@/lib/session";
import { C, F, MONO } from "@/theme";
import { Composer, type Attachment } from "./Composer";
import { ChatCtx, findSubagent, type ChatCtxValue } from "./context";
import { ModelPill, ModePill, MessageActions } from "./pickers";
import { StepSheet } from "./Steps";
import { AssistantTurnBody, TurnEnd, TurnTimeline } from "./Timeline";

export function Conversation({ threadId }: { threadId: string }) {
  const router = useRouter();
  const navigation = useNavigation();
  const toast = useToast();
  const insets = useSafeAreaInsets();
  const { client } = useSession();
  const http = useAtlasHttp();
  const qc = useQueryClient();

  const threadQ = useThread(threadId);
  const thread = threadQ.data;
  const device = thread?.device ?? undefined;
  // 本机会话：电脑在不在线跟着变（重新连上后自动恢复，不用刷新）
  useQuery({
    queryKey: qk.threads.detail(threadId),
    queryFn: () => http.get<Thread>(`/v1/threads/${threadId}`),
    enabled: Boolean(device),
    refetchInterval: 15_000,
  });
  const messagesQ = useMessages(threadId);
  const messages = useMemo(() => flattenMessages(messagesQ.data?.pages), [messagesQ.data]);
  const sendMessage = useSendMessage();
  const cancelRun = useCancelRun();
  const { state, reconnecting, streamError } = useThreadStream({ threadId });
  const activeRunId = state.activeRunId;
  const running = Boolean(activeRunId) && !isStreamDone(state.status);
  const live = activeRunId ? state : undefined;
  const liveRef = useRef(live);
  liveRef.current = live;
  const plan = planConversation(messages, live, activeRunId);

  // ── 文件（数字 = 工作目录里的文件数；本机会话 = 还没提交的改动数）
  const deviceOnline = device?.status === "online";
  const filesQ = useThreadFiles(threadId, running, !device);
  const changesQ = useDeviceChanges(threadId, Boolean(device) && deviceOnline);
  const fileCount = device ? changesQ.data?.entries.length ?? 0 : filesQ.data?.data.length ?? 0;
  const filePaths = useMemo(() => new Set(filesQ.data?.data.map((f) => f.path) ?? []), [filesQ.data]);
  const resolveFile = useCallback(
    (toolPath: string) => {
      const rel = toolPath.replace(/^\/+/, "");
      return filePaths.has(rel) ? rel : null;
    },
    [filePaths],
  );

  // ── 审批模式、模型：用户在选择器里选了、还没发出去的；null = 沿用会话记住的
  const approval = thread?.approval;
  const [pickedMode, setPickedMode] = useState<ApprovalMode | null>(null);
  useEffect(() => setPickedMode(null), [threadId, approval?.mode]);
  const modelChoice = thread?.model_choice;
  const [pickedModel, setPickedModel] = useState<string | null>(null);
  useEffect(() => setPickedModel(null), [threadId, thread?.model]);
  // 计划获批时 server 改了会话的模式：一轮结束后刷新会话，选择器跟着变
  const wasRunning = useRef(running);
  useEffect(() => {
    if (wasRunning.current && !running) void qc.invalidateQueries({ queryKey: qk.threads.detail(threadId) });
    wasRunning.current = running;
  }, [running, threadId, qc]);

  // ── 发送
  const handleSend = useCallback(
    async (text: string, files: Attachment[]) => {
      let paths: string[] = [];
      try {
        paths = await Promise.all(files.map((f) => uploadThreadFile(client, threadId, f)));
      } catch (e) {
        toast(`文件上传失败，消息没有发出：${(e as Error).message}`);
        throw e;
      }
      try {
        await sendMessage.mutateAsync({
          threadId,
          content: [{ type: "text", text: withAttachments(text, paths) }],
          idempotencyKey: newIdempotencyKey(),
          approvalMode: pickedMode ?? undefined,
          model: pickedModel ?? undefined,
        });
      } catch (e) {
        toast(`发送失败：${(e as Error).message}`);
        throw e;
      }
    },
    [client, threadId, sendMessage, pickedMode, pickedModel, toast],
  );
  const lastQuestion = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i]!;
      if (m.role !== "user") continue;
      const text = m.content.filter((b) => b.type === "text" && typeof b.text === "string").map((b) => b.text as string).join("");
      if (text) return text;
    }
    return undefined;
  }, [messages]);
  const onRegenerate = lastQuestion && !running ? () => void handleSend(lastQuestion, []).catch(() => undefined) : undefined;

  // ── 面板与跳转
  const [filesOpen, setFilesOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [step, setStep] = useState<ToolCall | null>(null);
  // 打开的子智能体过程。实时区里的跟着实时状态走（还在跑）；历史轮次的用打开时那份轨迹（不会再变）
  const [subRun, setSubRun] = useState<{ run: SubagentRun; subId?: string; root: RunState; live: boolean } | null>(null);
  // 从文件面板打开的预览，返回时面板要还开着
  const reopenFiles = useRef(false);
  useFocusEffect(
    useCallback(() => {
      if (reopenFiles.current) {
        reopenFiles.current = false;
        setFilesOpen(true);
      }
    }, []),
  );
  const openFile = useCallback(
    (path: string, fromSheet = false) => {
      // ★ Modal 盖在所有页面上面：跳转前先关掉，否则预览页会被挡住
      reopenFiles.current = fromSheet;
      setFilesOpen(false);
      setStep(null);
      setSubRun(null);
      router.push({ pathname: "/preview", params: { src: threadId, path } });
    },
    [router, threadId],
  );
  const ctx = useMemo<ChatCtxValue>(
    () => ({
      threadId,
      resolveFile,
      openFile: (p) => openFile(p),
      openStep: setStep,
      openSubRun: (run, subId, root) => setSubRun({ run, subId, root, live: root === liveRef.current }),
    }),
    [threadId, resolveFile, openFile],
  );

  // ── 自动贴底：只在用户已经接近底部时
  const scrollRef = useRef<ComponentRef<typeof ScrollView>>(null);
  const nearBottom = useRef(true);
  const firstLayout = useRef(true);
  const onScroll = (e: NativeSyntheticEvent<NativeScrollEvent>) => {
    const { contentOffset, contentSize, layoutMeasurement } = e.nativeEvent;
    nearBottom.current = contentSize.height - contentOffset.y - layoutMeasurement.height < 160;
  };
  const onContentSizeChange = () => {
    if (firstLayout.current || nearBottom.current) {
      scrollRef.current?.scrollToEnd({ animated: !firstLayout.current });
      if (messages.length > 0) firstLayout.current = false;
    }
  };

  // ── 头部状态：审批优先（用户要动手），其次是挂起（在等子智能体），最后是运行中
  const status = !running
    ? null
    : state.pendingApprovals.some((a) => a.kind === "question")
      ? { label: "等你回答", color: C.warning }
      : state.pendingApprovals.length > 0
        ? { label: "等你确认", color: C.warning }
        : state.status === "suspended"
          ? { label: "等待子智能体", color: C.primary }
          : { label: "运行中", color: C.success };
  const deviceDown = Boolean(device) && device!.status !== "online" && device!.status !== "unknown";

  const agentName = thread?.agent_name ?? "智能体";
  const isCli = Boolean(modelChoice && !modelChoice.switchable);

  return (
    <ChatCtx.Provider value={ctx}>
      <View style={{ flex: 1, backgroundColor: C.surface, paddingTop: insets.top }}>
        <Header
          line
          left={<IconButton name="menu" label="菜单" onPress={() => navigation.dispatch(DrawerActions.openDrawer())} />}
          onTitlePress={thread ? () => setMenuOpen(true) : undefined}
          title={
            <View style={{ flexDirection: "row", alignItems: "center", gap: 4, maxWidth: "100%" }}>
              <Text style={s.title} numberOfLines={1}>
                {thread ? thread.title || "新对话" : "加载中…"}
              </Text>
              {thread && <Icon name="down" size={16} color={C.ink3} />}
            </View>
          }
          subtitle={
            thread ? (
              <View style={{ flexDirection: "row", alignItems: "center", gap: 5 }}>
                <Text style={s.sub} numberOfLines={1}>
                  {agentName}
                </Text>
                {device && (
                  <>
                    <Text style={s.sub}>·</Text>
                    <View style={[s.dot, { backgroundColor: deviceDown ? C.ink4 : C.success }]} />
                    <Text style={s.sub} numberOfLines={1}>
                      {device.name}
                    </Text>
                  </>
                )}
                {status && (
                  <>
                    <Text style={s.sub}>·</Text>
                    <Text style={[s.sub, { color: status.color }]}>{status.label}</Text>
                  </>
                )}
              </View>
            ) : undefined
          }
          right={
            <>
              <IconButton name="folder" label="文件" badge={fileCount} onPress={() => setFilesOpen(true)} />
              <IconButton name="newChat" label="新对话" onPress={() => router.navigate("/")} />
            </>
          }
        />
        {device && (
          <View style={s.devbar}>
            <Icon name="laptop" size={16} color={C.ink3} />
            <Text style={s.devName}>{device.name}</Text>
            <Text style={s.devDir} numberOfLines={1}>
              {device.project_dir}
            </Text>
            <Tag tone={deviceDown ? "default" : "success"}>{deviceDown ? "离线" : "在线"}</Tag>
          </View>
        )}
        {device && deviceDown && running && (
          <Banner tone="warn" icon="clock">
            {`和 ${device.name} 的连接断了。这一轮还在你的电脑上进行，连上后接着显示。`}
          </Banner>
        )}

        <KeyboardAvoidingView behavior="padding" style={{ flex: 1 }}>
          <ScrollView
            ref={scrollRef}
            style={{ flex: 1 }}
            contentContainerStyle={s.msgs}
            onScroll={onScroll}
            scrollEventThrottle={100}
            onContentSizeChange={onContentSizeChange}
            keyboardShouldPersistTaps="handled"
          >
            {messagesQ.isPending && <Loading />}
            {messagesQ.hasNextPage && (
              <View style={{ alignItems: "center", paddingVertical: 8 }}>
                <Button small loading={messagesQ.isFetchingNextPage} onPress={() => void messagesQ.fetchNextPage()}>
                  加载更早的消息
                </Button>
              </View>
            )}
            {plan.entries.map((e) =>
              e.kind === "user" ? (
                <View key={e.message.id} style={s.userRow}>
                  <Text selectable style={s.bubble}>
                    {e.text}
                  </Text>
                </View>
              ) : (
                <View key={e.message.id} style={s.ai}>
                  <Who name={agentName} avatarKey={thread?.agent_avatar_key} />
                  <AssistantTurnBody
                    message={e.message}
                    blocks={e.blocks}
                    traceEnabled={e.traceEnabled}
                    fallback={e.useLiveFallback ? live : undefined}
                  />
                  <MessageActions
                    threadId={threadId}
                    messageId={e.feedbackMessageId}
                    text={e.blocks.join("\n\n")}
                    onRegenerate={e.canRegenerate ? onRegenerate : undefined}
                  />
                </View>
              ),
            )}
            {plan.showLive && live && (
              <View style={s.ai}>
                <Who name={agentName} avatarKey={thread?.agent_avatar_key} extra={reconnecting ? <Tag tone="warning">重新连接中…</Tag> : undefined} />
                {live.meta?.unsupported_tools && live.meta.unsupported_tools.length > 0 && (
                  <Text style={s.warnText}>{`以下工具本期未接入，本轮不会被调用：${live.meta.unsupported_tools.join("、")}`}</Text>
                )}
                <TurnTimeline state={live} streaming={!isStreamDone(live.status)} />
                {live.status === "running" && <Typing />}
                {live.status === "suspended" && live.pendingApprovals.length === 0 && (
                  <Text style={s.note}>正在等待子智能体完成，这一轮还没结束</Text>
                )}
                <TurnEnd state={live} />
              </View>
            )}
            {streamError && (
              <View style={s.err}>
                <Icon name="alert" size={18} color={C.dangerInk} />
                <Text style={{ flex: 1, color: C.dangerInk }}>{`事件流连接失败：${streamError.message}`}</Text>
              </View>
            )}
          </ScrollView>

          <View style={[s.foot, { paddingBottom: insets.bottom + 8 }]}>
            {device && deviceDown && !running ? (
              <View style={s.locked}>
                <Icon name="offline" size={18} color={C.ink3} />
                <Text style={s.lockedText}>
                  {`${device.name} 不在线，暂时不能继续这个对话。在电脑上启动连接器（`}
                  <Text style={{ fontFamily: MONO }}>atlas-connect run</Text>
                  {"）后自动恢复，不用刷新。"}
                </Text>
              </View>
            ) : (
              <Composer
                placeholder={`回复${agentName}`}
                running={running}
                onSend={handleSend}
                onCancel={() => activeRunId && cancelRun.mutate(activeRunId)}
                note={device ? `在 ${device.name} 上运行` : undefined}
                toolbar={
                  <>
                    {approval && (
                      <ModePill
                        value={pickedMode ?? (approval.mode as ApprovalMode)}
                        allowed={approval.allowed_modes as ApprovalMode[]}
                        onChange={setPickedMode}
                        disabled={running}
                      />
                    )}
                    {modelChoice && !isCli && thread && (
                      <ModelPill
                        agentId={thread.agent_id}
                        value={pickedModel ?? modelChoice.model}
                        defaultModel={modelChoice.default_model}
                        onChange={setPickedModel}
                        disabled={running}
                      />
                    )}
                  </>
                }
              />
            )}
          </View>
        </KeyboardAvoidingView>
      </View>

      {thread && (
        <FilesSheet
          open={filesOpen}
          onClose={() => setFilesOpen(false)}
          thread={thread}
          running={running}
          onOpenFile={(p) => openFile(p, true)}
          onOpenWorkspace={() => {
            setFilesOpen(false);
            router.push({ pathname: "/folder", params: { dir: thread.workspace_dir } });
          }}
        />
      )}
      <StepSheet call={step} onClose={() => setStep(null)} />
      {thread && <ThreadMenu open={menuOpen} onClose={() => setMenuOpen(false)} thread={thread} usage={state.contextUsage} />}
      {subRun && (
        <SubRunPage
          runId={subRun.run.runId}
          fallbackRun={subRun.run}
          subId={subRun.subId}
          root={subRun.live && live ? live : subRun.root}
          onClose={() => setSubRun(null)}
        />
      )}
    </ChatCtx.Provider>
  );
}

function Who({ name, avatarKey, extra }: { name: string; avatarKey?: string; extra?: ReactNode }) {
  return (
    <View style={s.who}>
      <Avatar name={name} avatarKey={avatarKey} size={22} />
      <Text style={s.whoName}>{name}</Text>
      {extra}
    </View>
  );
}

function Typing() {
  return (
    <View style={{ flexDirection: "row", gap: 4, paddingVertical: 6 }}>
      {[0, 1, 2].map((i) => (
        <View key={i} style={[s.typingDot, { opacity: 0.35 + i * 0.2 }]} />
      ))}
    </View>
  );
}

// ── 对话标题的菜单：重命名、上下文用量、工作目录、删除

function ThreadMenu({
  open,
  onClose,
  thread,
  usage,
}: {
  open: boolean;
  onClose: () => void;
  thread: Thread;
  usage?: RunState["contextUsage"];
}) {
  const router = useRouter();
  const toast = useToast();
  const rename = useRenameThread(thread.id);
  const del = useDeleteThread();
  const [mode, setMode] = useState<"menu" | "rename" | "delete">("menu");
  const [title, setTitle] = useState(thread.title);
  useEffect(() => {
    if (open) {
      setMode("menu");
      setTitle(thread.title);
    }
  }, [open, thread.title]);
  const pct = usage && usage.effective_window > 0 ? Math.min(100, Math.round((usage.tokens / usage.effective_window) * 100)) : null;

  if (mode === "rename") {
    return (
      <Sheet
        open={open}
        onClose={onClose}
        title="重命名"
        footer={
          <View style={s.sheetBtns}>
            <Button style={{ flex: 1 }} onPress={onClose}>
              取消
            </Button>
            <Button
              kind="primary"
              style={{ flex: 1 }}
              loading={rename.isPending}
              disabled={!title.trim()}
              onPress={() =>
                rename.mutate(title.trim(), {
                  onSuccess: () => {
                    toast("已保存");
                    onClose();
                  },
                  onError: (e) => toast((e as Error).message),
                })
              }
            >
              保存
            </Button>
          </View>
        }
      >
        <View style={{ paddingHorizontal: 20 }}>
          <TextInput style={s.input} value={title} onChangeText={setTitle} autoFocus maxLength={200} />
        </View>
      </Sheet>
    );
  }
  if (mode === "delete") {
    return (
      <Sheet
        open={open}
        onClose={onClose}
        title="删除这个对话？"
        footer={
          <View style={s.sheetBtns}>
            <Button style={{ flex: 1 }} onPress={onClose}>
              取消
            </Button>
            <Button
              kind="solidDanger"
              style={{ flex: 1 }}
              loading={del.isPending}
              onPress={() =>
                del.mutate(thread.id, {
                  onSuccess: () => {
                    onClose();
                    toast("已删除对话");
                    router.navigate("/");
                  },
                  onError: (e) => toast((e as Error).message),
                })
              }
            >
              删除
            </Button>
          </View>
        }
      >
        <Text style={{ paddingHorizontal: 20, color: C.ink3, fontSize: F.md, lineHeight: 21 }}>
          对话记录删除后不能恢复。工作目录里的文件不删，还在工作空间里。
        </Text>
      </Sheet>
    );
  }
  return (
    <Sheet open={open} onClose={onClose} title={thread.title || "新对话"} subtitle={thread.agent_name}>
      <View style={{ paddingHorizontal: 12, paddingBottom: 8 }}>
        <SheetRow icon="pencil" title="重命名" onPress={() => setMode("rename")} />
        <SheetRow
          icon="gauge"
          title="上下文用量"
          sub={
            pct !== null && usage ? (
              <View>
                <Text style={s.rowSub}>{`${pct}% · ${compactNumber(usage.tokens)} / ${compactNumber(usage.effective_window)} tokens，满了会自动压缩早期内容`}</Text>
                <View style={s.bar}>
                  <View style={[s.barFill, { width: `${pct}%` }]} />
                </View>
              </View>
            ) : (
              "发一条消息后显示"
            )
          }
        />
        {!thread.device && (
          <SheetRow
            icon="folder"
            title="工作目录"
            sub={<Text style={[s.rowSub, { fontFamily: MONO }]} numberOfLines={1}>{thread.workspace_dir}</Text>}
            right={<Icon name="right" size={18} color={C.ink4} />}
            onPress={() => {
              onClose();
              router.push({ pathname: "/folder", params: { dir: thread.workspace_dir } });
            }}
          />
        )}
        <SheetRow icon="trash" title="删除对话" danger onPress={() => setMode("delete")} />
      </View>
    </Sheet>
  );
}

// ── 子智能体的过程（单独一页；只能看，要追问回主对话）

function SubRunPage({
  runId,
  fallbackRun,
  subId,
  root,
  onClose,
}: {
  runId: string;
  fallbackRun: SubagentRun;
  subId?: string;
  root: RunState;
  onClose: () => void;
}) {
  const insets = useSafeAreaInsets();
  const run = findSubagent(root, runId) ?? fallbackRun;
  const sub = subId ? root.subRuns[subId] : undefined;
  const statusText = run.status === "running" ? "执行中" : run.status === "ok" ? `已完成 · ${durationMs(run.durationMs)}` : "失败";
  return (
    <Modal visible animationType="slide" onRequestClose={onClose} statusBarTranslucent navigationBarTranslucent>
      <SafeAreaView style={{ flex: 1, backgroundColor: C.surface }} edges={["top"]}>
        <Header
          line
          left={<IconButton name="back" label="返回" onPress={onClose} />}
          title={`${run.name} 的过程`}
          subtitle={`子智能体 · ${statusText}`}
        />
        <ScrollView contentContainerStyle={[s.msgs, { paddingBottom: insets.bottom + 24 }]}>
          {run.task ? (
            <View style={s.card}>
              <Text style={s.cardLabel}>委派任务</Text>
              <Text selectable style={s.cardText}>
                {run.task}
              </Text>
            </View>
          ) : null}
          {sub ? (
            <View style={s.ai}>
              <Who name={run.name} />
              <TurnTimeline state={sub} root={root} streaming={!isStreamDone(sub.status)} />
              {sub.status === "running" && <Typing />}
              <TurnEnd state={sub} />
            </View>
          ) : (
            <Text style={s.note}>过程已经看不到了（记录已清理，或不在这次加载的范围里）</Text>
          )}
          {run.result ? (
            <View style={s.card}>
              <Text style={s.cardLabel}>返回结果</Text>
              <Text selectable style={s.cardText}>
                {run.result}
              </Text>
            </View>
          ) : null}
          <Text style={[s.note, { textAlign: "center" }]}>子智能体的过程只能看；要追问，回到主对话里说</Text>
        </ScrollView>
      </SafeAreaView>
    </Modal>
  );
}

const s = StyleSheet.create({
  title: { fontSize: F.title, fontWeight: "700", color: C.ink, flexShrink: 1 },
  sub: { fontSize: F.xs, color: C.ink3 },
  dot: { width: 6, height: 6, borderRadius: 3 },
  devbar: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingHorizontal: 16,
    paddingVertical: 8,
    backgroundColor: C.surface2,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: C.line,
  },
  devName: { fontSize: 12.5, color: C.ink2, fontWeight: "600" },
  devDir: { flex: 1, fontFamily: MONO, fontSize: 11.5, color: C.ink3 },
  msgs: { paddingHorizontal: 16, paddingTop: 12, paddingBottom: 16 },
  userRow: { alignItems: "flex-end", marginTop: 4, marginBottom: 20 },
  bubble: {
    backgroundColor: C.bubble,
    paddingHorizontal: 14,
    paddingVertical: 10,
    borderRadius: 20,
    borderBottomRightRadius: 6,
    maxWidth: "86%",
    fontSize: F.body,
    lineHeight: 23,
    color: C.ink,
  },
  ai: { marginBottom: 18 },
  who: { flexDirection: "row", alignItems: "center", gap: 8, marginBottom: 8 },
  whoName: { fontSize: F.sm, fontWeight: "600", color: C.ink2 },
  warnText: { fontSize: F.sm, color: C.warningInk, marginBottom: 10 },
  note: { fontSize: F.sm, color: C.ink3, marginVertical: 8 },
  err: { flexDirection: "row", gap: 10, backgroundColor: C.dangerWeak, borderRadius: 14, padding: 12, marginBottom: 12 },
  typingDot: { width: 7, height: 7, borderRadius: 4, backgroundColor: C.ink4 },
  foot: { paddingHorizontal: 12, paddingTop: 6, backgroundColor: C.surface },
  locked: {
    flexDirection: "row",
    gap: 10,
    borderWidth: 1,
    borderStyle: "dashed",
    borderColor: C.line,
    borderRadius: 16,
    padding: 12,
    backgroundColor: C.surface2,
  },
  lockedText: { flex: 1, fontSize: 13.5, color: C.ink3, lineHeight: 20 },
  sheetBtns: { flexDirection: "row", gap: 8, paddingHorizontal: 16, paddingTop: 8 },
  input: { height: 46, borderWidth: 1, borderColor: C.line, borderRadius: 12, paddingHorizontal: 12, fontSize: F.input, color: C.ink },
  rowSub: { fontSize: 12.5, color: C.ink3, lineHeight: 18 },
  bar: { height: 6, borderRadius: 3, backgroundColor: C.line, overflow: "hidden", marginTop: 8 },
  barFill: { height: 6, borderRadius: 3, backgroundColor: C.primary },
  card: { borderWidth: 1, borderColor: C.line, borderRadius: 16, padding: 14, marginBottom: 12 },
  cardLabel: { fontSize: F.xs, color: C.ink3, marginBottom: 4 },
  cardText: { fontSize: F.md, color: C.ink, lineHeight: 21 },
});
