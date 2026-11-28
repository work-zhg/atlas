"use client";

/**
 * 流程页的头：标题、状态、谁什么时候发起，三个标签（概览 · 文件 · 记录）和「终止流程」。
 * 三个标签各是一个地址（/flows/{id}、/space、/trace），刷新、分享都停在原处。
 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Button, Dropdown, Input, Modal } from "antd";
import Link from "next/link";
import { useState } from "react";

import { api, errorText } from "@/lib/api";
import { dt } from "@/lib/format";
import { useProject } from "@/lib/queries";
import type { ProcessDetail } from "@/lib/types";

import { Crumb, Icon, PageHead, ProcessStatusTag } from "./ui";

export type FlowTab = "overview" | "files" | "records";

export function FlowHeader({
  process: p,
  tab,
  version,
  nameOf,
}: {
  process: ProcessDetail;
  tab: FlowTab;
  version: number | undefined;
  nameOf: (id: string | null | undefined) => string;
}) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const project = useProject(p.project_id);
  const [terminating, setTerminating] = useState(false);
  const [reason, setReason] = useState("");
  const template = project.data?.bindings.find((b) => b.template.id === p.template_id)?.template.name;

  const terminate = async () => {
    try {
      await api.post(`/processes/${p.id}/terminate`, { reason: reason.trim() || null });
      await qc.invalidateQueries({ queryKey: ["process", p.id] });
      setTerminating(false);
    } catch (e) {
      message.error(errorText(e));
    }
  };

  const tabs: [FlowTab, string, string][] = [
    ["overview", "概览", `/flows/${p.id}`],
    ["files", "文件", `/flows/${p.id}/space`],
    ["records", "记录", `/flows/${p.id}/trace`],
  ];
  return (
    <>
      <Crumb items={[[project.data?.name ?? "项目", `/projects/${p.project_id}`], [p.code, undefined]]} />
      <PageHead
        title={
          <>
            {p.title} <ProcessStatusTag status={p.status} />
          </>
        }
        sub={
          <>
            <span>
              {template ?? "流程模板"}
              {version ? ` v${version}` : ""}
            </span>
            <span>·</span>
            <span>
              {nameOf(p.started_by)} {dt(p.started_at)} 发起
            </span>
            {p.finished_at && (
              <>
                <span>·</span>
                <span>
                  {dt(p.finished_at)} {p.status === "completed" ? "完成" : "终止"}
                </span>
              </>
            )}
          </>
        }
        extra={
          p.can_terminate && (
            <Dropdown
              trigger={["click"]}
              menu={{ items: [{ key: "t", danger: true, label: "终止流程" }], onClick: () => setTerminating(true) }}
            >
              <Button icon={<Icon name="more" size={14} />} />
            </Dropdown>
          )
        }
      />
      <nav className="tabs" aria-label="流程">
        {tabs.map(([key, label, href]) => (
          <Link key={key} href={href} aria-current={key === tab ? "page" : undefined}>
            {label}
          </Link>
        ))}
      </nav>
      <Modal open={terminating} title="终止流程" onCancel={() => setTerminating(false)} onOk={() => void terminate()} okText="终止" okButtonProps={{ danger: true }}>
        <div className="sm muted" style={{ marginBottom: 8 }}>
          数字员工都会停下，还没结束的评审取消。文件保留，只能看不能改
        </div>
        <Input.TextArea value={reason} onChange={(e) => setReason(e.target.value)} placeholder="原因（可选）" maxLength={200} autoSize={{ minRows: 2 }} />
      </Modal>
    </>
  );
}
