"use client";

/** 个人设置：把自己的企业微信 / 钉钉 / 邮箱绑上，评审提醒除了站内通知也推到那里（团队打开了渠道才推） */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App, Button, Input } from "antd";
import { useEffect, useState } from "react";

import { Page, PageHead } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { useMe } from "@/lib/queries";

const CHANNELS = [
  { key: "wecom", label: "企业微信", hint: "企业微信里的账号（UserID）" },
  { key: "dingtalk", label: "钉钉", hint: "钉钉里的账号（userid）" },
  { key: "email", label: "邮件", hint: "收每日汇总的邮箱" },
];

export default function MePage() {
  const { data: me } = useMe();
  const { message } = App.useApp();
  const qc = useQueryClient();
  const bound = useQuery({ queryKey: ["me", "channels"], queryFn: () => api.get<Record<string, string>>("/me/channels") });
  const [draft, setDraft] = useState<Record<string, string>>({});
  useEffect(() => setDraft(bound.data ?? {}), [bound.data]);

  const save = async (ch: string) => {
    try {
      const v = (draft[ch] ?? "").trim();
      if (v) await api.put(`/me/channels/${ch}`, { account: v });
      else await api.del(`/me/channels/${ch}`);
      await qc.invalidateQueries({ queryKey: ["me", "channels"] });
      message.success("已保存");
    } catch (e) {
      message.error(errorText(e));
    }
  };

  return (
    <Page narrow>
      <PageHead title="个人设置" sub={me?.name} />
      <div className="card pad">
        <h2 style={{ marginBottom: 4 }}>通知绑定</h2>
        <div className="xs muted" style={{ marginBottom: 10 }}>
          要你评审的事、轮到你的步骤、被退回，除了站内通知也推到这里（团队打开了对应渠道才会推）
        </div>
        {CHANNELS.map((c) => (
          <div key={c.key} className="row" style={{ padding: "8px 0", borderBottom: "1px solid var(--tf-line-soft)" }}>
            <b className="sm" style={{ width: 80 }}>
              {c.label}
            </b>
            <Input
              style={{ maxWidth: 320 }}
              value={draft[c.key] ?? ""}
              onChange={(e) => setDraft((d) => ({ ...d, [c.key]: e.target.value }))}
              placeholder={c.hint}
              maxLength={128}
            />
            <Button size="small" onClick={() => void save(c.key)} disabled={(draft[c.key] ?? "") === (bound.data?.[c.key] ?? "")}>
              保存
            </Button>
          </div>
        ))}
      </div>
    </Page>
  );
}
