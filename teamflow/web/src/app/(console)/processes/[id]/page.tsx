"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App,
  Breadcrumb,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Form,
  Input,
  Radio,
  Row,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
} from "antd";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, use, useEffect, useState } from "react";

import { EmptyBox, PageHead } from "@/components/common";
import { FlowView } from "@/components/FlowView";
import { NodeConversation } from "@/components/NodeConversation";
import { ProcessFiles } from "@/components/ProcessFiles";
import { api, errorText } from "@/lib/api";
import { dt, RULE } from "@/lib/format";
import type { NodeDetail, ProcessDetail } from "@/lib/types";
import { useProcessStream } from "@/lib/useProcessStream";

const P_STATUS: Record<string, [string, string]> = {
  running: ["进行中", "processing"],
  completed: ["已完成", "success"],
  terminated: ["已终止", "default"],
};

export default function Page({ params }: { params: Promise<{ id: string }> }) {
  // useSearchParams 需要 Suspense 边界（next build 要求）
  return (
    <Suspense fallback={<Spin />}>
      <ProcessPage params={params} />
    </Suspense>
  );
}

function ProcessPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const search = useSearchParams();
  const { message, modal } = App.useApp();
  const qc = useQueryClient();
  const { data: p, error } = useQuery({ queryKey: ["process", id], queryFn: () => api.get<ProcessDetail>(`/processes/${id}`) });
  const [active, setActive] = useState<string>();
  const [seq, setSeq] = useState<number>();
  useEffect(() => {
    if (p && seq === undefined) setSeq(p.event_seq);
    if (p && !active) {
      const wanted = search.get("node");
      const first = Object.entries(p.nodes).find(([, n]) => ["working", "exit_review", "admit_review", "exited"].includes(n.status))?.[0];
      setActive(wanted ?? first ?? Object.keys(p.nodes)[0]);
    }
  }, [p, seq, active, search]);
  useProcessStream(id, seq);

  if (error) return <EmptyBox text={errorText(error)} />;
  if (!p || !active) return <Spin />;

  function terminate() {
    let reason = "";
    modal.confirm({
      title: "终止流程？",
      content: <Input.TextArea placeholder="原因（可选）" onChange={(e) => (reason = e.target.value)} />,
      okButtonProps: { danger: true },
      okText: "终止",
      onOk: async () => {
        try {
          await api.post(`/processes/${id}/terminate`, { reason });
          await qc.invalidateQueries({ queryKey: ["process", id] });
        } catch (e) {
          message.error(errorText(e));
        }
      },
    });
  }

  return (
    <>
      <Breadcrumb
        items={[
          { title: <Link href="/teams">团队</Link> },
          { title: <Link href={`/teams/${p.team_id}`}>{p.team_name}</Link> },
          { title: <Link href={`/projects/${p.project_id}`}>{p.project_name}</Link> },
          { title: `#${p.no} ${p.title}` },
        ]}
        style={{ marginBottom: 12 }}
      />
      <PageHead
        title={
          <Space>
            <span className="tf-muted tf-mono">#{p.no}</span>
            {p.title}
            <Tag color={P_STATUS[p.status]![1]}>{P_STATUS[p.status]![0]}</Tag>
          </Space>
        }
        sub={
          <>
            {p.started_by} 发起于 {dt(p.started_at)} · 模板 {p.template_version} · {p.progress.passed}/{p.progress.total} 节点通过
          </>
        }
        extra={
          p.can.terminate && (
            <Button danger onClick={terminate}>
              终止流程
            </Button>
          )
        }
      />
      <Card size="small" style={{ marginBottom: 16 }}>
        <FlowView definition={p.definition} states={p.nodes} active={active} onPick={setActive} />
      </Card>
      <NodePanel process={p} nodeId={active} />
    </>
  );
}

function NodePanel({ process, nodeId }: { process: ProcessDetail; nodeId: string }) {
  const { data: n } = useQuery({
    queryKey: ["node", process.id, nodeId],
    queryFn: () => api.get<NodeDetail>(`/processes/${process.id}/nodes/${nodeId}`),
  });
  if (!n) return <Spin />;
  const openReview = n.reviews.find((r) => r.status === "open");
  return (
    <Row gutter={16}>
      <Col span={16}>
        <Card
          size="small"
          title={
            <Space>
              {n.name}
              <Tag>{n.stage}</Tag>
              {n.round > 1 && <Tag>第 {n.round} 轮</Tag>}
            </Space>
          }
          extra={<NodeActions process={process} node={n} />}
        >
          {n.notice && <Alert type="warning" showIcon message={n.notice} style={{ marginBottom: 12 }} />}
          <Tabs
            defaultActiveKey={openReview && n.can.vote ? "review" : "chat"}
            key={n.node_id}
            items={[
              { key: "chat", label: "人机协同", children: <NodeConversation process={process} node={n} /> },
              { key: "artifact", label: "产物", children: <ProcessFiles processId={process.id} node={n} /> },
              {
                key: "review",
                label: (
                  <Space size={4}>
                    评审 {n.reviews.length}
                    {n.can.vote && <Tag color="orange">待我投票</Tag>}
                  </Space>
                ),
                children: <Reviews process={process} node={n} />,
              },
            ]}
          />
        </Card>
      </Col>
      <Col span={8}>
        <Card size="small" title="节点信息">
          <Descriptions column={1} size="small">
            <Descriptions.Item label="执行角色">{n.config.exec_role}</Descriptions.Item>
            <Descriptions.Item label="执行 Agent">{process.nodes[n.node_id]?.agent ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="产物">{n.config.output_name}</Descriptions.Item>
            <Descriptions.Item label="文件模板">
              {n.file_template ? `${n.file_template.name} v${n.file_template.version}（已锁定）` : "通用格式"}
            </Descriptions.Item>
            <Descriptions.Item label="准出">
              {n.config.exit_review.role} · {RULE[n.config.exit_review.rule]}
            </Descriptions.Item>
            <Descriptions.Item label="开工前准入">
              {n.config.admit_review ? `${n.config.admit_review.role} · ${RULE[n.config.admit_review.rule]}` : "不需要"}
            </Descriptions.Item>
          </Descriptions>
          {!!n.inputs.length && (
            <>
              <div className="tf-muted" style={{ fontSize: 12.5, margin: "8px 0 4px" }}>
                输入（上游产物）
              </div>
              {n.inputs.map((i) => (
                <div key={i.node_id} style={{ fontSize: 13 }}>
                  📄 {i.output} {i.version ? `v${i.version}` : "（尚无）"} <span className="tf-muted">· {i.node}</span>
                </div>
              ))}
            </>
          )}
        </Card>
      </Col>
    </Row>
  );
}

function useRefresh(process: ProcessDetail, nodeId: string) {
  const qc = useQueryClient();
  return () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: ["process", process.id] }),
      qc.invalidateQueries({ queryKey: ["node", process.id, nodeId] }),
    ]);
}

function NodeActions({ process, node }: { process: ProcessDetail; node: NodeDetail }) {
  const { message } = App.useApp();
  const refresh = useRefresh(process, node.node_id);
  async function submitReview() {
    try {
      await api.post(`/processes/${process.id}/nodes/${node.node_id}/submit-review`);
      await refresh();
      message.success("已提交评审");
    } catch (e) {
      message.error(errorText(e));
    }
  }
  if (!node.can.handle && !node.can.resubmit) return null;
  return (
    <Space>
      {node.can.handle && (
        <Button type="primary" onClick={submitReview} title="锁定产物路径当前最新的提交作为本次评审的版本">
          提交准出评审
        </Button>
      )}
      {node.can.resubmit && (
        <Button type="primary" onClick={submitReview}>
          重新提交评审（刷新评审人）
        </Button>
      )}
    </Space>
  );
}

function Reviews({ process, node }: { process: ProcessDetail; node: NodeDetail }) {
  const { message } = App.useApp();
  const refresh = useRefresh(process, node.node_id);
  const [form] = Form.useForm();
  const decision = Form.useWatch("decision", form);
  async function vote() {
    const v = await form.validateFields();
    try {
      await api.post(`/processes/${process.id}/nodes/${node.node_id}/votes`, v);
      form.resetFields();
      await refresh();
      message.success("已投票");
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Space direction="vertical" style={{ width: "100%" }} size={12}>
      {node.can.vote && (
        <Card
          size="small"
          style={{ background: "#fffbeb", borderColor: "#fde68a" }}
          title={node.status === "admit_review" ? "我的投票 · 准入评审（审核上游产物，决定本节点能否开工）" : "我的投票 · 准出评审（本节点产物能否交出）"}
        >
          <Form form={form} layout="vertical" initialValues={{ decision: "approve" }}>
            <Form.Item name="decision">
              <Radio.Group
                options={[
                  { value: "approve", label: "同意" },
                  { value: "reject", label: "驳回" },
                ]}
              />
            </Form.Item>
            <Form.Item name="comment" label="意见" rules={[{ required: decision === "reject", message: "驳回时请写明修改意见" }]}>
              <Input.TextArea rows={3} placeholder={decision === "reject" ? "修改意见会作为指令发给 Agent" : "可选"} />
            </Form.Item>
            {decision === "reject" && !!node.upstream.length && (
              <Form.Item
                name="return_to"
                label={node.status === "admit_review" ? "打回到哪个上游" : "打回到（可选）"}
                extra={
                  node.status === "admit_review"
                    ? "准入驳回会打回上游重做；只有一个上游时可不选"
                    : "不选 = 本节点修改；选择上游节点 = 打回到该节点重做"
                }
              >
                <Select
                  allowClear
                  options={node.upstream.map((id) => ({ value: id, label: process.definition.nodes[id]?.name ?? id }))}
                />
              </Form.Item>
            )}
            <Button type="primary" onClick={vote} danger={decision === "reject"}>
              提交投票
            </Button>
          </Form>
        </Card>
      )}
      {!node.reviews.length && <Empty description="还没有评审" />}
      {[...node.reviews].reverse().map((r) => (
        <Card
          key={r.id}
          size="small"
          title={
            <Space>
              {r.stage === "exit" ? "准出" : "准入"} · 第 {r.round} 轮
              <Tag color={{ open: "processing", passed: "success", rejected: "error", cancelled: "default" }[r.status]}>
                {{ open: "进行中", passed: "通过", rejected: "驳回", cancelled: "已取消" }[r.status]}
              </Tag>
              <span className="tf-muted" style={{ fontSize: 12 }}>
                {r.role} · {RULE[r.rule]} · {r.stage === "admit" ? "审核上游产物" : `针对 v${r.artifact_version}`}
              </span>
            </Space>
          }
        >
          <Space wrap style={{ marginBottom: 8 }}>
            {r.reviewers.map((u) => {
              const v = r.votes.find((x) => x.user_id === u.id);
              return (
                <Tag key={u.id} color={v ? (v.decision === "approve" ? "green" : "red") : undefined}>
                  {u.name} {v ? (v.decision === "approve" ? "✓ 同意" : "✗ 驳回") : "待投票"}
                </Tag>
              );
            })}
            {!r.reviewers.length && <span style={{ color: "#dc2626" }}>评审人名单为空</span>}
          </Space>
          {r.votes
            .filter((v) => v.comment)
            .map((v) => (
              <div key={v.user_id} style={{ fontSize: 13, marginTop: 4 }}>
                <b>{v.user}</b>：{v.comment}
                {v.return_to && <Tag style={{ marginLeft: 6 }}>打回到 {process.definition.nodes[v.return_to]?.name}</Tag>}
              </div>
            ))}
        </Card>
      ))}
    </Space>
  );
}
