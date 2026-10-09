"use client";

import { PlusOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Form, Input, List, Modal, Progress, Segmented, Space, Tag } from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { EmptyBox } from "@/components/common";
import { api, errorText } from "@/lib/api";
import { dt } from "@/lib/format";
import { useCan } from "@/lib/session";
import type { ProcessSummary, ProjectDetail } from "@/lib/types";

const STATUS: Record<string, [string, string]> = {
  running: ["进行中", "processing"],
  completed: ["已完成", "success"],
  terminated: ["已终止", "default"],
};

/** 项目首页：流程为主（团队设计 §10）。 */
export function ProcessList({ project }: { project: ProjectDetail }) {
  const can = useCan();
  const [filter, setFilter] = useState("all");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const { data = [], isLoading } = useQuery({
    queryKey: ["processes", project.id, filter, q],
    queryFn: () => api.get<ProcessSummary[]>(`/projects/${project.id}/processes`, { filter, q }),
    refetchInterval: 15_000,
  });
  const canStart = can("process:start") && project.status === "active";

  return (
    <Card
      title="流程"
      extra={
        <Space>
          <Segmented
            value={filter}
            onChange={(v) => setFilter(String(v))}
            options={[
              { value: "all", label: "全部" },
              { value: "todo", label: "待我处理" },
              { value: "running", label: "进行中" },
              { value: "done", label: "已完成" },
            ]}
          />
          <Input.Search placeholder="搜索流程" allowClear onSearch={setQ} style={{ width: 180 }} />
          {canStart && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
              发起流程
            </Button>
          )}
        </Space>
      }
    >
      {!isLoading && !data.length ? (
        <EmptyBox text={filter === "all" ? "还没有流程" : "没有符合条件的流程"}>
          {canStart && filter === "all" && (
            <Button type="primary" onClick={() => setOpen(true)}>
              发起第一个流程
            </Button>
          )}
        </EmptyBox>
      ) : (
        <List
          loading={isLoading}
          dataSource={data}
          renderItem={(p) => (
            <List.Item>
              <Link href={`/processes/${p.id}`} style={{ flex: 1, color: "inherit" }}>
                <Space direction="vertical" size={4} style={{ width: "100%" }}>
                  <Space>
                    <span className="tf-muted tf-mono">#{p.no}</span>
                    <b style={{ fontSize: 14.5 }}>{p.title}</b>
                    <Tag color={STATUS[p.status]![1]}>{STATUS[p.status]![0]}</Tag>
                  </Space>
                  <Space size={14} className="tf-muted" style={{ fontSize: 12.5 }} wrap>
                    <span>
                      {p.started_by} · {dt(p.started_at)}
                    </span>
                    {p.waiting.map((w) => (
                      <span key={w.node}>
                        ⏳ {w.node} · {w.stage} · 等「{w.who}」
                      </span>
                    ))}
                  </Space>
                  {p.notices.map((n) => (
                    <span key={n} style={{ color: "#dc2626", fontSize: 12.5 }}>
                      ⚠️ {n}
                    </span>
                  ))}
                </Space>
              </Link>
              <div style={{ width: 160 }}>
                <Progress percent={Math.round((p.progress.passed / Math.max(1, p.progress.total)) * 100)} size="small" />
                <span className="tf-muted" style={{ fontSize: 12 }}>
                  {p.progress.passed}/{p.progress.total} 节点通过
                </span>
              </div>
            </List.Item>
          )}
        />
      )}
      <StartProcess project={project} open={open} onClose={() => setOpen(false)} />
    </Card>
  );
}

function StartProcess({ project, open, onClose }: { project: ProjectDetail; open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const router = useRouter();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);
  async function submit() {
    const v = await form.validateFields();
    setBusy(true);
    try {
      const p = await api.post<ProcessSummary>(`/projects/${project.id}/processes`, v);
      await qc.invalidateQueries({ queryKey: ["processes", project.id] });
      onClose();
      router.push(`/processes/${p.id}`);
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal open={open} title="发起流程" onOk={submit} onCancel={onClose} confirmLoading={busy} destroyOnHidden width={560}>
      {project.roles.incomplete > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={`${project.roles.incomplete} 个角色未分配或不完整`}
          description="仍可发起；缺 Agent 的节点需要人直接提交产物，缺评审人的节点无法完成评审。"
        />
      )}
      <Form form={form} layout="vertical" requiredMark={false}>
        <Form.Item name="title" label="流程名称" rules={[{ required: true, message: "请填写流程名称" }]}>
          <Input maxLength={100} placeholder="如：支持手机号登录" />
        </Form.Item>
        <Form.Item name="requirement" label="需求" rules={[{ required: true, message: "请用一两句话描述需求" }]}>
          <Input.TextArea maxLength={5000} rows={4} placeholder="一句话需求：会作为每个节点 Agent 的开工上下文" />
        </Form.Item>
        <div className="tf-muted" style={{ fontSize: 12.5 }}>
          使用模板 {project.template_name} {project.template_version}（发起时锁定版本）
        </div>
      </Form>
    </Modal>
  );
}
