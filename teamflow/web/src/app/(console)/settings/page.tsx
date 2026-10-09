"use client";

import { Card, Descriptions } from "antd";

import { PageHead } from "@/components/common";

export default function SettingsPage() {
  return (
    <>
      <PageHead title="系统设置" sub="Atlas / Git 连接、通知（随流程模块一起上线）" />
      <Card size="small">
        <Descriptions column={1} size="small">
          <Descriptions.Item label="用户中心">人员、角色、菜单、团队数据权限（开放接口 /open/v1）</Descriptions.Item>
          <Descriptions.Item label="Atlas">团队 Agent 的来源（只读引用）</Descriptions.Item>
          <Descriptions.Item label="Git">产物存储（下一期）</Descriptions.Item>
        </Descriptions>
      </Card>
    </>
  );
}
