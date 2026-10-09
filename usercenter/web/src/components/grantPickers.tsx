"use client";

import { useQuery } from "@tanstack/react-query";
import { Checkbox, Empty, Spin } from "antd";

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

export function RoleChecks({ value, onChange, disabled = [] }: { value: string[]; onChange: (v: string[]) => void; disabled?: string[] }) {
  const { data, isLoading } = useAllRoles();
  if (isLoading) return <Spin />;
  if (!data?.length) return <Empty description="没有可选的角色" />;
  const toggle = (id: string, on: boolean) => onChange(on ? [...value, id] : value.filter((x) => x !== id));
  return (
    <div style={{ maxHeight: 360, overflow: "auto" }}>
      {data.map((g) => (
        <div key={g.app.id} style={{ marginBottom: 10 }}>
          <div className="uc-muted" style={{ fontWeight: 650 }}>
            {g.app.icon} {g.app.name}
          </div>
          {g.roles.map((r) => (
            <div key={r.id}>
              <Checkbox
                checked={value.includes(r.id) || disabled.includes(r.id)}
                disabled={disabled.includes(r.id)}
                onChange={(e) => toggle(r.id, e.target.checked)}
              >
                {r.name} <span className="uc-muted">{r.description}</span>
                {disabled.includes(r.id) && <span className="uc-muted">（已授予）</span>}
              </Checkbox>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
