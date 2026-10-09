"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { App, Input } from "antd";

import { api, errorText } from "@/lib/api";
import type { User } from "@/lib/types";

import { showOnce } from "./common";

/** 停用 / 启用 / 解锁 / 重置密码：列表与详情共用。 */
export function useUserActions() {
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const done = (text: string) => {
    qc.invalidateQueries({ queryKey: ["users"] });
    qc.invalidateQueries({ queryKey: ["user"] });
    message.success(text);
  };
  const run = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) => api.post(path, body),
    onError: (e) => message.error(errorText(e)),
  });
  return {
    pending: run.isPending,
    disable(u: User) {
      let reason = "";
      modal.confirm({
        title: `停用用户 · ${u.name}`,
        content: (
          <div>
            <p>停用后无法登录用户中心，接入应用会立即查到「已停用」；数据与授权保留，可随时启用。</p>
            <Input placeholder="停用原因（可选，如：离职）" onChange={(e) => (reason = e.target.value)} />
          </div>
        ),
        okText: "停用",
        okButtonProps: { danger: true },
        onOk: () => run.mutateAsync({ path: `/users/${u.id}/disable`, body: { reason } }).then(() => done(`已停用 ${u.name}`)),
      });
    },
    enable: (u: User) => run.mutateAsync({ path: `/users/${u.id}/enable` }).then(() => done(`已启用 ${u.name}`)),
    unlock: (u: User) => run.mutateAsync({ path: `/users/${u.id}/unlock` }).then(() => done(`已解锁 ${u.name}`)),
    reset(u: User) {
      modal.confirm({
        title: `重置密码 · ${u.name}`,
        content: "将生成新的临时密码，原密码立即失效、用户中心的登录会话被注销；用户下次登录必须修改密码。",
        okText: "重置",
        onOk: async () => {
          const r = await run.mutateAsync({ path: `/users/${u.id}/reset-password` });
          qc.invalidateQueries({ queryKey: ["user"] });
          showOnce(modal, "密码已重置", [["用户", `${u.name}（${u.account}）`], ["临时密码", (r as { temp_password: string }).temp_password]], "临时密码只显示这一次，请通过安全渠道告知用户。");
        },
      });
    },
  };
}

