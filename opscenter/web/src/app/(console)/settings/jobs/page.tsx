"use client";

/**
 * 系统任务：各系统后台定期执行的任务（入库巡检、兜底、清理……）在回应请求的那个实例上的运行情况。
 * 面板是共享组件 @atlas/jobs-ui，接口经系统代理转到各系统的 /ops/v1/jobs。只能查看。
 */
import { JobsPanel } from "@atlas/jobs-ui";
import { Tabs } from "antd";

import { PageHead } from "@/components/common";
import { sys, writeHeaders } from "@/lib/api";
import { useSystems, withCapability } from "@/lib/session";

export default function JobsPage() {
  const { data: systems } = useSystems();
  return (
    <>
      <PageHead title="系统任务" sub="各系统的后台任务（系统机制，只能查看）。多实例部署时看到的是回应请求的那个实例" />
      <Tabs
        items={withCapability(systems, "jobs").map((s) => ({
          key: s.code,
          label: s.name,
          children: <JobsPanel endpoint={`${sys(s.code).endpoint}/jobs`} headers={writeHeaders} />,
        }))}
      />
    </>
  );
}
