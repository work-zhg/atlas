"use client";

/** 团队流程模板的编排：草稿带 rev 乐观锁（两个人同时改，后存的会冲突），发布出新版本 */
import { useQueryClient } from "@tanstack/react-query";
import { Tag } from "antd";
import { useParams } from "next/navigation";
import { useRef } from "react";

import { TemplateEditor } from "@/components/TemplateEditor";
import { Icon, LoadError, Loading } from "@/components/ui";
import { api } from "@/lib/api";
import { useTemplate } from "@/lib/queries";
import { useFollowTeam } from "@/lib/team";
import type { Issue, Structure } from "@/lib/types";

export default function TeamTemplateEditor() {
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();
  const t = useTemplate(id);
  useFollowTeam(t.data?.team_id);
  const rev = useRef<number | null>(null);

  if (t.error) return <LoadError error={t.error} onRetry={() => void t.refetch()} />;
  if (!t.data?.draft) return <Loading />;
  const tpl = t.data;
  const draft = tpl.draft!;
  if (rev.current === null) rev.current = draft.rev;

  const save = async (structure: Structure) => {
    const r = await api.put<{ rev: number; errors: Issue[]; warnings: Issue[] }>(`/templates/${id}/draft`, { structure, rev: rev.current });
    rev.current = r.rev;
    return r;
  };
  const publish = async (note: string) => {
    await api.post(`/templates/${id}/publish`, { note: note || null });
    await qc.invalidateQueries({ queryKey: ["template", id] });
    await qc.invalidateQueries({ queryKey: ["team", tpl.team_id, "templates"] });
  };

  return (
    <TemplateEditor
      key={tpl.id}
      title={tpl.name}
      backHref="/team/templates"
      structure={draft.structure}
      nextVersion={tpl.latest_version_no + 1}
      save={save}
      publish={publish}
      roleHint="角色只是占位：在项目配置里给每个角色分配成员和数字员工"
      publishHint="发布后：新发起的流程用这一版；进行中的流程仍锁在各自发起时的版本。绑定了这个模板的项目跟着用新版本，新版本多了角色时要在项目配置里补上"
      tags={
        <>
          {tpl.latest_version_no > 0 && <Tag>v{tpl.latest_version_no} 已发布</Tag>}
          <Tag color="warning">草稿 v{tpl.latest_version_no + 1} 编辑中</Tag>
          {tpl.origin && <Tag icon={<Icon name="grid" size={11} />}>拷贝自流程市场 v{tpl.origin.version}</Tag>}
        </>
      }
    />
  );
}
