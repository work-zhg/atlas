"use client";

/**
 * 系统参数：各系统的应用配置库里的系统参数（不含任何租户内容；租户设置由租户管理员在各应用里改）。
 * 面板是共享组件 @atlas/appconfig-ui，接口经系统代理转到各系统的 /ops/v1/configs。
 */
import { AppConfigPanel } from "@atlas/appconfig-ui";
import { Tabs } from "antd";

import { PageHead } from "@/components/common";
import { refreshSession, sys, writeHeaders } from "@/lib/api";
import { useAccess, useSystems, withCapability } from "@/lib/session";

export default function ParamsPage() {
  const acc = useAccess();
  const { data: systems } = useSystems();
  return (
    <>
      <PageHead title="系统参数" sub="各系统的系统参数：可用镜像、上传上限、默认技能……改了下一次用到时生效，每次修改留一个版本，可以回滚" />
      <Tabs
        items={withCapability(systems, "settings")
          .filter((s) => acc.read(s.module))
          .map((s) => ({
          key: s.code,
          label: s.name,
          children: (
            <AppConfigPanel
              endpoint={`${sys(s.code).endpoint}/configs`}
              headers={writeHeaders}
              refresh={refreshSession}
              readOnly={!acc.write(s.module)}
            />
          ),
        }))}
      />
    </>
  );
}
