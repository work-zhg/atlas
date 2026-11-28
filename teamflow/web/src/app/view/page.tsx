"use client";

/**
 * 流程文件的预览页：从各页点文件在新标签页打开，整页都给文件（不带应用外壳的菜单）。
 * 地址：/view?flow=流程&path=文件路径
 */
import { Button } from "antd";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect } from "react";

import { downloadUrl, FileViewer } from "@/components/FilePreview";
import { Icon, Loading } from "@/components/ui";
import { base } from "@/lib/format";
import { useProcess } from "@/lib/queries";

export default function ViewPage() {
  return (
    <Suspense fallback={<Loading />}>
      <View />
    </Suspense>
  );
}

function View() {
  const q = useSearchParams();
  const flow = q.get("flow") ?? "";
  const path = q.get("path") ?? "";
  const proc = useProcess(flow || undefined);
  const name = base(path);
  useEffect(() => {
    document.title = `${name} · ${proc.data?.title ?? "AI TeamFlow"}`;
  }, [name, proc.data?.title]);
  if (!flow || !path) return <div className="empty">地址不完整：缺流程或文件</div>;
  const dir = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  return (
    <div className="viewpage">
      <header className="viewhead">
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 6 }}>
            <Icon name="file" size={16} />
            <b className="ellipsis" style={{ fontSize: 15 }}>
              {name}
            </b>
          </div>
          <div className="xs muted ellipsis" style={{ marginTop: 2 }}>
            {proc.data ? (
              <Link href={`/flows/${flow}/space${dir ? `?dir=${encodeURIComponent(dir)}` : ""}`}>
                {proc.data.code} {proc.data.title}
              </Link>
            ) : (
              "…"
            )}
            {dir && <span> · {dir}/</span>}
            <span> · 文件的当前内容</span>
          </div>
        </div>
        <Button icon={<Icon name="download" size={14} />} href={downloadUrl(flow, path)}>
          下载
        </Button>
      </header>
      <main className="viewbody">
        <FileViewer processId={flow} path={path} />
      </main>
    </div>
  );
}
