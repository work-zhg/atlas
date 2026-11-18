"use client";

/**
 * 个人中心 › 我的应用：我能访问的应用，点「打开」去它的入口地址；与右上角「应用切换」同一份数据。
 */
import { useQuery } from "@tanstack/react-query";
import { Button, Empty, Spin } from "antd";

import { AppIcon, PageHead } from "@/components/common";
import { Icon } from "@/components/Icon";
import { api } from "@/lib/api";
import type { AppInfo } from "@/lib/types";

export default function MyAppsPage() {
  const apps = useQuery({ queryKey: ["me", "apps"], queryFn: () => api.get<AppInfo[]>("/me/apps") });
  return (
    <>
      <PageHead title="我的应用" sub="你能访问的应用；能访问哪些由管理员设置。" />
      {apps.isLoading ? (
        <Spin />
      ) : (apps.data ?? []).length ? (
        <div className="uc-grid">
          {(apps.data ?? []).map((a) => (
            <div key={a.id} className="uc-acard">
              <div className="uc-row" style={{ gap: 10 }}>
                <AppIcon app={a} size={40} />
                <div className="uc-grow">
                  <b>{a.name}</b>
                  {a.code && <div className="uc-sub uc-mono">{a.code}</div>}
                </div>
              </div>
              <div className="desc">{a.description || "—"}</div>
              <div className="foot">
                <div className="uc-grow" />
                {a.home_url ? (
                  // 入口在网关上的其他前端（或外部地址）：整页跳转
                  <Button size="small" type="primary" href={a.home_url} icon={<Icon name="external" size={14} />} iconPosition="end">
                    打开
                  </Button>
                ) : (
                  <span className="uc-sub">管理员还没登记入口地址</span>
                )}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <Empty description="没有可访问的应用" />
      )}
    </>
  );
}
