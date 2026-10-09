"use client";

import { EditOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Breadcrumb, Button, Card, Col, Descriptions, List, Popconfirm, Row, Space, Spin, Tag } from "antd";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useState } from "react";

import { EmptyBox, PageHead, StatusTag } from "@/components/common";
import { FlowView, RoleTags } from "@/components/FlowView";
import { api, errorText } from "@/lib/api";
import { dt } from "@/lib/format";
import { useCan } from "@/lib/session";
import type { FlowTemplate, FlowVersion } from "@/lib/types";

export default function FlowTemplatePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const can = useCan();
  const qc = useQueryClient();
  const router = useRouter();
  const { message } = App.useApp();
  const [label, setLabel] = useState<string>();
  const { data: t, error } = useQuery({ queryKey: ["flow-template", id], queryFn: () => api.get<FlowTemplate>(`/flow-templates/${id}`) });
  const { data: old } = useQuery({
    queryKey: ["flow-template", id, "v", label],
    queryFn: () => api.get<FlowVersion>(`/flow-templates/${id}/versions/${label}`),
    enabled: !!label && label !== t?.current_version?.label,
  });
  if (error) return <EmptyBox text={errorText(error)} />;
  if (!t) return <Spin />;
  const manage = can("flow_template:manage");
  const showing = label && old && label !== t.current_version?.label ? old : undefined;
  const definition = showing?.definition ?? t.definition;
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["flow-template", id] });
    void qc.invalidateQueries({ queryKey: ["flow-templates"] });
  };

  async function act(path: string) {
    try {
      await api.post(`/flow-templates/${id}/${path}`);
      refresh();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  async function remove() {
    try {
      await api.del(`/flow-templates/${id}`);
      await qc.invalidateQueries({ queryKey: ["flow-templates"] });
      router.replace("/templates/flow");
    } catch (e) {
      message.error(errorText(e));
    }
  }

  return (
    <>
      <Breadcrumb items={[{ title: <Link href="/templates/flow">流程模板</Link> }, { title: t.name }]} style={{ marginBottom: 12 }} />
      <PageHead
        title={
          <Space>
            {t.icon ?? "🧭"} {t.name}
            {t.current_version ? <Tag color="blue">{t.current_version.label}</Tag> : <Tag>未发布</Tag>}
            <StatusTag status={t.status} />
          </Space>
        }
        sub={t.description || undefined}
        extra={
          manage && (
            <Space>
              <Link href={`/templates/flow/${id}/edit`}>
                <Button type="primary" icon={<EditOutlined />}>
                  {t.has_draft ? `继续编辑草稿${t.draft_editor_name ? `（${t.draft_editor_name}）` : ""}` : "编辑"}
                </Button>
              </Link>
              {t.status === "active" ? (
                <Popconfirm title="停用后新项目不能绑定；已绑定的项目照常发起流程" onConfirm={() => act("disable")}>
                  <Button>停用</Button>
                </Popconfirm>
              ) : (
                <Button onClick={() => act("enable")}>启用</Button>
              )}
              <Popconfirm title="删除后不可恢复。被项目绑定过的模板不能删除" onConfirm={remove}>
                <Button danger>删除</Button>
              </Popconfirm>
            </Space>
          )
        }
      />
      <Row gutter={16}>
        <Col span={17}>
          <Card size="small" title={showing ? `${showing.label}（历史版本，只读）` : t.current_version ? `${t.current_version.label}（当前版本）` : "结构"}>
            {definition ? <FlowView definition={definition} /> : <EmptyBox text="还没有发布任何版本" />}
          </Card>
          {(showing ?? t.current_version) && (
            <Card size="small" title="角色" style={{ marginTop: 16 }}>
              <RoleTags roles={(showing ?? t.current_version)!.roles} />
            </Card>
          )}
        </Col>
        <Col span={7}>
          <Space direction="vertical" style={{ width: "100%" }} size={16}>
            <Card size="small" title="信息">
              <Descriptions column={1} size="small">
                <Descriptions.Item label="适用范围">{t.scope || "—"}</Descriptions.Item>
                <Descriptions.Item label="节点数">{t.node_count}</Descriptions.Item>
                <Descriptions.Item label="更新">{dt(t.updated_at)}</Descriptions.Item>
              </Descriptions>
            </Card>
            <Card size="small" title={`版本 ${t.versions?.length ?? 0}`}>
              <List
                size="small"
                dataSource={t.versions}
                locale={{ emptyText: "未发布" }}
                renderItem={(v) => (
                  <List.Item
                    style={{ cursor: "pointer", background: (label ?? t.current_version?.label) === v.label ? "#eef0ff" : undefined }}
                    onClick={() => setLabel(v.label)}
                  >
                    <Space direction="vertical" size={0}>
                      <Space>
                        <b>{v.label}</b>
                        {v.id === t.current_version?.id && <Tag color="green">当前</Tag>}
                      </Space>
                      <span style={{ fontSize: 12.5 }}>{v.change_note}</span>
                      <span className="tf-muted" style={{ fontSize: 12 }}>
                        {v.published_by_name ?? "—"} · {dt(v.published_at)}
                      </span>
                    </Space>
                  </List.Item>
                )}
              />
            </Card>
            <Card size="small" title={`绑定的项目 ${t.projects?.length ?? 0}`}>
              <List
                size="small"
                dataSource={t.projects}
                locale={{ emptyText: "没有项目绑定" }}
                renderItem={(p) => (
                  <List.Item>
                    📁 {p.name}
                    {p.status === "archived" && <StatusTag status="archived" />}
                  </List.Item>
                )}
              />
            </Card>
          </Space>
        </Col>
      </Row>
    </>
  );
}
