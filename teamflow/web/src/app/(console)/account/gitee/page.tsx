"use client";

/** Gitee OAuth 回调：把 code / state 交给后端完成绑定，然后回到「Gitee 账号」页。 */
import { useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Spin } from "antd";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { api, errorText } from "@/lib/api";
import type { GitAccountView } from "@/lib/types";

function Callback() {
  const params = useSearchParams();
  const router = useRouter();
  const qc = useQueryClient();
  const [error, setError] = useState<string>();
  const done = useRef(false);

  useEffect(() => {
    if (done.current) return; // 授权码只能用一次：开发模式下 effect 会跑两遍
    done.current = true;
    const code = params.get("code");
    const state = params.get("state");
    if (!code || !state) {
      setError(params.get("error_description") ?? "Gitee 没有返回授权码（可能取消了授权）");
      return;
    }
    api
      .post<GitAccountView>("/me/git-account/oauth/callback", { code, state })
      .then((v) => {
        qc.setQueryData(["git-account"], v);
        router.replace("/account");
      })
      .catch((e) => setError(errorText(e)));
  }, [params, qc, router]);

  if (error)
    return (
      <Alert
        type="error"
        showIcon
        message="绑定失败"
        description={error}
        action={
          <Link href="/account">
            <Button size="small">返回</Button>
          </Link>
        }
        style={{ maxWidth: 560 }}
      />
    );
  return <Spin tip="正在完成绑定…" style={{ display: "block", marginTop: 120 }} />;
}

export default function GiteeCallbackPage() {
  return (
    <Suspense fallback={<Spin style={{ display: "block", marginTop: 120 }} />}>
      <Callback />
    </Suspense>
  );
}
