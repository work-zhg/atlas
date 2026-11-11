"use client";

/**
 * 每一轮对话都会带给智能体的指令（doc/context-management-design.html §10）。
 * 个人的在「个性化」里，平台的在「系统 › 平台设置」里（影响所有人）。
 */
import { Alert, App, Button, Input } from "antd";
import { useEffect, useState } from "react";

import { useInstruction, useSaveInstruction } from "@/api/context";

export function InstructionEditor({
  scope,
  placeholder,
  hint,
}: {
  scope: "me" | "platform";
  placeholder: string;
  hint: string;
}) {
  const { message } = App.useApp();
  const q = useInstruction(scope);
  const save = useSaveInstruction(scope);
  const [text, setText] = useState("");
  useEffect(() => {
    if (q.data) setText(q.data.content);
  }, [q.data]);

  if (q.error) return <Alert type="error" showIcon message={`读取失败：${(q.error as Error).message}`} />;
  return (
    <>
      <Input.TextArea
        rows={8}
        value={text}
        placeholder={placeholder}
        onChange={(e) => setText(e.target.value)}
        maxLength={q.data?.max_chars ?? 10000}
        showCount
      />
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 14 }}>
        <span style={{ fontSize: 12.5, color: "var(--fg-3)" }}>{hint}</span>
        <Button
          type="primary"
          style={{ marginLeft: "auto" }}
          loading={save.isPending}
          disabled={!q.data || text === q.data.content}
          onClick={() =>
            save
              .mutateAsync(text)
              .then(() => message.success("已保存，下一轮对话生效"))
              .catch((e: Error) => message.error(e.message))
          }
        >
          保存
        </Button>
      </div>
    </>
  );
}
