"use client";

import { useQuery } from "@tanstack/react-query";
import { Checkbox, Empty, Spin } from "antd";

import { AppIcon } from "@/components/common";
import { api } from "@/lib/api";
import type { AppInfo, PositionRow, Role } from "@/lib/types";

export function usePositions(enabled = true) {
  return useQuery({ queryKey: ["positions"], queryFn: () => api.get<PositionRow[]>("/positions"), enabled });
}

/** 所有应用的角色（按应用分组）。需要 app:manage 或 role:view。 */
export function useAllRoles(enabled = true) {
  return useQuery({
    queryKey: ["roles", "all"],
    enabled,
    queryFn: async () => {
      const apps = await api.get<AppInfo[]>("/apps");
      const groups = await Promise.all(apps.map(async (a) => ({ app: a, roles: await api.get<Role[]>(`/apps/${a.id}/roles`) })));
      return groups.filter((g) => g.roles.length);
    },
  });
}

/** 按应用分组勾选角色（授予角色、岗位的默认角色共用） */
export function RoleChecks({ value, onChange, disabled = [] }: { value: string[]; onChange: (v: string[]) => void; disabled?: string[] }) {
  const { data, isLoading } = useAllRoles();
  if (isLoading) return <Spin />;
  if (!data?.length) return <Empty description="没有可选的角色" />;
  const toggle = (id: string, on: boolean) => onChange(on ? [...value, id] : value.filter((x) => x !== id));
  return (
    <div style={{ maxHeight: 400, overflow: "auto", display: "flex", flexDirection: "column", gap: 10 }}>
      {data.map((g) => (
        <div key={g.app.id} style={{ border: "1px solid var(--line)", borderRadius: 10, overflow: "hidden" }}>
          <div className="uc-row" style={{ padding: "8px 12px", background: "var(--bg-side)", borderBottom: "1px solid var(--line)", fontWeight: 600 }}>
            <AppIcon app={g.app} size={20} />
            {g.app.name}
          </div>
          {g.roles.map((r) => (
            <div key={r.id} style={{ padding: "7px 12px" }}>
              <Checkbox checked={value.includes(r.id) || disabled.includes(r.id)} disabled={disabled.includes(r.id)} onChange={(e) => toggle(r.id, e.target.checked)}>
                <b>{r.name}</b> <span className="uc-mono uc-muted">{r.code}</span>
                {disabled.includes(r.id) && <span className="uc-sub">（已授予）</span>}
                {r.description && <div className="uc-sub">{r.description}</div>}
              </Checkbox>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
