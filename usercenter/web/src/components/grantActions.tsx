"use client";

import { useQueryClient } from "@tanstack/react-query";
import { App, Modal } from "antd";
import { useState } from "react";

import { api, errorText } from "@/lib/api";
import type { GrantRow } from "@/lib/types";

import { SubjectPicker, toSubjects, type SubjectValue } from "./common";

export function useGrantMutations() {
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["grants"] });
    qc.invalidateQueries({ queryKey: ["subject-grants"] });
    qc.invalidateQueries({ queryKey: ["positions"] });
    qc.invalidateQueries({ queryKey: ["role"] });
  };
  return {
    revoke(g: GrantRow) {
      modal.confirm({
        title: "撤销授权",
        content: `撤销「${g.target.name}」对「${g.subject.name}」的授权？覆盖 ${g.covered} 人；没有其他来源的人将失去对应权限。`,
        okText: "撤销",
        okButtonProps: { danger: true },
        onOk: async () => {
          try {
            await api.del(`/grants/${g.id}`);
            refresh();
            message.success("已撤销");
          } catch (e) {
            message.error(errorText(e));
          }
        },
      });
    },
    async toggleSub(g: GrantRow) {
      try {
        await api.patch(`/grants/${g.id}`, { include_sub: !g.include_sub });
        refresh();
        message.success(g.include_sub ? "已改为仅本部门" : "已改为包含下级部门");
      } catch (e) {
        message.error(errorText(e));
      }
    },
    refresh,
  };
}


/** 从岗位 / 角色出发：一次授予多个部门和用户。 */
export function GrantToModal({ kind, target, onClose }: { kind: "role" | "position"; target: { id: string; name: string }; onClose: () => void }) {
  const { message } = App.useApp();
  const actions = useGrantMutations();
  const [value, setValue] = useState<SubjectValue>({ depts: [], users: [], includeSub: true });
  async function ok() {
    const subjects = toSubjects(value);
    if (!subjects.length) return message.warning("请选择组织或用户");
    try {
      const r = await api.post<{ added: number; updated: number }>("/grants", { kind, target_ids: [target.id], subjects, include_sub: value.includeSub });
      actions.refresh();
      message.success(`新增 ${r.added} 条授权${r.updated ? `，更新 ${r.updated} 条范围` : ""}`);
      onClose();
    } catch (e) {
      message.error(errorText(e));
    }
  }
  return (
    <Modal open title={`授权 · ${kind === "role" ? "角色" : "岗位"}「${target.name}」`} onCancel={onClose} onOk={ok} okText="授权" width={560}>
      <SubjectPicker value={value} onChange={setValue} />
      <div className="uc-muted" style={{ marginTop: 8 }}>
        已授权的对象会跳过；部门已授权时按本次的「包含下级部门」更新范围。
      </div>
    </Modal>
  );
}

