"use client";

import { Card, Descriptions } from "antd";

import { PageHead } from "@/components/common";

export default function SettingsPage() {
  return (
    <>
      <PageHead title="集成配置" sub="系统设置 · 外部系统的连接（只读说明，配置在部署环境变量中）" />
      <Card size="small">
        <Descriptions column={1} size="small">
          <Descriptions.Item label="用户中心">人员、角色、菜单、团队数据权限（开放接口 /open/v1）</Descriptions.Item>
          <Descriptions.Item label="Atlas">团队 Agent 的来源（只读引用）</Descriptions.Item>
          <Descriptions.Item label="Gitee">产物仓库：Agent 在会话 Pod 里用 git 提交；成员绑定 Gitee 账号后按团队身份获得仓库权限</Descriptions.Item>
        </Descriptions>
      </Card>
    </>
  );
}
