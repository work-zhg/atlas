"use client";

import { ArrowDownOutlined, ArrowUpOutlined, DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App,
  AutoComplete,
  Breadcrumb,
  Button,
  Card,
  Checkbox,
  Col,
  Divider,
  Dropdown,
  Form,
  Input,
  List,
  Modal,
  Radio,
  Row,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Typography,
} from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useCallback, useEffect, useRef, useState } from "react";

import { EmptyBox, PageHead } from "@/components/common";
import { FlowView, RoleTags } from "@/components/FlowView";
import { api, ApiError, errorText } from "@/lib/api";
import { dt } from "@/lib/format";
import { addNode, addParallel, addToTrack, addTrack, moveStep, nodeIds, removeNode } from "@/lib/flowEdit";
import type { Definition, Draft, FileTemplate, FlowTemplate, Issue, NodeConfig } from "@/lib/types";

type Check = { errors: Issue[]; warnings: Issue[]; roles: { exec: string[]; review: string[] }; new_roles: string[] };

export default function FlowEditor({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const qc = useQueryClient();
  const router = useRouter();
  const { message, modal } = App.useApp();
  const { data: t } = useQuery({ queryKey: ["flow-template", id], queryFn: () => api.get<FlowTemplate>(`/flow-templates/${id}`) });
  const [draft, setDraft] = useState<Draft>();
  const [def, setDef] = useState<Definition>();
  const [active, setActive] = useState<string>();
  const [saving, setSaving] = useState<"idle" | "pending" | "saving" | "error">("idle");
  const [publishing, setPublishing] = useState<Check>();
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const edits = useRef(0);
  const versionRef = useRef(0);

  // 打开（或取回）草稿
  useEffect(() => {
    api
      .post<Draft>(`/flow-templates/${id}/draft`)
      .then((d) => {
        setDraft(d);
        setDef(d.definition);
        versionRef.current = d.version;
        setActive(nodeIds(d.definition.flow)[0]);
      })
      .catch((e) => message.error(errorText(e)));
  }, [id, message]);

  const save = useCallback(
    async (next: Definition) => {
      const mine = edits.current;
      setSaving("saving");
      try {
        const d = await api.put<Draft>(`/flow-templates/${id}/draft`, { definition: next, version: versionRef.current });
        versionRef.current = d.version;
        setDraft(d);
        // 保存期间又有新编辑：保留本地内容，等下一次保存
        if (edits.current === mine) setDef(d.definition);
        setSaving(edits.current === mine ? "idle" : "pending");
      } catch (e) {
        setSaving("error");
        if (e instanceof ApiError && (e.code === "DRAFT_LOCKED" || e.code === "STALE_ROW_VERSION")) {
          modal.warning({ title: "无法保存", content: e.message, onOk: () => window.location.reload() });
        } else message.error(errorText(e));
      }
    },
    [id, message, modal],
  );

  function change(next: Definition, focus?: string) {
    edits.current += 1;
    setDef(next);
    if (focus) setActive(focus);
    setSaving("pending");
    clearTimeout(timer.current);
    timer.current = setTimeout(() => void save(next), 700);
  }

  if (!t || !draft || !def) return <Spin />;
  const locked = !draft.editing_by_me && !!draft.editor_id;

  async function takeover() {
    try {
      const d = await api.post<Draft>(`/flow-templates/${id}/draft/takeover`);
      setDraft(d);
      setDef(d.definition);
      versionRef.current = d.version;
    } catch (e) {
      message.error(errorText(e));
    }
  }
  async function discard() {
    modal.confirm({
      title: "放弃草稿？",
      content: "草稿中的修改将丢失，模板保持当前版本",
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await api.del(`/flow-templates/${id}/draft`);
          await qc.invalidateQueries({ queryKey: ["flow-template", id] });
          router.replace(`/templates/flow/${id}`);
        } catch (e) {
          message.error(errorText(e));
        }
      },
    });
  }
  async function openPublish() {
    clearTimeout(timer.current);
    if (saving === "pending") await save(def!);
    try {
      setPublishing(await api.post<Check>(`/flow-templates/${id}/draft/check`));
    } catch (e) {
      message.error(errorText(e));
    }
  }

  const node = active ? def.nodes[active] : undefined;
  const issues = [...draft.errors, ...draft.warnings];

  return (
    <>
      <Breadcrumb
        items={[
          { title: <Link href="/templates/flow">流程模板</Link> },
          { title: <Link href={`/templates/flow/${id}`}>{t.name}</Link> },
          { title: "编辑草稿" },
        ]}
        style={{ marginBottom: 12 }}
      />
      <PageHead
        title={`编辑：${t.name}`}
        sub={
          <>
            基于 {t.current_version?.label ?? "空白"} · {saving === "saving" ? "保存中…" : saving === "pending" ? "有未保存的修改" : saving === "error" ? "保存失败" : `已自动保存 ${dt(draft.updated_at)}`}
          </>
        }
        extra={
          <Space>
            {t.current_version && (
              <Button danger onClick={discard} disabled={locked}>
                放弃草稿
              </Button>
            )}
            <Button type="primary" onClick={openPublish} disabled={locked}>
              发布…
            </Button>
          </Space>
        }
      />
      {locked && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={`${draft.editor_name ?? "他人"} 正在编辑这份草稿`}
          description="同一模板同一时间只能一人编辑。接管后对方将无法继续保存。"
          action={<Button onClick={takeover}>接管编辑</Button>}
        />
      )}
      <Card size="small" title="结构预览" style={{ marginBottom: 16 }}>
        <FlowView definition={def} active={active} onPick={setActive} issues={draft.errors} />
      </Card>
      <Row gutter={16}>
        <Col span={9}>
          <Card size="small" title="步骤">
            <StepList def={def} active={active} setActive={setActive} change={change} disabled={locked} />
          </Card>
          <Card size="small" title={`校验 · ${draft.errors.length} 错误 · ${draft.warnings.length} 警告`} style={{ marginTop: 16 }}>
            {!issues.length ? (
              <span style={{ color: "#16a34a" }}>✓ 可以发布</span>
            ) : (
              <List
                size="small"
                dataSource={issues}
                renderItem={(i) => (
                  <List.Item style={{ cursor: i.node ? "pointer" : undefined }} onClick={() => i.node && setActive(i.node)}>
                    <Space align="start">
                      {draft.errors.includes(i) ? <Tag color="red">错误</Tag> : <Tag color="orange">警告</Tag>}
                      <span style={{ fontSize: 13 }}>{i.message}</span>
                    </Space>
                  </List.Item>
                )}
              />
            )}
          </Card>
        </Col>
        <Col span={15}>
          <Card size="small" title={node ? `节点配置：${node.name || "未命名"}` : "节点配置"}>
            {node && active ? (
              <NodeForm
                key={active}
                node={node}
                disabled={locked}
                onChange={(n) => change({ ...def, nodes: { ...def.nodes, [active]: n } })}
              />
            ) : (
              <EmptyBox text="在结构中选择一个节点" />
            )}
          </Card>
        </Col>
      </Row>
      {publishing && (
        <PublishModal
          id={id}
          check={publishing}
          onClose={() => setPublishing(undefined)}
          onDone={async () => {
            await qc.invalidateQueries({ queryKey: ["flow-template", id] });
            await qc.invalidateQueries({ queryKey: ["flow-templates"] });
            router.replace(`/templates/flow/${id}`);
          }}
        />
      )}
    </>
  );
}

function StepList({
  def,
  active,
  setActive,
  change,
  disabled,
}: {
  def: Definition;
  active?: string;
  setActive: (id: string) => void;
  change: (d: Definition, focus?: string) => void;
  disabled: boolean;
}) {
  const total = nodeIds(def.flow).length;
  const chip = (id: string) => (
    <Space key={id} size={2}>
      <Tag
        color={active === id ? "geekblue" : undefined}
        style={{ cursor: "pointer", padding: "2px 8px", fontSize: 13 }}
        onClick={() => setActive(id)}
      >
        {def.nodes[id]?.name || "未命名"}
      </Tag>
      {!disabled && total > 1 && (
        <Button size="small" type="text" icon={<DeleteOutlined />} onClick={() => change(removeNode(def, id))} />
      )}
    </Space>
  );
  const insertMenu = (i: number) => ({
    items: [
      { key: "node", label: "插入节点" },
      { key: "par", label: "插入并行组" },
    ],
    onClick: ({ key }: { key: string }) => {
      const [d, focus] = key === "node" ? addNode(def, i) : addParallel(def, i);
      change(d, focus);
    },
  });
  return (
    <Space direction="vertical" style={{ width: "100%" }} size={8}>
      {def.flow.map((s, i) => (
        <div key={i} style={{ border: "1px solid #eef0f4", borderRadius: 10, padding: "8px 10px", background: s.type === "parallel" ? "#fafaff" : "#fff" }}>
          <Space style={{ width: "100%", justifyContent: "space-between" }} align="start">
            <div>
              <span className="tf-muted" style={{ fontSize: 12, marginRight: 6 }}>
                {i + 1}.
              </span>
              {s.type === "node" ? (
                chip(s.id)
              ) : (
                <Space direction="vertical" size={6}>
                  <span style={{ fontSize: 12, color: "#6366f1", fontWeight: 600 }}>并行组</span>
                  {s.tracks.map((tr, ti) => (
                    <Space key={ti} wrap size={4}>
                      <span className="tf-muted" style={{ fontSize: 12 }}>
                        轨道 {ti + 1}
                      </span>
                      {tr.nodes.map((n, ni) => (
                        <Space key={n} size={2}>
                          {ni > 0 && <span className="tf-muted">→</span>}
                          {chip(n)}
                        </Space>
                      ))}
                      {!disabled && (
                        <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={() => change(...addToTrack(def, i, ti))} />
                      )}
                    </Space>
                  ))}
                  {!disabled && (
                    <Button size="small" type="dashed" onClick={() => change(...addTrack(def, i))}>
                      ＋ 轨道
                    </Button>
                  )}
                </Space>
              )}
            </div>
            {!disabled && (
              <Space size={0}>
                <Button size="small" type="text" icon={<ArrowUpOutlined />} disabled={i === 0} onClick={() => change(moveStep(def, i, -1))} />
                <Button
                  size="small"
                  type="text"
                  icon={<ArrowDownOutlined />}
                  disabled={i === def.flow.length - 1}
                  onClick={() => change(moveStep(def, i, 1))}
                />
                <Dropdown menu={insertMenu(i)}>
                  <Button size="small" type="text" icon={<PlusOutlined />} />
                </Dropdown>
              </Space>
            )}
          </Space>
        </div>
      ))}
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        ＋ 在该步之后插入节点或并行组；并行组内每条轨道是一串顺序节点；不支持嵌套。
      </Typography.Text>
    </Space>
  );
}

function NodeForm({ node, onChange, disabled }: { node: NodeConfig; onChange: (n: NodeConfig) => void; disabled: boolean }) {
  const { data: roles = [] } = useQuery({
    queryKey: ["flow-roles"],
    queryFn: () => api.get<{ name: string }[]>("/flow-templates/roles"),
  });
  const { data: files = [] } = useQuery({
    queryKey: ["file-templates", "active-artifacts"],
    queryFn: () => api.get<FileTemplate[]>("/file-templates", { status: "active" }),
  });
  const roleOptions = roles.map((r) => ({ value: r.name }));
  const set = (patch: Partial<NodeConfig>) => onChange({ ...node, ...patch });
  return (
    <Form layout="vertical" disabled={disabled}>
      <Row gutter={12}>
        <Col span={12}>
          <Form.Item label="节点名称" required>
            <Input value={node.name} maxLength={32} onChange={(e) => set({ name: e.target.value })} />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item label="执行角色" required extra="项目中分配：人 + 1 个 Agent">
            <AutoComplete
              value={node.exec_role}
              options={roleOptions}
              filterOption={(i, o) => String(o?.value ?? "").includes(i)}
              onChange={(v) => set({ exec_role: v })}
              placeholder="如：架构师"
            />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item label="产物名称" required>
            <Input value={node.output_name} maxLength={32} onChange={(e) => set({ output_name: e.target.value })} placeholder="如：设计文档" />
          </Form.Item>
        </Col>
        <Col span={12}>
          <Form.Item label="产物文件模板" extra="Agent 按模板格式产出；不选则按通用格式">
            <Select
              allowClear
              value={node.artifact_file_template_id ?? undefined}
              onChange={(v) => set({ artifact_file_template_id: v ?? null })}
              options={files.map((f) => ({ value: f.id, label: `${f.icon ?? "📄"} ${f.name}${f.current_version ? "" : "（未上传）"}` }))}
            />
          </Form.Item>
        </Col>
      </Row>
      <Divider style={{ margin: "4px 0 12px" }} />
      <Form.Item label="准出评审（产出方把关质量）" required>
        <Space>
          <AutoComplete
            style={{ width: 220 }}
            value={node.exit_review.role}
            options={roleOptions}
            filterOption={(i, o) => String(o?.value ?? "").includes(i)}
            onChange={(v) => set({ exit_review: { ...node.exit_review, role: v } })}
            placeholder="评审角色"
          />
          <Radio.Group
            value={node.exit_review.rule}
            onChange={(e) => set({ exit_review: { ...node.exit_review, rule: e.target.value } })}
            options={[
              { value: "any", label: "任一人同意" },
              { value: "all", label: "所有人同意" },
            ]}
          />
        </Space>
      </Form.Item>
      <Form.Item label="准入评审（下游把关能否作为输入）">
        <Space direction="vertical">
          <Switch
            checked={!!node.admit_review}
            checkedChildren="需要准入"
            unCheckedChildren="不需要"
            onChange={(v) => set({ admit_review: v ? { role: "", rule: "all" } : null })}
          />
          {node.admit_review && (
            <Space>
              <AutoComplete
                style={{ width: 220 }}
                value={node.admit_review.role}
                options={roleOptions}
                filterOption={(i, o) => String(o?.value ?? "").includes(i)}
                onChange={(v) => set({ admit_review: { ...node.admit_review!, role: v } })}
                placeholder="评审角色"
              />
              <Radio.Group
                value={node.admit_review.rule}
                onChange={(e) => set({ admit_review: { ...node.admit_review!, rule: e.target.value } })}
                options={[
                  { value: "any", label: "任一人同意" },
                  { value: "all", label: "所有人同意" },
                ]}
              />
            </Space>
          )}
        </Space>
      </Form.Item>
    </Form>
  );
}

function PublishModal({ id, check, onClose, onDone }: { id: string; check: Check; onClose: () => void; onDone: () => void }) {
  const { message } = App.useApp();
  const [note, setNote] = useState("");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const blocked = check.errors.length > 0;
  async function submit() {
    if (!note.trim()) return message.warning("请填写更新说明");
    setBusy(true);
    try {
      await api.post(`/flow-templates/${id}/publish`, { change_note: note, acknowledged_warnings: ack });
      message.success("已发布");
      onDone();
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      open
      title="发布新版本"
      onCancel={onClose}
      onOk={submit}
      confirmLoading={busy}
      okText="发布"
      okButtonProps={{ disabled: blocked || (check.warnings.length > 0 && !ack) }}
      width={580}
    >
      <Space direction="vertical" style={{ width: "100%" }}>
        {blocked && (
          <Alert
            type="error"
            showIcon
            message={`有 ${check.errors.length} 个错误，修正后才能发布`}
            description={check.errors.map((e) => e.message).join("；")}
          />
        )}
        {check.warnings.length > 0 && (
          <Alert
            type="warning"
            showIcon
            message="警告"
            description={
              <Space direction="vertical">
                {check.warnings.map((w, i) => (
                  <span key={i}>{w.message}</span>
                ))}
                <Checkbox checked={ack} onChange={(e) => setAck(e.target.checked)}>
                  我已知晓，仍要发布
                </Checkbox>
              </Space>
            }
          />
        )}
        <div>
          <div className="tf-muted" style={{ fontSize: 12.5, marginBottom: 4 }}>
            发布后的角色
          </div>
          <RoleTags roles={check.roles} />
        </div>
        <Input.TextArea placeholder="更新说明（必填）" maxLength={500} rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          发布后版本不可修改；绑定该模板的项目在之后新发起的流程中使用新版本，进行中的流程不受影响。
        </Typography.Text>
      </Space>
    </Modal>
  );
}
