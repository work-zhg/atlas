"use client";

import { DownloadOutlined, UploadOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Breadcrumb, Button, Card, Col, Descriptions, Form, Input, List, Modal, Popconfirm, Row, Space, Spin, Tag, Upload } from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useState } from "react";

import { EmptyBox, PageHead, StatusTag } from "@/components/common";
import { api, errorText, upload } from "@/lib/api";
import { dt, size, USAGE } from "@/lib/format";
import { useCan } from "@/lib/session";
import type { FileTemplate, FileVersion } from "@/lib/types";

export default function FileTemplatePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const can = useCan();
  const qc = useQueryClient();
  const router = useRouter();
  const { message } = App.useApp();
  const [viewing, setViewing] = useState<number>();
  const [uploading, setUploading] = useState(false);
  const { data: t, error } = useQuery({ queryKey: ["file-template", id], queryFn: () => api.get<FileTemplate>(`/file-templates/${id}`) });
  const { data: old } = useQuery({
    queryKey: ["file-template", id, "v", viewing],
    queryFn: () => api.get<FileVersion>(`/file-templates/${id}/versions/${viewing}`),
    enabled: viewing !== undefined && viewing !== t?.current_version?.version_no,
  });
  if (error) return <EmptyBox text={errorText(error)} />;
  if (!t) return <Spin />;
  const manage = can("file_template:manage");
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["file-template", id] });
    void qc.invalidateQueries({ queryKey: ["file-templates"] });
  };
  const showing = viewing && old && viewing !== t.current_version?.version_no ? old : undefined;
  const content = showing ? showing.content : t.content;

  async function setStatus(active: boolean) {
    try {
      await api.post(`/file-templates/${id}/${active ? "enable" : "disable"}`);
      refresh();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  async function remove() {
    try {
      await api.del(`/file-templates/${id}`);
      await qc.invalidateQueries({ queryKey: ["file-templates"] });
      router.replace("/templates/file");
    } catch (e) {
      message.error(errorText(e));
    }
  }

  return (
    <>
      <Breadcrumb items={[{ title: <Link href="/templates/file">文件模板</Link> }, { title: t.name }]} style={{ marginBottom: 12 }} />
      <PageHead
        title={
          <Space>
            {t.icon} {t.name} <StatusTag status={t.status} />
          </Space>
        }
        sub={t.description || undefined}
        extra={
          manage && (
            <Space>
              <EditInfo t={t} onDone={refresh} />
              <Button type="primary" icon={<UploadOutlined />} disabled={t.status !== "active"} onClick={() => setUploading(true)}>
                上传新版本
              </Button>
              {t.status === "active" ? (
                <Popconfirm title="停用后流程模板不能再引用它，已引用的不受影响" onConfirm={() => setStatus(false)}>
                  <Button>停用</Button>
                </Popconfirm>
              ) : (
                <Button onClick={() => setStatus(true)}>启用</Button>
              )}
              <Popconfirm title="删除后不可恢复。被流程模板引用过的不能删除" onConfirm={remove}>
                <Button danger>删除</Button>
              </Popconfirm>
            </Space>
          )
        }
      />
      <Row gutter={16}>
        <Col span={16}>
          <Card
            size="small"
            title={showing ? `v${showing.version_no}（历史版本）` : t.current_version ? `v${t.current_version.version_no}（生效）· ${t.current_version.file_name}` : "内容"}
            extra={
              (showing ?? t.current_version) && (
                <a href={`/api/v1/file-templates/${id}/versions/${(showing ?? t.current_version)!.version_no}/download`}>
                  <DownloadOutlined /> 下载
                </a>
              )
            }
          >
            {content ? <pre className="tf-md">{content}</pre> : <EmptyBox text="还没有上传内容" />}
          </Card>
        </Col>
        <Col span={8}>
          <Space direction="vertical" style={{ width: "100%" }} size={16}>
            <Card size="small" title="信息">
              <Descriptions column={1} size="small">
                <Descriptions.Item label="用途">{USAGE[t.usage]}</Descriptions.Item>
                <Descriptions.Item label="大小">{t.current_version ? size(t.current_version.size_bytes) : "—"}</Descriptions.Item>
                <Descriptions.Item label="更新">{dt(t.updated_at)}</Descriptions.Item>
              </Descriptions>
            </Card>
            <Card size="small" title={`版本 ${t.versions?.length ?? 0}`}>
              <List
                size="small"
                dataSource={t.versions}
                renderItem={(v) => (
                  <List.Item
                    style={{ cursor: "pointer", background: (viewing ?? t.current_version?.version_no) === v.version_no ? "#eef0ff" : undefined }}
                    onClick={() => setViewing(v.version_no)}
                  >
                    <Space direction="vertical" size={0}>
                      <Space>
                        <b>v{v.version_no}</b>
                        {v.id === t.current_version?.id && <Tag color="green">生效</Tag>}
                      </Space>
                      <span style={{ fontSize: 12.5 }}>{v.change_note}</span>
                      <span className="tf-muted" style={{ fontSize: 12 }}>
                        {v.uploaded_by_name ?? "—"} · {dt(v.uploaded_at)}
                      </span>
                    </Space>
                  </List.Item>
                )}
              />
            </Card>
            <Card size="small" title={`被引用 ${t.references?.length ?? 0}`}>
              <List
                size="small"
                dataSource={t.references}
                locale={{ emptyText: "没有流程模板引用" }}
                renderItem={(r) => (
                  <List.Item>
                    <Link href={`/templates/flow/${r.flow_template_id}`}>🧭 {r.flow_template}</Link>
                    <span className="tf-muted">
                      {r.node} · {r.in}
                    </span>
                  </List.Item>
                )}
              />
            </Card>
          </Space>
        </Col>
      </Row>
      <UploadModal t={t} open={uploading} onClose={() => setUploading(false)} onDone={() => (setViewing(undefined), refresh())} />
    </>
  );
}

function EditInfo({ t, onDone }: { t: FileTemplate; onDone: () => void }) {
  const { message } = App.useApp();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  async function submit() {
    const v = await form.validateFields();
    try {
      await api.patch(`/file-templates/${t.id}`, { ...v, version: t.version });
      onDone();
      setOpen(false);
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <>
      <Button onClick={() => setOpen(true)}>编辑信息</Button>
      <Modal open={open} title="编辑信息" onOk={submit} onCancel={() => setOpen(false)} destroyOnHidden>
        <Form form={form} layout="vertical" initialValues={{ name: t.name, icon: t.icon, description: t.description }}>
          <Form.Item name="name" label="名称" rules={[{ required: true }]}>
            <Input maxLength={64} />
          </Form.Item>
          <Form.Item name="icon" label="图标">
            <Input maxLength={4} style={{ width: 80 }} />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input.TextArea maxLength={2000} rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

function UploadModal({ t, open, onClose, onDone }: { t: FileTemplate; open: boolean; onClose: () => void; onDone: () => void }) {
  const { message } = App.useApp();
  const [file, setFile] = useState<File>();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit() {
    if (!file) return message.warning("请选择 Markdown 文件");
    if (!note.trim()) return message.warning("请填写更新说明");
    const form = new FormData();
    form.set("file", file);
    form.set("change_note", note);
    form.set("version", String(t.version));
    setBusy(true);
    try {
      await upload(`/file-templates/${t.id}/versions`, form);
      message.success("已上传，新版本立即生效");
      setFile(undefined);
      setNote("");
      onDone();
      onClose();
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal open={open} title="上传新版本" onOk={submit} onCancel={onClose} confirmLoading={busy} destroyOnHidden>
      <Space direction="vertical" style={{ width: "100%" }}>
        <Upload.Dragger
          accept=".md,.markdown"
          maxCount={1}
          beforeUpload={(f) => (setFile(f), false)}
          onRemove={() => setFile(undefined)}
          fileList={file ? [{ uid: "1", name: file.name, status: "done" }] : []}
        >
          <p style={{ fontSize: 28, margin: 0 }}>📄</p>
          <p>点击或拖入 Markdown 文件（.md，≤ 100 KB，UTF-8）</p>
        </Upload.Dragger>
        <Input.TextArea placeholder="更新说明（必填）" maxLength={500} rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
      </Space>
    </Modal>
  );
}
