"use client";

import { SendOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App,
  Breadcrumb,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Empty,
  Form,
  Input,
  List,
  Modal,
  Radio,
  Row,
  Select,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography,
} from "antd";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, use, useEffect, useState } from "react";

import { AgentAvatar, EmptyBox, PageHead, PersonAvatar } from "@/components/common";
import { FlowView } from "@/components/FlowView";
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
      const first = Object.entries(p.nodes).find(([, n]) => ["working", "exit_review", "admit_review"].includes(n.status))?.[0];
      setActive(wanted ?? first ?? Object.keys(p.nodes)[0]);
    }
  }, [p, seq, active, search]);
  const live = useProcessStream(id, seq);

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
        <Typography.Paragraph style={{ margin: "0 0 6px" }} ellipsis={{ rows: 2, expandable: true, symbol: "展开" }}>
          <b>需求：</b>
          {p.requirement}
        </Typography.Paragraph>
        <FlowView definition={p.definition} states={p.nodes} active={active} onPick={setActive} />
      </Card>
      <NodePanel process={p} nodeId={active} live={live[active] ?? ""} />
    </>
  );
}

function NodePanel({
  process,
  nodeId,
  live,
}: {
  process: ProcessDetail;
  nodeId: string;
  live: string;
}) {
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
              { key: "chat", label: `人机协同 ${n.messages.length}`, children: <Chat process={process} node={n} live={live} /> },
              { key: "artifact", label: `产物 ${n.artifacts.length}`, children: <Artifacts process={process} node={n} /> },
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
            <Descriptions.Item label="准入">
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
  const [editing, setEditing] = useState(false);
  const current = node.artifacts.find((a) => a.current);
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
        <>
          <Button onClick={() => setEditing(true)}>手工提交产物</Button>
          <Button type="primary" disabled={!current || current.round !== node.round} onClick={submitReview}>
            提交准出评审
          </Button>
        </>
      )}
      {node.can.resubmit && (
        <Button type="primary" onClick={submitReview}>
          重新提交评审（刷新评审人）
        </Button>
      )}
      <ArtifactEditor process={process} node={node} open={editing} onClose={() => setEditing(false)} onDone={refresh} />
    </Space>
  );
}

function splitArtifact(text: string): { before: string; artifact: string | null; after: string } {
  const m = /<artifact(?:\s[^>]*)?>([\s\S]*?)(?:<\/artifact>|$)/.exec(text);
  if (!m) return { before: text, artifact: null, after: "" };
  return { before: text.slice(0, m.index), artifact: m[1]!.trim(), after: text.slice(m.index + m[0].length) };
}

function AgentText({ text }: { text: string }) {
  const { before, artifact, after } = splitArtifact(text);
  return (
    <>
      {before.trim() && <div style={{ whiteSpace: "pre-wrap" }}>{before.trim()}</div>}
      {artifact !== null && (
        <Collapse
          size="small"
          style={{ margin: "6px 0", background: "#fff" }}
          items={[{ key: "a", label: "📄 产物全文（已提交为产物版本）", children: <pre className="tf-md">{artifact}</pre> }]}
        />
      )}
      {after.trim() && <div style={{ whiteSpace: "pre-wrap" }}>{after.trim()}</div>}
    </>
  );
}

function Chat({ process, node, live }: { process: ProcessDetail; node: NodeDetail; live: string }) {
  const { message } = App.useApp();
  const refresh = useRefresh(process, node.node_id);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const agentName = process.nodes[node.node_id]?.agent ?? "Agent";
  async function send() {
    if (!text.trim()) return;
    setBusy(true);
    try {
      await api.post(`/processes/${process.id}/nodes/${node.node_id}/messages`, { text });
      setText("");
      await refresh();
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div>
      <div style={{ display: "flex", flexDirection: "column", gap: 12, maxHeight: 560, overflow: "auto", padding: "4px 2px" }}>
        {!node.messages.length && <Empty description="节点开工后，Agent 会在这里澄清问题并产出" />}
        {node.messages.map((m) =>
          m.role === "system" ? (
            <Collapse
              key={m.id}
              size="small"
              items={[
                {
                  key: "s",
                  label: (
                    <span className="tf-muted" style={{ fontSize: 12.5 }}>
                      ⚙️ 系统发给 Agent：{m.content.split("\n")[0]!.slice(0, 60)}… · 第 {m.round} 轮 · {dt(m.created_at)}
                      {m.status === "queued" && " · 排队中"}
                    </span>
                  ),
                  children: <pre className="tf-md">{m.content}</pre>,
                },
              ]}
            />
          ) : m.role === "user" ? (
            <div key={m.id} style={{ alignSelf: "flex-end", maxWidth: "80%", display: "flex", gap: 8 }}>
              <div style={{ background: "#eef0ff", borderRadius: 12, padding: "8px 12px", whiteSpace: "pre-wrap" }}>
                {m.content}
                <div className="tf-muted" style={{ fontSize: 11.5, marginTop: 4 }}>
                  {m.author} · {dt(m.created_at)}
                  {m.status === "queued" && " · 排队中"}
                </div>
              </div>
              <PersonAvatar name={m.author ?? "人"} />
            </div>
          ) : (
            <div key={m.id} style={{ maxWidth: "90%", display: "flex", gap: 8 }}>
              <AgentAvatar name={agentName} />
              <div
                style={{
                  background: m.status === "failed" ? "#fef2f2" : "#f8fafc",
                  border: "1px solid #eef0f4",
                  borderRadius: 12,
                  padding: "8px 12px",
                  minWidth: 120,
                }}
              >
                {m.status === "streaming" ? (
                  live ? <AgentText text={live} /> : <Spin size="small" />
                ) : m.status === "failed" ? (
                  <span style={{ color: "#dc2626" }}>运行失败 {m.content}</span>
                ) : (
                  <AgentText text={m.content} />
                )}
                <div className="tf-muted" style={{ fontSize: 11.5, marginTop: 4 }}>
                  {agentName} · {dt(m.created_at)}
                </div>
              </div>
            </div>
          ),
        )}
      </div>
      {node.can.handle && (
        <Space.Compact style={{ width: "100%", marginTop: 12 }}>
          <Input.TextArea
            value={text}
            onChange={(e) => setText(e.target.value)}
            autoSize={{ minRows: 2, maxRows: 6 }}
            placeholder={process.nodes[node.node_id]?.agent ? "给 Agent 下指令、回答它的问题（Ctrl+Enter 发送）" : "该节点没有可用 Agent，请手工提交产物"}
            disabled={!process.nodes[node.node_id]?.agent}
            onKeyDown={(e) => e.key === "Enter" && (e.ctrlKey || e.metaKey) && void send()}
          />
          <Button type="primary" icon={<SendOutlined />} loading={busy} onClick={send} style={{ height: "auto" }} disabled={!process.nodes[node.node_id]?.agent}>
            发送
          </Button>
        </Space.Compact>
      )}
    </div>
  );
}

function Artifacts({ process, node }: { process: ProcessDetail; node: NodeDetail }) {
  const [version, setVersion] = useState<number>();
  const cur = node.artifacts.find((a) => a.current);
  const shown = version ?? cur?.version;
  const { data } = useQuery({
    queryKey: ["artifact", process.id, node.node_id, shown],
    queryFn: () => api.get<{ content: string; commit: string; path: string }>(`/processes/${process.id}/nodes/${node.node_id}/artifacts/${shown}`),
    enabled: shown !== undefined && shown !== cur?.version,
  });
  if (!node.artifacts.length) return <Empty description="还没有产物" />;
  const content = shown === cur?.version ? node.current_content : data?.content;
  const meta = node.artifacts.find((a) => a.version === shown);
  return (
    <Row gutter={12}>
      <Col span={7}>
        <List
          size="small"
          dataSource={node.artifacts}
          renderItem={(a) => (
            <List.Item
              onClick={() => setVersion(a.version)}
              style={{ cursor: "pointer", background: a.version === shown ? "#eef0ff" : undefined, paddingLeft: 8 }}
            >
              <Space direction="vertical" size={0}>
                <Space size={4}>
                  <b>v{a.version}</b>
                  {a.current && <Tag color="green">当前</Tag>}
                  <Tag>{a.source === "agent" ? "Agent" : a.by ?? "人工"}</Tag>
                </Space>
                <span className="tf-muted" style={{ fontSize: 11.5 }}>
                  第 {a.round} 轮 · {dt(a.created_at)}
                </span>
              </Space>
            </List.Item>
          )}
        />
      </Col>
      <Col span={17}>
        {meta && (
          <div className="tf-muted tf-mono" style={{ fontSize: 11.5, marginBottom: 6 }}>
            {meta.path} @ {meta.commit.slice(0, 10)}
            {meta.url && (
              <a href={meta.url} target="_blank" rel="noreferrer" style={{ marginLeft: 8 }}>
                在 Gitee 查看 ↗
              </a>
            )}
          </div>
        )}
        {content ? <pre className="tf-md">{content}</pre> : <Spin />}
      </Col>
    </Row>
  );
}

function ArtifactEditor({
  process,
  node,
  open,
  onClose,
  onDone,
}: {
  process: ProcessDetail;
  node: NodeDetail;
  open: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const { message } = App.useApp();
  const [content, setContent] = useState("");
  const [note, setNote] = useState("");
  useEffect(() => {
    if (open) setContent(node.current_content ?? "");
  }, [open, node.current_content]);
  async function submit() {
    try {
      await api.post(`/processes/${process.id}/nodes/${node.node_id}/artifacts`, { content, note });
      message.success("已提交为新版本（Git 提交）");
      onDone();
      onClose();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open={open} title={`手工提交产物：${node.config.output_name}`} onOk={submit} onCancel={onClose} width={820} destroyOnHidden okText="提交新版本">
      <Input.TextArea value={content} onChange={(e) => setContent(e.target.value)} autoSize={{ minRows: 14, maxRows: 26 }} className="tf-mono" />
      <Input placeholder="说明（可选）" value={note} onChange={(e) => setNote(e.target.value)} style={{ marginTop: 8 }} maxLength={200} />
    </Modal>
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
        <Card size="small" style={{ background: "#fffbeb", borderColor: "#fde68a" }} title="我的投票">
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
              <Form.Item name="return_to" label="打回到（可选）" extra="不选 = 本节点修改；选择上游节点 = 打回到该节点重做">
                <Select
                  allowClear
                  options={node.upstream.map((id) => ({ value: id, label: process.definition.nodes[id]?.name ?? id }))}
                />
              </Form.Item>
            )}
            <Button type="primary" onClick={vote} danger={decision === "reject"}>
              提交
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
                {r.role} · {RULE[r.rule]} · 针对 v{r.artifact_version}
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
