"use client";

/**
 * 持久指令（doc/context-management-design.html §10）：每一轮都会带给智能体的规则。
 *
 *   个人指令  只对自己的会话生效（如「默认中文、技术术语保留英文」）
 *   平台指令  对所有人生效，管理员维护
 *   工作区    在会话工作区根目录放 ATLAS.md（或 AGENTS.md），对该会话生效
 *
 * 指令与记忆的区别：指令是你主动写下、必须遵守的规则；记忆是系统从对话中观察到的事实。
 */
import { Alert, App, Button, Card, Input, Space, Typography } from "antd";
import { useEffect, useState } from "react";

import {
  type InstructionOut,
  useInstruction,
  useSaveInstruction,
} from "@/api/context";
import { can, useMe } from "@/lib/session";

function Editor({
  scope,
  title,
  hint,
  editable,
}: {
  scope: "me" | "platform";
  title: string;
  hint: string;
  editable: boolean;
}) {
  const { message } = App.useApp();
  const q = useInstruction(scope);
  const save = useSaveInstruction(scope);
  const [text, setText] = useState("");
  useEffect(() => {
    if (q.data) setText(q.data.content);
  }, [q.data]);
  const data: InstructionOut | undefined = q.data;

  return (
    <Card title={title} size="small" style={{ marginBottom: 16 }}>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12.5 }}>
        {hint}
      </Typography.Paragraph>
      {q.error ? (
        <Alert
          type="error"
          message={`读取失败：${(q.error as Error).message}`}
        />
      ) : (
        <>
          <Input.TextArea
            rows={8}
            value={text}
            disabled={!editable}
            onChange={(e) => setText(e.target.value)}
            maxLength={data?.max_chars ?? 10000}
            showCount
          />
          <Space style={{ marginTop: 8 }}>
            {editable && (
              <Button
                type="primary"
                loading={save.isPending}
                disabled={text === (data?.content ?? "")}
                onClick={async () => {
                  try {
                    await save.mutateAsync(text);
                    message.success("已保存，从下一轮对话起生效");
                  } catch (e) {
                    message.error((e as Error).message);
                  }
                }}
              >
                保存
              </Button>
            )}
            {data?.updated_at && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                最近修改：{new Date(data.updated_at).toLocaleString()}
              </Typography.Text>
            )}
          </Space>
        </>
      )}
    </Card>
  );
}

export default function InstructionsPage() {
  const { data: me } = useMe();
  const admin = can(me, "instruction:manage", "model:manage");
  return (
    <div
      style={{ flex: 1, overflow: "auto", padding: "24px 28px", maxWidth: 900 }}
    >
      <Typography.Title level={4} style={{ margin: 0 }}>
        持久指令
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 13 }}>
        每一轮对话都会带给智能体的规则，按「平台 → 个人 →
        工作区（ATLAS.md）」的顺序拼在系统提示词之后，
        后面的优先。修改从下一轮对话起生效。
      </Typography.Paragraph>
      <Editor
        scope="me"
        title="个人指令"
        hint="只对你自己的会话生效。适合写个人偏好，例如回答语言、代码风格、输出格式。"
        editable
      />
      <Editor
        scope="platform"
        title="平台指令"
        hint={
          admin
            ? "对所有人的所有会话生效。适合写合规要求与全局约定。"
            : "由管理员维护，对所有会话生效。"
        }
        editable={admin}
      />
    </div>
  );
}
