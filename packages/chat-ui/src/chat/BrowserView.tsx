/// <reference path="../novnc.d.ts" />
"use client";

import { Button } from "antd";
import { useEffect, useRef, useState } from "react";

import { useBrowserViewUrl } from "../api/browser";
import styles from "./chat.module.css";

interface Props {
  threadId: string;
  /** 接管时可以操作；否则只看，免得打乱智能体 */
  interactive: boolean;
  height?: number;
}

type ViewState =
  | { phase: "connecting" }
  | { phase: "connected" }
  | { phase: "closed"; message: string };

/**
 * 沙箱里浏览器的实时画面（c4-model/agent/browser/code-browser-view.html）。
 *
 * ★ 看画面的客户端（noVNC）随控制台发布，沙箱只出一条画面数据流，经 Agent 服务同源转发：
 *   不把沙箱里的网页嵌进控制台，HTTPS 页面也连不了集群里的明文地址。
 * ★ WebSocket 自己建、再交给 noVNC：关闭时服务端带的原因（不是你的会话、浏览器没在运行……）
 *   要读出来给人看，noVNC 的断开事件里没有它。
 * ★ noVNC 是 ESM、用了顶层 await：只在浏览器里按需加载。
 */
export function BrowserView({ threadId, interactive, height = 480 }: Props) {
  const url = useBrowserViewUrl(threadId);
  const screen = useRef<HTMLDivElement>(null);
  const rfbRef = useRef<{ viewOnly: boolean; disconnect: () => void; focus: () => void } | null>(null);
  const [state, setState] = useState<ViewState>({ phase: "connecting" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!url || !screen.current) return undefined;
    let cancelled = false;
    setState({ phase: "connecting" });
    const ws = new WebSocket(url, ["binary"]);
    ws.binaryType = "arraybuffer";
    ws.addEventListener("close", (e) => {
      if (!cancelled) setState({ phase: "closed", message: closeText(e) });
    });
    ws.addEventListener("open", () => {
      void import("@novnc/novnc").then(({ default: RFB }) => {
        if (cancelled || !screen.current || ws.readyState !== WebSocket.OPEN) return;
        const rfb = new RFB(screen.current, ws);
        rfb.scaleViewport = true;
        rfb.resizeSession = false;
        rfb.background = "#f4f5f7";
        rfb.viewOnly = !interactive;
        rfb.addEventListener("connect", () => {
          if (!cancelled) setState({ phase: "connected" });
        });
        rfbRef.current = rfb;
      });
    });
    return () => {
      cancelled = true;
      try {
        rfbRef.current?.disconnect();
      } catch {
        // 已经断了
      }
      rfbRef.current = null;
      if (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN) ws.close();
    };
    // interactive 不重连：下面单独切换
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, attempt]);

  useEffect(() => {
    if (rfbRef.current) rfbRef.current.viewOnly = !interactive;
  }, [interactive, state.phase]);

  return (
    <div className={styles.browserView} style={{ height }}>
      <span className={styles.browserBadge}>{interactive ? "可以操作" : "只看"}</span>
      <div ref={screen} className={styles.browserScreen} />
      {state.phase !== "connected" && (
        <div className={styles.browserOverlay}>
          {state.phase === "connecting" ? (
            <span>正在连接浏览器画面…</span>
          ) : (
            <>
              <span>{state.message}</span>
              <Button size="small" onClick={() => setAttempt((n) => n + 1)}>
                重新连接
              </Button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function closeText(e: CloseEvent): string {
  if (e.reason) return e.reason;
  if (e.code === 1006) return "画面连不上（浏览器可能没在运行）";
  return "画面断开了";
}
