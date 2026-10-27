"use client";

/**
 * 记忆页的弹窗：添加、更正、移动条目，修改主题（doc/memory-design.html §7）。
 *
 * ★ 添加、更正的来源都记为「用户亲自」：会话后补记不能改动这类记忆（§6.3）。
 */
import { App, Form, Input, Modal, Radio, Select, Switch } from "antd";
import { useEffect, useState } from "react";

import { memoryApi, useMemoryMutation } from "@/api/memories";
import type { MemoryEntry, MemoryTopic } from "@/api/types";

const NEW_TOPIC = "__new__";

function topicOptions(topics: MemoryTopic[]) {
  return topics.map((t) => ({ value: t.code, label: t.resident ? `${t.code}（常驻）` : t.code }));
}

export function AddEntryModal({
  open,
  topics,
  defaultTopic,
  onClose,
}: {
  open: boolean;
  topics: MemoryTopic[];
  defaultTopic: string | null;
  onClose: () => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const add = useMemoryMutation(memoryApi.addEntry);
  const target = Form.useWatch("topic", form);

  useEffect(() => {
    if (open) {
      form.resetFields();
      form.setFieldsValue({ topic: defaultTopic ?? topics[0]?.code ?? NEW_TOPIC, resident: false });
    }
  }, [open, defaultTopic, topics, form]);

  async function submit() {
    const v = await form.validateFields();
    const creating = v.topic === NEW_TOPIC;
    try {
      const r = await add.mutateAsync({
        topic: creating ? v.newTopic.trim() : v.topic,
        content: v.content.trim(),
        topic_description: creating ? v.description.trim() : undefined,
        resident: creating ? v.resident : undefined,
      });
      message.success(`已记住 #${r.number}`);
      onClose();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <Modal title="添加记忆" open={open} onCancel={onClose} onOk={submit} okText="保存" confirmLoading={add.isPending}>
      <Form form={form} layout="vertical" requiredMark={false}>
        <Form.Item
          name="content"
          label="内容"
          extra="一条只写一个事实，用陈述句，单独拿出来也能看懂；日期写成绝对日期，例如「2026-10 起 JS 项目改用 bun」"
          rules={[{ required: true, whitespace: true, message: "写下要记住的内容" }]}
        >
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} maxLength={4000} />
        </Form.Item>
        <Form.Item name="topic" label="主题">
          <Select options={[...topicOptions(topics), { value: NEW_TOPIC, label: "＋ 新建主题" }]} />
        </Form.Item>
        {target === NEW_TOPIC && (
          <>
            <Form.Item
              name="newTopic"
              label="主题编码"
              extra="写成「大类-细分」，例如 编码-java"
              rules={[{ required: true, whitespace: true, message: "给主题起个编码" }]}
            >
              <Input maxLength={64} />
            </Form.Item>
            <Form.Item
              name="description"
              label="收什么、不收什么"
              extra="智能体靠它判断写到哪个主题、要不要加载"
              rules={[{ required: true, whitespace: true, message: "写一句主题说明" }]}
            >
              <Input maxLength={300} />
            </Form.Item>
            <Form.Item name="resident" label="常驻" valuePropName="checked" extra="每次对话都用得上的（身份、沟通偏好）才常驻">
              <Switch />
            </Form.Item>
          </>
        )}
      </Form>
    </Modal>
  );
}

export function CorrectEntryModal({
  entry,
  topics,
  onClose,
}: {
  entry: MemoryEntry | null;
  topics: MemoryTopic[];
  onClose: () => void;
}) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const correct = useMemoryMutation((v: { number: number; content: string; topic?: string }) =>
    memoryApi.correctEntry(v.number, { content: v.content, topic: v.topic }),
  );

  useEffect(() => {
    if (entry) form.setFieldsValue({ content: entry.content, topic: entry.topic });
  }, [entry, form]);

  async function submit() {
    if (!entry) return;
    const v = await form.validateFields();
    try {
      const r = await correct.mutateAsync({
        number: entry.number,
        content: v.content.trim(),
        topic: v.topic !== entry.topic ? v.topic : undefined,
      });
      message.success(`已更正：#${entry.number} 由 #${r.number} 取代`);
      onClose();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <Modal
      title={`更正 #${entry?.number ?? ""}`}
      open={entry !== null}
      onCancel={onClose}
      onOk={submit}
      okText="保存"
      confirmLoading={correct.isPending}
    >
      <p style={{ color: "var(--fg-2)", fontSize: 13, marginTop: 0 }}>
        保存后原条目被新内容取代（可以在修订历史里撤销），来源记为「用户亲自」。
      </p>
      <Form form={form} layout="vertical" requiredMark={false}>
        <Form.Item name="content" label="内容" rules={[{ required: true, whitespace: true }]}>
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} maxLength={4000} />
        </Form.Item>
        <Form.Item name="topic" label="主题">
          <Select options={topicOptions(topics)} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

export function MoveEntriesModal({
  numbers,
  topics,
  onClose,
}: {
  numbers: number[] | null;
  topics: MemoryTopic[];
  onClose: () => void;
}) {
  const { message } = App.useApp();
  const [target, setTarget] = useState<string>();
  const move = useMemoryMutation((v: { numbers: number[]; to: string }) => memoryApi.moveEntries(v.numbers, v.to));

  useEffect(() => setTarget(undefined), [numbers]);

  async function submit() {
    if (!numbers || !target) return;
    try {
      await move.mutateAsync({ numbers, to: target });
      message.success(`已移到「${target}」`);
      onClose();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <Modal
      title={`移动 ${numbers?.length ?? 0} 条记忆`}
      open={numbers !== null}
      onCancel={onClose}
      onOk={submit}
      okText="移动"
      okButtonProps={{ disabled: !target }}
      confirmLoading={move.isPending}
    >
      <p style={{ color: "var(--fg-2)", fontSize: 13, marginTop: 0 }}>只换所属主题，内容、编号、时间和来源都不变。</p>
      <Select style={{ width: "100%" }} placeholder="移到哪个主题" value={target} onChange={setTarget} options={topicOptions(topics)} />
    </Modal>
  );
}

export function TopicEditModal({ topic, onClose }: { topic: MemoryTopic | null; onClose: () => void }) {
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const update = useMemoryMutation((v: { code: string; description: string; resident: boolean }) =>
    memoryApi.updateTopic(v.code, { description: v.description, resident: v.resident }),
  );

  useEffect(() => {
    if (topic) form.setFieldsValue({ description: topic.description, resident: topic.resident ? "yes" : "no" });
  }, [topic, form]);

  async function submit() {
    if (!topic) return;
    const v = await form.validateFields();
    try {
      await update.mutateAsync({ code: topic.code, description: v.description.trim(), resident: v.resident === "yes" });
      message.success("已保存，下一轮对话生效");
      onClose();
    } catch (e) {
      message.error((e as Error).message);
    }
  }

  return (
    <Modal
      title={`主题：${topic?.code ?? ""}`}
      open={topic !== null}
      onCancel={onClose}
      onOk={submit}
      okText="保存"
      confirmLoading={update.isPending}
    >
      <Form form={form} layout="vertical" requiredMark={false}>
        <Form.Item
          name="description"
          label="收什么、不收什么"
          extra="智能体靠它判断写到哪个主题、要不要加载"
          rules={[{ required: true, whitespace: true }]}
        >
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} maxLength={300} />
        </Form.Item>
        <Form.Item name="resident" label="加载方式">
          <Radio.Group
            options={[
              { value: "yes", label: "常驻：每次对话都放进上下文" },
              { value: "no", label: "按需：智能体需要时再加载" },
            ]}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
