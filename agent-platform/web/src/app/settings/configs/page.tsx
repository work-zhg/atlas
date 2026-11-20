"use client";

/**
 * 租户设置项（应用配置库 scope=TENANT，后端 api/v1/tenant_settings.py）：每人同时运行的任务、
 * 沙箱、工作空间存储上限，默认智能体…… 没改过的用平台的系统值。看要登录，改要 settings:manage。
 * 面板是共享组件 @atlas/appconfig-ui。
 *
 * ★ 镜像白名单、上传上限这类系统参数在运营中心改，这里看不到。
 */
import { AppConfigPanel } from "@atlas/appconfig-ui";

import { SETTINGS_MANAGE } from "@/api/tenant-settings";
import pageStyles from "@/features/agents/agents.module.css";
import { API_BASE, authHeaders } from "@/lib/http";
import { can, useMe } from "@/lib/session";

const ENDPOINT = `${API_BASE}/v1/tenant-settings/configs`;

export default function Page() {
  const { data: me } = useMe();
  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <h1 className={pageStyles.title}>配置项</h1>
        </div>
      </header>
      <div className={pageStyles.body}>
        <AppConfigPanel endpoint={ENDPOINT} headers={authHeaders} readOnly={!can(me, SETTINGS_MANAGE)} />
      </div>
    </div>
  );
}
