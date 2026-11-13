/**
 * 新对话（首页，同网页 HomePage）：发出第一句话就新建对话并跳过去。
 *
 * ★ 默认用平台的默认智能体；点顶部的名字换。都没有时退回第一个能用的智能体。
 * ★ 带 dir（工作空间文件夹上的「在这里新建对话」）：新对话以这个目录为工作目录，建好后不能换。
 * ★ 本机 CLI 智能体：输入框上方选电脑和项目目录，建对话时定下，之后不能换。
 * ★ 添加的文件在会话建好之后再上传（会话之前没有工作目录），消息里附上它们的路径。
 */
import { useNavigation, useRouter } from "expo-router";
import { DrawerActions } from "expo-router/react-navigation";
import { useCallback, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useSendMessage } from "@atlas/chat-ui/src/api/runs";
import type { ApprovalMode } from "@atlas/chat-ui/src/lib/events";
import { newIdempotencyKey } from "@atlas/chat-ui/src/lib/id";
import {
  uploadThreadFile,
  useAgent,
  useAgents,
  useCreateThread,
  useDefaultAgent,
  useMe,
  withAttachments,
  type AgentSpecView,
} from "@/api/queries";
import { Avatar, Empty, Header, Icon, IconButton, Loading, useToast } from "@/components/ui";
import { Composer, ContextChip, type Attachment } from "@/features/chat/Composer";
import { AgentSheet, DeviceSheet, ModelPill, ModePill, useDevicePick, type DevicePick } from "@/features/chat/pickers";
import { useSession } from "@/lib/session";
import { C, F } from "@/theme";

function greeting(): string {
  const h = new Date().getHours();
  return h < 6 ? "夜深了" : h < 11 ? "早上好" : h < 14 ? "中午好" : h < 18 ? "下午好" : "晚上好";
}

/** 快捷问题：点一下填进输入框，不直接发 */
const QUICK = ["整理本周的工作周报", "分析一份表格数据", "帮我写一个脚本", "查一下公司的报销制度"];

export function Home({ dir, onDropDir }: { dir: string; onDropDir: () => void }) {
  const router = useRouter();
  const navigation = useNavigation();
  const insets = useSafeAreaInsets();
  const toast = useToast();
  const { client } = useSession();
  const meQ = useMe();
  const defaultQ = useDefaultAgent();
  const agentsQ = useAgents();
  const createThread = useCreateThread();
  const sendMessage = useSendMessage();

  const [picked, setPicked] = useState<string>();
  const [mode, setMode] = useState<ApprovalMode | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [fill, setFill] = useState<{ text: string; seq: number }>();
  const [agentsOpen, setAgentsOpen] = useState(false);
  const [devOpen, setDevOpen] = useState(false);
  const [devicePick, setDevicePick] = useState<DevicePick>({});
  const onDevicePick = useCallback((p: DevicePick) => setDevicePick(p), []);

  const agents = agentsQ.data?.data ?? [];
  const defaultId = defaultQ.data?.id;
  const agentId = picked ?? defaultId ?? agents[0]?.id;
  const agentQ = useAgent(agentId);
  const agent = agents.find((a) => a.id === agentId) ?? agentQ.data;
  const spec = agentQ.data?.spec as AgentSpecView | undefined;
  const isCli = spec?.kind === "cli";
  // 本机 CLI：跑在使用者自己的电脑上
  const deviceCli = isCli && spec?.cli?.placement === "device" ? spec.cli : undefined;
  const approval = isCli ? undefined : spec?.approval;
  const devices = useDevicePick(deviceCli?.cli_type, devicePick, onDevicePick);

  const send = async (text: string, files: Attachment[]) => {
    if (!agentId) return;
    if (deviceCli && !devicePick.deviceId) {
      toast("没有可用的电脑：先在电脑版「我的设备」里配对，并在电脑上启动连接器");
      throw new Error("no device");
    }
    try {
      const thread = await createThread.mutateAsync({
        agent_id: agentId,
        title: "",
        workspace_dir: dir || undefined,
        ...(deviceCli ? { device_id: devicePick.deviceId, project_dir: devicePick.dir } : {}),
      });
      const paths = await Promise.all(files.map((f) => uploadThreadFile(client, thread.id, f)));
      await sendMessage.mutateAsync({
        threadId: thread.id,
        content: [{ type: "text", text: withAttachments(text, paths) }],
        idempotencyKey: newIdempotencyKey(),
        approvalMode: mode ?? undefined,
        model: model ?? undefined,
      });
      onDropDir();
      router.navigate({ pathname: "/chat/[threadId]", params: { threadId: thread.id } });
    } catch (e) {
      toast((e as Error).message);
      throw e;
    }
  };

  const header = (
    <Header
      left={<IconButton name="menu" label="菜单" onPress={() => navigation.dispatch(DrawerActions.openDrawer())} />}
      onTitlePress={agents.length > 0 ? () => setAgentsOpen(true) : undefined}
      title={
        agent ? (
          <View style={s.agentTitle}>
            <Avatar name={agent.name} avatarKey={agent.avatar_key} size={22} />
            <Text style={s.agentName} numberOfLines={1}>
              {agent.name}
            </Text>
            <Icon name="down" size={16} color={C.ink3} />
          </View>
        ) : undefined
      }
      right={<View style={{ width: 44 }} />}
    />
  );

  if (defaultQ.isPending || agentsQ.isPending) {
    return (
      <View style={{ flex: 1, backgroundColor: C.surface, paddingTop: insets.top }}>
        {header}
        <Loading style={{ flex: 1, justifyContent: "center" }} />
      </View>
    );
  }
  if (!agentId) {
    return (
      <View style={{ flex: 1, backgroundColor: C.surface, paddingTop: insets.top }}>
        {header}
        <Empty icon="spark" title="还没有可以对话的智能体">请管理员在电脑版里设置默认智能体，或者在电脑上创建一个</Empty>
      </View>
    );
  }

  const device = devices.device;
  const context = (
    <>
      {dir ? <ContextChip icon="folder" tone="dir" label={`在「${dir}」里工作`} onRemove={onDropDir} /> : null}
      {deviceCli ? (
        <ContextChip
          icon="laptop"
          tone="dev"
          label={device ? `${device.name} · ${devicePick.dir ? devicePick.dir : "新目录"}` : devices.loading ? "正在找电脑…" : "没有可用的电脑"}
          onPress={() => setDevOpen(true)}
        />
      ) : null}
    </>
  );

  return (
    <View style={{ flex: 1, backgroundColor: C.surface, paddingTop: insets.top }}>
      {header}
      <KeyboardAvoidingView behavior="padding" style={{ flex: 1 }}>
        <ScrollView contentContainerStyle={s.hero} keyboardShouldPersistTaps="handled">
          <Text style={s.h1}>{`${greeting()}，${meQ.data?.name ?? ""}`}</Text>
          {agent?.description ? <Text style={s.desc}>{`我是${agent.name}，${agent.description}`}</Text> : null}
        </ScrollView>
        <ScrollView horizontal showsHorizontalScrollIndicator={false} style={{ flexGrow: 0 }} contentContainerStyle={s.quick} keyboardShouldPersistTaps="handled">
          {QUICK.map((q) => (
            <Pressable key={q} style={s.quickItem} onPress={() => setFill({ text: q, seq: Date.now() })}>
              <Text style={s.quickText}>{q}</Text>
            </Pressable>
          ))}
        </ScrollView>
        <View style={[s.foot, { paddingBottom: insets.bottom + 8 }]}>
          <Composer
            placeholder={dir ? "想让智能体用这个目录里的文件做什么？" : `给${agent?.name ?? "智能体"}发消息`}
            running={false}
            onSend={send}
            fill={fill}
            context={dir || deviceCli ? context : undefined}
            note={deviceCli && device ? `在 ${device.name} 上运行` : undefined}
            toolbar={
              <>
                {approval && (
                  <ModePill value={mode ?? approval.default_mode} allowed={approval.allowed_modes} onChange={setMode} />
                )}
                {spec?.model && !isCli && agentId && (
                  <ModelPill agentId={agentId} value={model ?? spec.model.model} defaultModel={spec.model.model} onChange={setModel} />
                )}
              </>
            }
          />
        </View>
      </KeyboardAvoidingView>
      <AgentSheet
        open={agentsOpen}
        onClose={() => setAgentsOpen(false)}
        agents={agents}
        value={agentId}
        defaultId={defaultId}
        onChange={(id) => {
          setPicked(id);
          setMode(null);
          setModel(null);
          setDevicePick({});
        }}
      />
      {deviceCli && (
        <DeviceSheet
          open={devOpen}
          onClose={() => setDevOpen(false)}
          cliType={deviceCli.cli_type}
          defaultProjectDir={deviceCli.default_project_dir ?? ""}
          choices={devices.choices}
          value={devicePick}
          onChange={setDevicePick}
        />
      )}
    </View>
  );
}

const s = StyleSheet.create({
  agentTitle: { flexDirection: "row", alignItems: "center", gap: 6, maxWidth: "100%" },
  agentName: { fontSize: F.title, fontWeight: "700", color: C.ink, flexShrink: 1 },
  hero: { flexGrow: 1, justifyContent: "center", paddingHorizontal: 24, paddingVertical: 24 },
  h1: { fontSize: F.h1, fontWeight: "700", color: C.ink, marginBottom: 6, lineHeight: 34 },
  desc: { fontSize: F.body, color: C.ink3, lineHeight: 23 },
  quick: { gap: 8, paddingHorizontal: 12, paddingBottom: 10 },
  quickItem: { height: 38, paddingHorizontal: 14, borderRadius: 19, borderWidth: 1, borderColor: C.line, justifyContent: "center" },
  quickText: { fontSize: F.md, color: C.ink2 },
  foot: { paddingHorizontal: 12, paddingTop: 2 },
});
