"use client";

/** 「项目」直接进当前团队的第一个项目；没有项目、没有团队时说明下一步做什么 */
import { Button } from "antd";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { NewProjectModal, NewTeamModal } from "@/components/shell/AppShell";
import { Empty, Loading, Page } from "@/components/ui";
import { useProjects } from "@/lib/queries";
import { useCan, useCurrentTeam } from "@/lib/team";

export default function ProjectsHome() {
  const router = useRouter();
  const { team, teams, loading, setTeam } = useCurrentTeam();
  const can = useCan();
  const { data: projects, isPending } = useProjects(team?.id);
  const [creating, setCreating] = useState<"team" | "project" | null>(null);
  const first = projects?.find((p) => p.status === "active");
  useEffect(() => {
    if (first) router.replace(`/projects/${first.id}`);
  }, [first, router]);

  if (loading || (team && isPending) || first) return <Loading />;
  return (
    <Page>
      <div className="card">
        {!teams.length ? (
          <Empty icon="users" title="你还不在任何团队里">
            {can.createTeam ? "新建一个团队，把成员和数字员工拉进来" : "请团队管理员把你加进团队"}
            {can.createTeam && (
              <div style={{ marginTop: 12 }}>
                <Button type="primary" onClick={() => setCreating("team")}>
                  新建团队
                </Button>
              </div>
            )}
          </Empty>
        ) : (
          <Empty title={`「${team?.name}」还没有项目`}>
            {can.configFlow ? "新建项目，绑定流程模板、分配角色后就能发起流程" : "等团队管理员建好项目"}
            {can.configFlow && (
              <div style={{ marginTop: 12 }}>
                <Button type="primary" onClick={() => setCreating("project")}>
                  新建项目
                </Button>
              </div>
            )}
          </Empty>
        )}
      </div>
      {creating === "team" && <NewTeamModal onClose={() => setCreating(null)} onCreated={(t) => setTeam(t.id)} />}
      {creating === "project" && team && <NewProjectModal team={team} onClose={() => setCreating(null)} />}
    </Page>
  );
}
