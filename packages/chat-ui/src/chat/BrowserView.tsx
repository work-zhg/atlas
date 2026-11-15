/// <reference path="../novnc.d.ts" />
"use client";

import { Button } from "antd";
import { type RefObject, useCallback, useEffect, useRef, useState } from "react";

import { useBrowserClipboard, useBrowserViewUrl } from "../api/browser";
import styles from "./chat.module.css";

interface Props {
  threadId: string;
  /** 接管时可以操作；否则只看，免得打乱智能体 */
  interactive: boolean;
  /** 画面（含上面一条工具栏）的高度：数字是像素；独立页面给 "100%" 占满 */
  height?: number | string;
}

type ViewState =
  | { phase: "connecting" }
  | { phase: "connected" }
  | { phase: "closed"; message: string };

interface Remote {
  viewOnly: boolean;
  disconnect(): void;
  focus(): void;
  sendKey(keysym: number, code: string | null, down?: boolean): void;
}

/** X11 keysym：沙箱里是 Linux，复制粘贴的快捷键是 Ctrl */
const XK_CONTROL_L = 0xffe3;
const XK_V = 0x0076;
/** 按下复制后等沙箱里的浏览器把选中的内容放进剪贴板，再取回来 */
const COPY_SETTLE_MS = 300;
/** 按下 Ctrl / ⌘ + V 后等本机的 paste 事件多久（它紧跟着按键，几毫秒就到） */
const PASTE_FALLBACK_MS = 150;

/**
 * 沙箱里浏览器的实时画面（c4-model/agent/browser/code-browser-view.html）。
 *
 * ★ 看画面的客户端（noVNC）随控制台发布，沙箱只出一条画面数据流，经 Agent 服务同源转发：
 *   不把沙箱里的网页嵌进控制台，HTTPS 页面也连不了集群里的明文地址。
 * ★ WebSocket 自己建、再交给 noVNC：关闭时服务端带的原因（不是你的会话、浏览器没在运行……）
 *   要读出来给人看，noVNC 的断开事件里没有它。
 * ★ noVNC 是 ESM、用了顶层 await：只在浏览器里按需加载。
 * ★ 工具栏在画面上方、不盖在画面上（盖住会挡到沙箱里浏览器右上角的菜单）；全屏的是整个外框。
 */
export function BrowserView({ threadId, interactive, height = 480 }: Props) {
  const url = useBrowserViewUrl(threadId);
  const frame = useRef<HTMLDivElement>(null);
  const screen = useRef<HTMLDivElement>(null);
  const rfbRef = useRef<Remote | null>(null);
  const [state, setState] = useState<ViewState>({ phase: "connecting" });
  const [attempt, setAttempt] = useState(0);
  const [full, setFull] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const notify = useCallback((text: string) => setNotice(text), []);
  useEffect(() => {
    if (!notice) return undefined;
    const t = window.setTimeout(() => setNotice(null), 3000);
    return () => window.clearTimeout(t);
  }, [notice]);

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

  useRemoteKeys(frame, rfbRef, interactive && state.phase === "connected", threadId, notify);

  useEffect(() => {
    const sync = () => setFull(document.fullscreenElement === frame.current);
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);
  const toggleFull = () => {
    if (document.fullscreenElement) {
      void document.exitFullscreen();
      return;
    }
    frame.current
      ?.requestFullscreen()
      .then(() => rfbRef.current?.focus())
      .catch(() => notify("这个浏览器不让全屏"));
  };

  return (
    <div ref={frame} className={styles.browserView} style={{ height }}>
      <div className={styles.browserBar}>
        <span className={styles.browserBadge}>{interactive ? "可以操作" : "只看"}</span>
        <span className={styles.browserHint}>
          {notice ?? (interactive ? "复制粘贴用 Ctrl / ⌘ + C、V" : "智能体在这里操作网页")}
        </span>
        <Button size="small" type="text" onClick={toggleFull}>
          {full ? "退出全屏" : "全屏"}
        </Button>
      </div>
      <div className={styles.browserStage}>
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
    </div>
  );
}

/**
 * 可以操作时的键盘：复制粘贴经平台进出沙箱的剪贴板；⌘ 当 Ctrl 发给沙箱里的浏览器（那边是 Linux）。
 *
 * ★ 监听挂在外框上、走捕获阶段：比 noVNC（挂在画面的 canvas 上）先拿到按键。
 * 粘贴：Ctrl / ⌘ + V 不交给 noVNC，本机浏览器照常触发 paste 事件，从事件里拿本机剪贴板的文字
 *   （不用申请读剪贴板的权限）→ 写进沙箱的剪贴板 → 再给沙箱里的浏览器按 Ctrl+V。
 * 复制：C、X 照常发过去（沙箱里的浏览器自己复制），稍等把沙箱剪贴板的文字取回、写进本机剪贴板。
 */
function useRemoteKeys(
  frame: RefObject<HTMLDivElement | null>,
  rfb: RefObject<Remote | null>,
  enabled: boolean,
  threadId: string,
  notify: (text: string) => void,
) {
  const clipboard = useBrowserClipboard(threadId);
  useEffect(() => {
    const el = frame.current;
    if (!enabled || !el) return undefined;
    // 沙箱里的 Ctrl 此刻是不是按着：本机的 Ctrl 经 noVNC 发过去，⌘ 经这里换成 Ctrl 发过去
    let ctrlDown = false;
    let copyTimer: number | undefined;
    let pasteFallback: number | undefined;

    const copyOut = async () => {
      let text: string;
      try {
        text = await clipboard.read();
      } catch (err) {
        notify(`没复制出来：${messageOf(err)}`);
        return;
      }
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        notify("已复制到你的剪贴板");
      } catch {
        notify("浏览器不让写剪贴板：请在地址栏旁允许剪贴板权限");
      }
    };

    const pressPaste = () => {
      const remote = rfb.current;
      if (!remote) return;
      // 写剪贴板要来回一趟：这期间人可能已经松开了 Ctrl / ⌘
      const held = ctrlDown;
      if (!held) remote.sendKey(XK_CONTROL_L, "ControlLeft", true);
      remote.sendKey(XK_V, "KeyV");
      if (!held) remote.sendKey(XK_CONTROL_L, "ControlLeft", false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      const remote = rfb.current;
      if (!remote) return;
      if (isMeta(e.code)) {
        e.stopPropagation(); // 只是不让 noVNC 当 Super 发过去；默认动作照旧（⌘V 还要触发 paste）
        if (!e.repeat) remote.sendKey(XK_CONTROL_L, "ControlLeft", true);
        ctrlDown = true;
        return;
      }
      if (isControl(e.code)) {
        ctrlDown = true;
        return;
      }
      const shortcut = (e.ctrlKey || e.metaKey) && !e.altKey;
      if (shortcut && e.code === "KeyV") {
        e.stopPropagation(); // 不交给 noVNC，也不阻止默认动作：要的就是随后的 paste 事件
        // 本机不把这个组合当粘贴（如 Mac 上按的是 Ctrl+V）就等不到 paste：照常按给沙箱
        window.clearTimeout(pasteFallback);
        pasteFallback = window.setTimeout(pressPaste, PASTE_FALLBACK_MS);
        return;
      }
      if (shortcut && (e.code === "KeyC" || e.code === "KeyX")) {
        window.clearTimeout(copyTimer);
        copyTimer = window.setTimeout(() => void copyOut(), COPY_SETTLE_MS);
      }
    };
    const onKeyUp = (e: KeyboardEvent) => {
      if (isMeta(e.code)) {
        e.stopPropagation();
        rfb.current?.sendKey(XK_CONTROL_L, "ControlLeft", false);
        ctrlDown = false;
      } else if (isControl(e.code)) {
        ctrlDown = false;
      }
    };
    const onPaste = (e: ClipboardEvent) => {
      window.clearTimeout(pasteFallback);
      e.preventDefault();
      const text = e.clipboardData?.getData("text/plain") ?? "";
      // 本机剪贴板里没有文字（如复制的是图片）：照常按 Ctrl+V，粘沙箱里自己复制的
      if (!text) {
        pressPaste();
        return;
      }
      clipboard.write(text).then(pressPaste, (err: unknown) => notify(`没粘贴进去：${messageOf(err)}`));
    };
    // 切走窗口时收不到 ⌘ 的抬起：别让沙箱里的 Ctrl 一直按着
    const onBlur = () => {
      if (ctrlDown) rfb.current?.sendKey(XK_CONTROL_L, "ControlLeft", false);
      ctrlDown = false;
    };

    el.addEventListener("keydown", onKeyDown, true);
    el.addEventListener("keyup", onKeyUp, true);
    el.addEventListener("paste", onPaste);
    window.addEventListener("blur", onBlur);
    return () => {
      window.clearTimeout(copyTimer);
      window.clearTimeout(pasteFallback);
      el.removeEventListener("keydown", onKeyDown, true);
      el.removeEventListener("keyup", onKeyUp, true);
      el.removeEventListener("paste", onPaste);
      window.removeEventListener("blur", onBlur);
    };
  }, [enabled, frame, rfb, clipboard, notify]);
}

const isMeta = (code: string) => code === "MetaLeft" || code === "MetaRight";
const isControl = (code: string) => code === "ControlLeft" || code === "ControlRight";

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function closeText(e: CloseEvent): string {
  if (e.reason) return e.reason;
  if (e.code === 1006) return "画面连不上（浏览器可能没在运行）";
  return "画面断开了";
}
