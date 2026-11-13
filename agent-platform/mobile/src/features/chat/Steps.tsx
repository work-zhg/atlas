/**
 * 一组连续的工具调用，按「步骤」呈现（同网页 chat-ui 的 Steps）。
 *
 * ★ 进行中默认展开、结束自动收起；用户点过就按用户的来。
 * ★ 点某一步从底部弹出参数和结果（手机上不在原地展开长 JSON）。
 * ★ 手机端新增：写入 / 修改的文件在工作目录里能找到时，右侧有「预览」。
 */
import { useEffect, useRef, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import type { ToolCall } from "@atlas/chat-ui/src/lib/run-reducer";
import { stepDuration, stepsTitle, stepView } from "@atlas/chat-ui/src/lib/steps";
import { Button, Icon, Sheet, Spin, type IconName } from "@/components/ui";
import { C, F, MONO } from "@/theme";
import { useChat } from "./context";

/** 自动模式拦下的不算失败（它是在保护用户） */
const isAutoDenied = (c: ToolCall) => c.status === "error" && c.errorKind === "auto_mode_denied";

/** 写入、修改文件的工具：参数里的路径就是产出的文件 */
const WRITES_FILE = new Set(["write_file", "edit_file"]);

function filePathOf(call: ToolCall): string | undefined {
  if (!WRITES_FILE.has(call.name) || !call.args || typeof call.args !== "object") return undefined;
  const v = (call.args as Record<string, unknown>).file_path;
  return typeof v === "string" ? v : undefined;
}

export function Steps({ calls, live }: { calls: ToolCall[]; live: boolean }) {
  const running = live && calls.some((c) => c.status === "running");
  const failed = calls.some((c) => c.status === "error" && !isAutoDenied(c));
  const [open, setOpen] = useState(running);
  const touched = useRef(false);
  useEffect(() => {
    if (!touched.current) setOpen(running);
  }, [running]);

  return (
    <View style={s.box}>
      <Pressable
        style={s.head}
        onPress={() => {
          touched.current = true;
          setOpen((o) => !o);
        }}
      >
        {running ? <Spin /> : <Icon name={failed ? "alert" : "check"} size={16} color={failed ? C.warning : C.success} />}
        <Text style={s.headText} numberOfLines={1}>
          {stepsTitle(calls, live)}
        </Text>
        <View style={{ transform: [{ rotate: open ? "90deg" : "0deg" }] }}>
          <Icon name="right" size={16} color={C.ink4} />
        </View>
      </Pressable>
      {open && calls.map((c) => <Step key={c.callId} call={c} live={live} />)}
    </View>
  );
}

function Step({ call, live }: { call: ToolCall; live: boolean }) {
  const { openStep, openFile, resolveFile } = useChat();
  const v = stepView(call);
  const denied = isAutoDenied(call);
  const running = live && call.status === "running";
  const toolPath = filePathOf(call);
  const file = toolPath && call.status === "ok" ? resolveFile(toolPath) : null;
  return (
    <Pressable style={({ pressed }) => [s.step, pressed && { backgroundColor: C.hover }]} onPress={() => openStep(call)}>
      <Icon
        name={call.status === "error" && !denied ? "x" : (v.icon as IconName)}
        size={16}
        color={call.status === "error" ? (denied ? C.warning : C.danger) : C.ink3}
      />
      <Text style={s.label}>{v.label}</Text>
      <Text style={s.target} numberOfLines={1}>
        {denied ? "被自动模式拦下" : v.target}
      </Text>
      {running ? (
        <Spin />
      ) : file ? (
        <Pressable hitSlop={10} onPress={() => openFile(file)}>
          <Text style={s.preview}>预览</Text>
        </Pressable>
      ) : (
        <Text style={s.time}>{stepDuration(call.durationMs)}</Text>
      )}
    </Pressable>
  );
}

function json(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

/** 某一步的参数和结果（底部面板） */
export function StepSheet({ call, onClose }: { call: ToolCall | null; onClose: () => void }) {
  const { openFile, resolveFile } = useChat();
  if (!call) return null;
  const v = stepView(call);
  const hasArgs = call.args !== undefined && !(call.args && typeof call.args === "object" && Object.keys(call.args).length === 0);
  const args = hasArgs ? json(call.args) : call.argsPreview;
  const result =
    call.status === "error"
      ? call.error ?? call.resultPreview ?? "（没有说明原因）"
      : call.resultPreview ?? (call.result !== undefined ? json(call.result) : call.status === "running" ? "进行中…" : "（无输出）");
  const toolPath = filePathOf(call);
  const file = toolPath && call.status === "ok" ? resolveFile(toolPath) : null;
  const sub =
    call.status === "running"
      ? "进行中"
      : isAutoDenied(call)
        ? `被自动模式拦下${call.deniedReason ? `：${call.deniedReason}` : ""}`
        : call.status === "error"
          ? `没成功 · ${stepDuration(call.durationMs)}`
          : `用时 ${stepDuration(call.durationMs) || "—"}`;
  return (
    <Sheet
      open
      onClose={onClose}
      title={`${v.label}${v.target ? ` ${v.target}` : ""}`}
      subtitle={sub}
      footer={
        file ? (
          <View style={{ paddingHorizontal: 16, paddingTop: 8 }}>
            <Button
              kind="primary"
              icon="eye"
              onPress={() => {
                onClose();
                openFile(file);
              }}
            >
              {`预览 ${file.split("/").pop()}`}
            </Button>
          </View>
        ) : undefined
      }
    >
      <ScrollView contentContainerStyle={{ paddingHorizontal: 16, paddingBottom: 12 }}>
        {args ? (
          <>
            <Text style={s.kvLabel}>参数</Text>
            <Text selectable style={s.pre}>
              {args}
            </Text>
          </>
        ) : null}
        <Text style={s.kvLabel}>{call.status === "error" ? "没有成功" : "结果"}</Text>
        <Text selectable style={s.pre}>
          {result}
        </Text>
      </ScrollView>
    </Sheet>
  );
}

const s = StyleSheet.create({
  box: { borderWidth: 1, borderColor: C.line, borderRadius: 14, marginBottom: 12, overflow: "hidden", backgroundColor: C.surface2 },
  head: { flexDirection: "row", alignItems: "center", gap: 8, minHeight: 44, paddingHorizontal: 12 },
  headText: { flex: 1, fontSize: 13.5, color: C.ink2 },
  step: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    minHeight: 44,
    paddingHorizontal: 12,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: C.line,
    backgroundColor: C.surface,
  },
  label: { fontSize: 13.5, color: C.ink3, flexShrink: 0, maxWidth: "60%" },
  target: { flex: 1, fontFamily: MONO, fontSize: 12.5, color: C.ink },
  time: { fontSize: F.xs, color: C.ink4 },
  preview: { fontSize: 12.5, color: C.primary, fontWeight: "700", paddingLeft: 6 },
  kvLabel: { fontSize: F.xs, color: C.ink3, marginTop: 12, marginBottom: 4 },
  pre: {
    fontFamily: MONO,
    fontSize: 12,
    lineHeight: 19,
    color: C.ink,
    backgroundColor: "#f6f7fb",
    borderWidth: 1,
    borderColor: C.line2,
    borderRadius: 10,
    padding: 10,
  },
});
