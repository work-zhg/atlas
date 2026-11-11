"use client";

/**
 * 应用配置（原型 c4-model/infra-platform/ui-appconfig.html）：Atlas 运行期可改的业务参数 ——
 * 每人沙箱配额、镜像白名单……（后端 runtime_config.py 声明）。看和改都要 config:manage
 * （api/v1/configs.py），所以这里不分只读。面板是共享组件 @atlas/appconfig-ui。
 */
import { AppConfigPanel } from "@atlas/appconfig-ui";

import pageStyles from "@/features/agents/agents.module.css";
import { API_BASE, authHeaders } from "@/lib/http";

const ENDPOINT = `${API_BASE}/v1/configs`;

export default function Page() {
  return (
    <div className={pageStyles.page}>
      <header className={pageStyles.head}>
        <div className={pageStyles.titleRow}>
          <h1 className={pageStyles.title}>配置项</h1>
        </div>
      </header>
      <div className={pageStyles.body}>
        <AppConfigPanel endpoint={ENDPOINT} headers={authHeaders} />
      </div>
    </div>
  );
}
