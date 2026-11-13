/**
 * 输入框里的选择：执行前确认、模型、智能体、电脑和项目目录；回答下面的操作（复制、重新生成、反馈）。
 * 规则同网页（chat-ui 的 ModePicker / ModelPicker，web 的 AgentPicker / DevicePickers / MessageActions）。
 */
import * as Clipboard from "expo-clipboard";
import { useEffect, useMemo, useState } from "react";
import { ScrollView, StyleSheet, Text, TextInput, View } from "react-native";

import { useSetFeedback, useThreadFeedback, type Rating } from "@atlas/chat-ui/src/api/feedback";
import { selectableModels, useAgentModels } from "@atlas/chat-ui/src/api/models";
import type { Agent } from "@atlas/chat-ui/src/api/types";
import type { ApprovalMode } from "@atlas/chat-ui/src/lib/events";
import { useDeviceChoices, useDeviceProjects, type DeviceChoice } from "@/api/queries";
import { Avatar, Button, IconButton, Sheet, SheetGroup, SheetRow, Tag, useToast } from "@/components/ui";
import { C, F, MONO, R } from "@/theme";
import { Pill } from "./Composer";

// ── 执行前确认（审批模式）

export const MODE_TEXT: Record<ApprovalMode, { label: string; hint: string }> = {
  auto: { label: "自动", hint: "常规操作直接执行，有风险的先问你" },
  manual: { label: "手动", hint: "每次动手前都先问你" },
  plan: { label: "先规划", hint: "先给出计划，你确认后再执行" },
};
const MODE_ORDER: ApprovalMode[] = ["auto", "manual", "plan"];

export function ModePill({
  value,
  allowed,
  onChange,
  disabled,
}: {
  value: ApprovalMode;
  allowed: ApprovalMode[];
  onChange: (m: ApprovalMode) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const options = MODE_ORDER.filter((m) => allowed.includes(m));
  return (
    <>
      <Pill icon={value === "plan" ? "list" : "shield"} label={MODE_TEXT[value].label} onPress={options.length > 1 ? () => setOpen(true) : undefined} disabled={disabled} />
      <Sheet open={open} onClose={() => setOpen(false)} title="执行前确认">
        <View style={{ paddingHorizontal: 12, paddingBottom: 8 }}>
          {options.map((m) => (
            <SheetRow
              key={m}
              icon={m === "plan" ? "list" : "shield"}
              title={MODE_TEXT[m].label}
              sub={MODE_TEXT[m].hint}
              checked={m === value}
              onPress={() => {
                onChange(m);
                setOpen(false);
              }}
            />
          ))}
        </View>
      </Sheet>
    </>
  );
}

// ── 模型（只换主智能体的模型；列表 = 这个智能体的 Key 能用的模型）

export function ModelPill({
  agentId,
  value,
  defaultModel,
  onChange,
  disabled,
}: {
  agentId: string;
  value: string;
  defaultModel: string;
  onChange: (m: string) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const modelsQ = useAgentModels(agentId);
  const models = selectableModels(modelsQ.data);
  const label = models.find((m) => m.model === value)?.display_name || value;
  return (
    <>
      <Pill icon="brain" label={label} onPress={models.length > 0 ? () => setOpen(true) : undefined} disabled={disabled} />
      <Sheet open={open} onClose={() => setOpen(false)} title="模型" subtitle="只换这个对话的主模型，子智能体不变">
        <ScrollView contentContainerStyle={{ paddingHorizontal: 12, paddingBottom: 8 }}>
          {models.map((m) => (
            <SheetRow
              key={m.model}
              icon="brain"
              title={m.display_name || m.model}
              right={m.model === defaultModel ? <Tag>智能体默认</Tag> : undefined}
              checked={m.model === value}
              onPress={() => {
                onChange(m.model);
                setOpen(false);
              }}
            />
          ))}
        </ScrollView>
      </Sheet>
    </>
  );
}

// ── 智能体（只选，不建不改）

export function AgentSheet({
  open,
  onClose,
  agents,
  value,
  defaultId,
  onChange,
}: {
  open: boolean;
  onClose: () => void;
  agents: Agent[];
  value?: string;
  defaultId?: string;
  onChange: (id: string) => void;
}) {
  const [q, setQ] = useState("");
  const filtered = useMemo(() => {
    const k = q.trim().toLowerCase();
    return k ? agents.filter((a) => a.name.toLowerCase().includes(k) || a.description.toLowerCase().includes(k)) : agents;
  }, [agents, q]);
  // 最近用过的排前面（last_used_at），默认智能体总在最近使用里
  const recent = filtered
    .filter((a) => a.last_used_at || a.id === defaultId)
    .sort((a, b) => (b.last_used_at ?? "").localeCompare(a.last_used_at ?? ""))
    .slice(0, 5);
  const rest = filtered.filter((a) => !recent.includes(a));
  const row = (a: Agent) => (
    <SheetRow
      key={a.id}
      left={<Avatar name={a.name} avatarKey={a.avatar_key} size={40} square />}
      title={
        <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
          <Text style={{ fontSize: F.body, fontWeight: "600", color: C.ink, flexShrink: 1 }} numberOfLines={1}>
            {a.name}
          </Text>
          {a.placement === "device" && <Tag tone="success">本机</Tag>}
          {a.kind === "cli" && <Tag tone="blue">Claude Code</Tag>}
          {a.id === defaultId && <Tag>默认</Tag>}
        </View>
      }
      sub={a.description ? <Text style={st.sub} numberOfLines={1}>{a.description}</Text> : undefined}
      checked={a.id === value}
      onPress={() => {
        onChange(a.id);
        onClose();
      }}
    />
  );
  return (
    <Sheet open={open} onClose={onClose} title="选择智能体" tall>
      <View style={st.search}>
        <TextInput style={st.searchInput} value={q} onChangeText={setQ} placeholder="搜索智能体" placeholderTextColor={C.ink4} />
      </View>
      <ScrollView contentContainerStyle={{ paddingHorizontal: 12, paddingBottom: 12 }}>
        {recent.length > 0 && <SheetGroup>最近使用</SheetGroup>}
        {recent.map(row)}
        {rest.length > 0 && <SheetGroup>{recent.length > 0 ? "全部" : "智能体"}</SheetGroup>}
        {rest.map(row)}
        <Text style={st.foot}>创建和修改智能体请到电脑上</Text>
      </ScrollView>
    </Sheet>
  );
}

// ── 本机 CLI：电脑和项目目录（同网页 DevicePickers）

/** dir 为空 = 新目录（默认项目地址 / 对话 id） */
export interface DevicePick {
  deviceId?: string;
  dir?: string;
}

export function preferredDevice(choices: DeviceChoice[]): DeviceChoice | undefined {
  return choices.find((c) => c.is_default && c.selectable) ?? choices.find((c) => c.selectable);
}

export function useDevicePick(cliType: string | undefined, value: DevicePick, onChange: (p: DevicePick) => void) {
  const choicesQ = useDeviceChoices(cliType);
  const device = choicesQ.data?.find((c) => c.id === value.deviceId);
  // 还没选、或选的那台不能用了：换成首选
  useEffect(() => {
    if (choicesQ.data && !device?.selectable) {
      const next = preferredDevice(choicesQ.data);
      if (next?.id !== value.deviceId) onChange({ deviceId: next?.id, dir: undefined });
    }
  }, [choicesQ.data, device?.selectable, value.deviceId, onChange]);
  return { choices: choicesQ.data ?? [], device, loading: choicesQ.isPending };
}

export function DeviceSheet({
  open,
  onClose,
  cliType,
  defaultProjectDir,
  choices,
  value,
  onChange,
}: {
  open: boolean;
  onClose: () => void;
  cliType: string;
  defaultProjectDir: string;
  choices: DeviceChoice[];
  value: DevicePick;
  onChange: (p: DevicePick) => void;
}) {
  const projectsQ = useDeviceProjects(value.deviceId, open);
  const why = (c: DeviceChoice) => (!c.online ? "不在线" : !c.has_cli ? `没装 ${cliType}` : "");
  return (
    <Sheet open={open} onClose={onClose} title="在哪台电脑上运行" tall>
      <ScrollView contentContainerStyle={{ paddingHorizontal: 12, paddingBottom: 12 }}>
        {choices.length === 0 && <Text style={st.foot}>还没有配对的电脑：在电脑版「我的设备」里配对，并在电脑上启动连接器</Text>}
        {choices.map((c) => (
          <SheetRow
            key={c.id}
            icon="laptop"
            title={
              <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
                <Text style={{ fontSize: F.body, color: C.ink }}>{c.name}</Text>
                {c.is_default && <Tag>默认</Tag>}
              </View>
            }
            sub={c.selectable ? <Text style={{ color: C.success, fontSize: 12.5 }}>在线</Text> : why(c)}
            disabled={!c.selectable}
            checked={c.id === value.deviceId}
            onPress={() => onChange({ deviceId: c.id, dir: undefined })}
          />
        ))}
        <SheetGroup>项目目录</SheetGroup>
        <SheetRow
          icon="plus"
          title="新目录"
          sub={<Text style={st.mono}>{`${defaultProjectDir || "~/atlas-projects"}/对话 id`}</Text>}
          checked={!value.dir}
          onPress={() => onChange({ ...value, dir: undefined })}
        />
        {projectsQ.data?.roots.flatMap((r) =>
          r.entries.map((e) => (
            <SheetRow
              key={e.path}
              icon="folder"
              title={e.name}
              sub={<Text style={st.mono}>{e.path}</Text>}
              right={e.is_repo ? <Tag>git</Tag> : undefined}
              checked={value.dir === e.path}
              onPress={() => onChange({ ...value, dir: e.path })}
            />
          )),
        )}
        <Text style={st.foot}>只列出电脑上允许的目录（本机策略 allowed_roots）</Text>
      </ScrollView>
      <View style={{ paddingHorizontal: 16, paddingTop: 8 }}>
        <Button kind="primary" onPress={onClose}>
          完成
        </Button>
      </View>
    </Sheet>
  );
}

// ── 回答下面的操作

const REASONS = ["内容不准确", "没理解我的意思", "太啰嗦", "操作有误", "其他"];

export function MessageActions({
  threadId,
  messageId,
  text,
  onRegenerate,
}: {
  threadId: string;
  messageId: string;
  text: string;
  onRegenerate?: () => void;
}) {
  const toast = useToast();
  const fbQ = useThreadFeedback(threadId);
  const setFb = useSetFeedback(threadId);
  const mine = fbQ.data?.[messageId];
  const [ask, setAsk] = useState(false);
  const [reasons, setReasons] = useState<string[]>([]);
  const [comment, setComment] = useState("");
  const rate = (rating: Rating | null, extra?: { reasons: string[]; comment: string }) =>
    setFb.mutate({ messageId, rating, ...extra }, { onError: (e) => toast(`没有提交成功：${(e as Error).message}`) });

  return (
    <View style={st.acts}>
      <IconButton name="copy" label="复制" size={18} color={C.ink4} onPress={() => void Clipboard.setStringAsync(text).then(() => toast("已复制"))} />
      {onRegenerate && <IconButton name="refresh" label="重新生成" size={18} color={C.ink4} onPress={onRegenerate} />}
      <IconButton name="like" label="有帮助" size={18} color={mine?.rating === "up" ? C.primary : C.ink4} onPress={() => rate(mine?.rating === "up" ? null : "up")} />
      <IconButton
        name="dislike"
        label="没帮助"
        size={18}
        color={mine?.rating === "down" ? C.primary : C.ink4}
        onPress={() => {
          if (mine?.rating === "down") return rate(null);
          // 先记下「没帮助」，原因可选：直接关掉也不丢这次反馈
          rate("down");
          setReasons([]);
          setComment("");
          setAsk(true);
        }}
      />
      <Sheet
        open={ask}
        onClose={() => setAsk(false)}
        title="哪里不好？"
        subtitle="会交给智能体的维护人改进"
        footer={
          <View style={{ paddingHorizontal: 16, paddingTop: 8 }}>
            <Button
              kind="primary"
              onPress={() => {
                rate("down", { reasons, comment: comment.trim() });
                setAsk(false);
                toast("谢谢，已提交");
              }}
            >
              提交
            </Button>
          </View>
        }
      >
        <View style={st.chips}>
          {REASONS.map((r) => {
            const on = reasons.includes(r);
            return (
              <Text key={r} onPress={() => setReasons(on ? reasons.filter((x) => x !== r) : [...reasons, r])} style={[st.chip, on && st.chipOn]}>
                {r}
              </Text>
            );
          })}
        </View>
        <View style={{ paddingHorizontal: 16 }}>
          <TextInput
            style={st.textarea}
            multiline
            value={comment}
            onChangeText={setComment}
            maxLength={2000}
            placeholder="补充说明（可选）"
            placeholderTextColor={C.ink4}
          />
        </View>
      </Sheet>
    </View>
  );
}

const st = StyleSheet.create({
  sub: { fontSize: 12.5, color: C.ink3, marginTop: 1 },
  search: { paddingHorizontal: 20, paddingBottom: 4 },
  searchInput: { height: 40, borderRadius: R.md, backgroundColor: C.hover, paddingHorizontal: 12, fontSize: F.input, color: C.ink },
  foot: { fontSize: F.xs, color: C.ink4, textAlign: "center", paddingVertical: 12 },
  mono: { fontFamily: MONO, fontSize: F.xs, color: C.ink3 },
  acts: { flexDirection: "row", marginLeft: -10, marginTop: -2, marginBottom: 6 },
  chips: { flexDirection: "row", flexWrap: "wrap", gap: 8, paddingHorizontal: 16, paddingBottom: 12 },
  chip: {
    height: 36,
    lineHeight: 34,
    paddingHorizontal: 14,
    borderRadius: 18,
    borderWidth: 1,
    borderColor: C.line,
    fontSize: F.md,
    color: C.ink2,
    overflow: "hidden",
  },
  chipOn: { borderColor: C.primary2, backgroundColor: C.primaryWeak, color: C.primaryInk, fontWeight: "600" },
  textarea: { minHeight: 80, borderWidth: 1, borderColor: C.line, borderRadius: R.md, padding: 12, fontSize: F.input, textAlignVertical: "top", color: C.ink },
});
