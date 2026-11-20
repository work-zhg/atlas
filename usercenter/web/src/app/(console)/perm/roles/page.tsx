"use client";

/**
 * 权限 › 角色：全部应用的角色集中在这里 —— 左边按应用分组（可筛选应用），右边看 / 改一个角色。
 * ?app=<应用 id> 预先筛好（应用详情「操作」页签跳过来时带）。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Card, Empty, Select, Spin, Tag } from "antd";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { AppIcon, PageHead } from "@/components/common";
import { useAllRoles } from "@/components/grantPickers";
import { Icon } from "@/components/Icon";
import { RoleDetail, RoleForm } from "@/components/roles";
import { api } from "@/lib/api";
import { useCan } from "@/lib/session";
import type { AppInfo } from "@/lib/types";

export default function RolesPage() {
  return (
    <Suspense>
      <RolesInner />
    </Suspense>
  );
}

function RolesInner() {
  const can = useCan();
  const qc = useQueryClient();
  const router = useRouter();
  const params = useSearchParams();
  const { message } = App.useApp();
  const appFilter = params.get("app") ?? undefined;
  const [sel, setSel] = useState<string>();
  const [form, setForm] = useState<{ open: boolean; mode?: "new" | "edit" | "copy" }>({ open: false });
  const all = useAllRoles();
  // 新建角色时选应用：全部应用（还没有角色的也要能选）
  const apps = useQuery({ queryKey: ["apps"], queryFn: () => api.get<AppInfo[]>("/apps") });
  const groups = (all.data ?? []).filter((g) => !appFilter || g.app.id === appFilter);
  const roles = groups.flatMap((g) => g.roles.map((role) => ({ role, app: g.app })));
  const current = roles.find((x) => x.role.id === sel) ?? roles[0];

  const setApp = (v?: string) => {
    setSel(undefined);
    router.replace(v ? `/perm/roles?app=${v}` : "/perm/roles");
  };

  return (
    <>
      <PageHead
        title="角色"
        sub="角色属于应用：一组操作权限与可见菜单。授给人、部门或放进岗位后生效。"
        extra={
          can("role:manage") && (
            <Button type="primary" icon={<Icon name="plus" size={14} />} onClick={() => setForm({ open: true, mode: "new" })}>
              新建角色
            </Button>
          )
        }
      />
      <div className="uc-split" style={{ gridTemplateColumns: "260px minmax(0, 1fr)" }}>
        <div className="uc-panel">
          <div className="uc-panel-head">
            <Select
              allowClear
              style={{ width: "100%" }}
              placeholder="全部应用"
              value={appFilter}
              onChange={setApp}
              options={(apps.data ?? []).map((a) => ({ value: a.id, label: a.name }))}
            />
          </div>
          <div className="uc-panel-body">
            {all.isLoading && <Spin style={{ display: "block", margin: 24 }} />}
            {groups.map((g) => (
              <div key={g.app.id}>
                <div className="uc-group-label">
                  <AppIcon app={g.app} size={16} />
                  <span className="uc-grow">{g.app.name}</span>
                  {g.roles.length}
                </div>
                {g.roles.map((r) => (
                  <button key={r.id} type="button" className="uc-li" aria-current={r.id === current?.role.id} onClick={() => setSel(r.id)}>
                    <div className="uc-grow">
                      <div className="uc-row" style={{ gap: 6 }}>
                        <b style={{ fontSize: 13.5 }}>{r.name}</b>
                        {(r.is_builtin || r.shared) && <Tag style={{ marginInlineEnd: 0 }}>内置</Tag>}
                      </div>
                      <div className="uc-sub uc-mono">{r.code}</div>
                    </div>
                    <span className="uc-sub uc-nw">{r.holder_count ?? 0} 人</span>
                  </button>
                ))}
              </div>
            ))}
          </div>
        </div>
        {current ? (
          <RoleDetail
            key={current.role.id}
            app={current.app}
            roleId={current.role.id}
            onEdit={(mode) => setForm({ open: true, mode })}
            onDeleted={() => setSel(undefined)}
          />
        ) : (
          <Card>{!all.isLoading && <Empty description="还没有角色：先在应用的「操作」里登记操作，再新建角色" />}</Card>
        )}
      </div>
      {form.open && (
        <RoleForm
          app={form.mode === "new" ? apps.data?.find((a) => a.id === appFilter) : current?.app}
          apps={apps.data ?? []}
          mode={form.mode!}
          role={form.mode === "new" ? null : (current?.role ?? null)}
          onClose={() => setForm({ open: false })}
          onDone={(id, appId) => {
            qc.invalidateQueries({ queryKey: ["roles"] });
            qc.invalidateQueries({ queryKey: ["app", appId] });
            setSel(id);
            message.success("已保存");
          }}
        />
      )}
    </>
  );
}
