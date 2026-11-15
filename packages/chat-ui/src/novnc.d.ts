// noVNC（@novnc/novnc）没有类型声明：只用到 RFB 的几个属性和事件，按需声明
declare module "@novnc/novnc" {
  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, urlOrChannel: string | WebSocket, options?: Record<string, unknown>);
    viewOnly: boolean;
    scaleViewport: boolean;
    resizeSession: boolean;
    background: string;
    focusOnClick: boolean;
    disconnect(): void;
    focus(): void;
    sendKey(keysym: number, code: string | null, down?: boolean): void;
  }
}
