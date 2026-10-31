"use client";

/**
 * MCP 服务的表单零件：连接、上游凭据、gRPC 描述文件、发现结果的挑选。登记和修改共用。
 * 都是受控组件：值的形状就是网关接口要的（connection、CredentialIn），校验以网关为准。
 */
import { Alert, Checkbox, Input, Radio, Select, Table, Tag, Upload } from "antd";

import {
  GW_CREDENTIAL_LABEL,
  GW_CREDENTIAL_TYPES,
  GW_EFFECT,
  GW_KIND,
  type GwCredential,
  type GwCredentialIn,
  type GwDiscovered,
  type GwDiscoveredTool,
  type GwEffect,
  type GwKind,
  type GwVisibility,
} from "@/api/mcp-gateway";
import styles from "./catalog.module.css";

export const STREAM_LABEL: Record<string, string> = {
  unary: "一元",
  server: "服务端流",
  client: "客户端流",
  bidi: "双向流",
};

const TLS_LABEL: Record<string, string> = { plaintext: "明文（集群内）", tls: "TLS", mtls: "mTLS" };

export const fmtTime = (at: string | null | undefined) => (at ? at.slice(0, 16).replace("T", " ") : "—");

export function KindTag({ kind }: { kind: GwKind }) {
  return <Tag color={kind === "mcp" ? "purple" : kind === "rest" ? "geekblue" : "cyan"}>{GW_KIND[kind].label}</Tag>;
}

export function VisibilityTag({ visibility }: { visibility: GwVisibility }) {
  return visibility === "public" ? <Tag color="blue">公开</Tag> : <Tag>私有</Tag>;
}

export function EffectTag({ effect }: { effect: GwEffect }) {
  const e = GW_EFFECT[effect];
  return <Tag color={e.color}>{e.label}</Tag>;
}

function Field({ label, hint, children }: { label: string; hint?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ fontSize: 13, marginBottom: 4 }}>{label}</div>
      {children}
      {hint && (
        <div className={styles.muted} style={{ fontSize: 12, marginTop: 4 }}>
          {hint}
        </div>
      )}
    </div>
  );
}

export { Field as FormField };

// ═══════════════════════════════════════════════ 连接

/** REST 的接口定义来源只在表单里用：manual = 不填 spec_url */
export type Connection = Record<string, unknown> & { source?: "openapi" | "manual" };

export function emptyConnection(kind: GwKind): Connection {
  if (kind === "mcp") return { url: "", transport: "streamable" };
  if (kind === "rest") return { base_url: "", spec_url: "", source: "openapi" };
  return { address: "", tls: "plaintext", discovery: "reflection" };
}

/** 存库的连接 → 表单值 */
export function connectionOf(kind: GwKind, saved: Record<string, unknown>): Connection {
  if (kind === "rest") return { ...saved, source: saved.spec_url ? "openapi" : "manual" };
  return { ...saved };
}

/** 表单值 → 接口要的连接 */
export function connectionBody(kind: GwKind, c: Connection): Record<string, unknown> {
  const { source, ...rest } = c;
  if (kind === "rest" && source === "manual") return { ...rest, spec_url: "" };
  return rest;
}

const str = (v: unknown) => (typeof v === "string" ? v : "");

export function ConnectionFields({
  kind,
  value,
  onChange,
  descriptor,
}: {
  kind: GwKind;
  value: Connection;
  onChange: (next: Connection) => void;
  /** gRPC 上传描述文件（登记、重新发现时才有） */
  descriptor?: { value: string | null; onChange: (b64: string | null) => void };
}) {
  const set = (patch: Connection) => onChange({ ...value, ...patch });
  if (kind === "mcp") {
    return (
      <>
        <Field label="地址" hint="只接远程 MCP Server；stdio 的 MCP Server 先单独部署成 HTTP 服务（放在 K8s Pod 里）再登记">
          <div style={{ display: "flex", gap: 8 }}>
            <Input
              value={str(value.url)}
              onChange={(e) => set({ url: e.target.value.trim() })}
              placeholder="http://xxx-mcp.ns:8000/mcp"
            />
            <Select
              style={{ width: 200 }}
              value={str(value.transport) || "streamable"}
              onChange={(v) => set({ transport: v })}
              options={[
                { value: "streamable", label: "Streamable HTTP" },
                { value: "sse", label: "SSE（旧版协议）" },
              ]}
            />
          </div>
        </Field>
      </>
    );
  }
  if (kind === "rest") {
    const source = value.source ?? "openapi";
    return (
      <>
        <Field label="Base URL">
          <Input
            value={str(value.base_url)}
            onChange={(e) => set({ base_url: e.target.value.trim() })}
            placeholder="http://xxx-api.ns:8080/api"
          />
        </Field>
        <Field
          label="接口定义"
          hint={source === "openapi" ? "支持 OpenAPI 3.x、Swagger 2.0（JSON 或 YAML）；一个操作转成一个工具" : "登记后在「工具」里逐个添加接口"}
        >
          <Radio.Group value={source} onChange={(e) => set({ source: e.target.value })}>
            <Radio value="openapi">导入 OpenAPI</Radio>
            <Radio value="manual">手工添加</Radio>
          </Radio.Group>
          {source === "openapi" && (
            <Input
              style={{ marginTop: 6 }}
              value={str(value.spec_url)}
              onChange={(e) => set({ spec_url: e.target.value.trim() })}
              placeholder="http://xxx-api.ns:8080/openapi.json"
            />
          )}
        </Field>
      </>
    );
  }
  const discovery = str(value.discovery) || "reflection";
  return (
    <>
      <Field label="地址">
        <div style={{ display: "flex", gap: 8 }}>
          <Input
            value={str(value.address)}
            onChange={(e) => set({ address: e.target.value.trim() })}
            placeholder="xxx-svc.ns:9090"
          />
          <Select
            style={{ width: 200 }}
            value={str(value.tls) || "plaintext"}
            onChange={(v) => set({ tls: v })}
            options={Object.entries(TLS_LABEL).map(([v, label]) => ({ value: v, label }))}
          />
        </div>
      </Field>
      <Field label="方法发现">
        <Radio.Group value={discovery} onChange={(e) => set({ discovery: e.target.value })}>
          <Radio value="reflection">服务端反射</Radio>
          <Radio value="descriptor">上传描述文件</Radio>
        </Radio.Group>
      </Field>
      {discovery === "descriptor" && descriptor && <DescriptorUpload {...descriptor} />}
    </>
  );
}

/** gRPC 描述文件：FileDescriptorSet（protoc --include_imports --descriptor_set_out=x.pb） */
export function DescriptorUpload({ value, onChange }: { value: string | null; onChange: (b64: string | null) => void }) {
  return (
    <Field
      label="描述文件"
      hint={
        <>
          只接受 FileDescriptorSet：<code>protoc --include_imports --descriptor_set_out=api.pb *.proto</code>
          （不支持直接上传 .proto 文本）
        </>
      }
    >
      <Upload
        accept=".pb,.desc,.protoset,.bin"
        maxCount={1}
        beforeUpload={(file) => {
          void file.arrayBuffer().then((buf) => {
            let bin = "";
            for (const b of new Uint8Array(buf)) bin += String.fromCharCode(b);
            onChange(btoa(bin));
          });
          return false;
        }}
        onRemove={() => onChange(null)}
      >
        <a>{value ? "换一个文件" : "选择文件"}</a>
      </Upload>
    </Field>
  );
}

export function connectionRows(kind: GwKind, c: Record<string, unknown>): [string, React.ReactNode][] {
  if (kind === "mcp")
    return [
      ["地址", <code key="u">{str(c.url)}</code>],
      ["传输", c.transport === "sse" ? "SSE（旧版协议）" : "Streamable HTTP"],
      ["工具定义", "定时从上游拉取，拉到就生效"],
    ];
  if (kind === "rest")
    return [
      ["Base URL", <code key="b">{str(c.base_url)}</code>],
      ["接口定义", c.spec_url ? <code key="s">OpenAPI · {str(c.spec_url)}</code> : "手工定义"],
      ["请求与响应", "JSON；非 2xx 作为工具错误返回（isError），超过结果上限截断"],
    ];
  const services = Array.isArray(c.services) ? (c.services as string[]) : [];
  return [
    ["地址", <code key="a">{str(c.address)}</code>],
    ["传输安全", TLS_LABEL[str(c.tls)] ?? str(c.tls)],
    ["方法发现", c.discovery === "descriptor" ? "描述文件" : "服务端反射"],
    ["服务", services.length ? <code key="sv">{services.join("、")}</code> : "—"],
    ["流式", "支持一元和服务端流（消息合并成数组返回）；不支持客户端流、双向流"],
  ];
}

// ═══════════════════════════════════════════════ 上游凭据

export function credentialOf(saved: GwCredential | undefined): GwCredentialIn {
  const { hint: _hint, ...rest } = saved ?? { type: "none" };
  return { ...rest, secret: "" };
}

export function credentialLabel(c: GwCredential): string {
  const label = GW_CREDENTIAL_LABEL[c.type] ?? c.type;
  if (c.type === "none") return label;
  const where = c.type === "apikey" ? `（${c.where === "query" ? "查询参数" : "请求头"} ${c.key ?? ""}）` : "";
  const named = c.type === "header" || c.type === "metadata" ? `（${c.key ?? ""}）` : "";
  return `${label}${where}${named}${c.hint ? ` · ${c.hint}` : ""}`;
}

export function CredentialFields({
  kind,
  value,
  onChange,
  saved,
}: {
  kind: GwKind;
  value: GwCredentialIn;
  onChange: (next: GwCredentialIn) => void;
  /** 修改时：原来的凭据（类型没变、值留空 = 沿用） */
  saved?: GwCredential;
}) {
  const set = (patch: Partial<GwCredentialIn>) => onChange({ ...value, ...patch });
  const keeps = saved && saved.type === value.type && saved.hint;
  const secretPh = keeps ? `已保存（${saved.hint}），留空不修改` : "";
  const text = (k: keyof GwCredentialIn, label: string, placeholder = "") => (
    <Field label={label}>
      <Input value={(value[k] as string | null | undefined) ?? ""} onChange={(e) => set({ [k]: e.target.value })} placeholder={placeholder} />
    </Field>
  );
  const secret = (label: string, multiline = false) => (
    <Field label={label}>
      {multiline ? (
        <Input.TextArea rows={3} value={value.secret ?? ""} onChange={(e) => set({ secret: e.target.value })} placeholder={secretPh} />
      ) : (
        <Input.Password value={value.secret ?? ""} onChange={(e) => set({ secret: e.target.value })} placeholder={secretPh} autoComplete="new-password" />
      )}
    </Field>
  );
  const grid = (children: React.ReactNode) => <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>{children}</div>;

  return (
    <>
      <Field label="上游凭据" hint="网关调上游时注入，保存后不回显。调用方只带自己的 MCP API Key，拿不到上游凭据">
        <Select
          style={{ width: 280 }}
          value={value.type}
          onChange={(type) => onChange(saved && saved.type === type ? credentialOf(saved) : { type, secret: "" })}
          options={GW_CREDENTIAL_TYPES[kind].map((t) => ({ value: t, label: GW_CREDENTIAL_LABEL[t] ?? t }))}
        />
      </Field>
      {value.type === "apikey" &&
        grid(
          <>
            <Field label="位置">
              <Select
                value={value.where || "header"}
                onChange={(where) => set({ where })}
                options={[
                  { value: "header", label: "请求头" },
                  { value: "query", label: "查询参数" },
                ]}
              />
            </Field>
            {text("key", "名称", "X-Api-Key")}
          </>,
        )}
      {value.type === "apikey" && secret("值")}
      {value.type === "bearer" && secret("Token")}
      {value.type === "header" && grid(<>{text("key", "请求头名称", "X-Token")}{secret("值")}</>)}
      {value.type === "basic" && grid(<>{text("user", "用户名")}{secret("密码")}</>)}
      {value.type === "oauth2" && (
        <>
          {text("token_url", "Token 地址", "https://idp.example.com/oauth2/token")}
          {grid(<>{text("user", "client_id")}{secret("client_secret")}</>)}
          {text("scope", "scope（可选）")}
          <div className={styles.muted} style={{ fontSize: 12, marginTop: -6, marginBottom: 12 }}>
            网关用 client_credentials 换 Token，缓存到过期前；各实例各自换
          </div>
        </>
      )}
      {value.type === "metadata" && grid(<>{text("key", "键", "authorization")}{secret("值")}</>)}
      {value.type === "mtls" && (
        <>
          <Field label="客户端证书（PEM）">
            <Input.TextArea rows={3} value={value.cert ?? ""} onChange={(e) => set({ cert: e.target.value })} />
          </Field>
          {secret("私钥（PEM）", true)}
        </>
      )}
    </>
  );
}

/** 凭据表单 → 接口：apikey 的位置缺省是请求头 */
export function credentialBody(c: GwCredentialIn): GwCredentialIn {
  return c.type === "apikey" && !c.where ? { ...c, where: "header" } : c;
}

export function IdentityCheckbox({ kind, value, onChange }: { kind: GwKind; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <Checkbox checked={value} onChange={(e) => onChange(e.target.checked)}>
      向上游传递调用者身份（{kind === "grpc" ? "metadata x-auth-uid、x-uid" : "请求头 X-Auth-Uid、X-Uid"}，以及
      X-Run-Id、X-Thread-Id）
    </Checkbox>
  );
}

// ═══════════════════════════════════════════════ 发现结果

/** 发现后默认勾选：支持的都勾上，REST 的 DELETE 除外（破坏性，让管理员自己决定） */
export function defaultPicked(found: GwDiscovered): string[] {
  return found.tools
    .filter((t) => !t.unsupported && !t.exists && !(found.kind === "rest" && t.label.startsWith("DELETE ")))
    .map((t) => t.key);
}

export function DiscoveredTable({
  found,
  picked,
  onPick,
}: {
  found: GwDiscovered;
  picked: string[];
  onPick: (keys: string[]) => void;
}) {
  const mcp = found.kind === "mcp";
  const pickable = (t: GwDiscoveredTool) => !mcp && !t.unsupported && !t.exists;
  return (
    <>
      {found.kind === "grpc" && found.services.length > 0 && (
        <div className={styles.muted} style={{ marginBottom: 8 }}>
          服务 <code>{found.services.join("、")}</code>（grpc.health、grpc.reflection 等系统服务不列出）
        </div>
      )}
      <Table<GwDiscoveredTool>
        size="small"
        rowKey="key"
        dataSource={found.tools}
        pagination={false}
        scroll={{ y: 320 }}
        rowSelection={
          mcp
            ? undefined
            : {
                selectedRowKeys: picked,
                onChange: (keys) => onPick(keys as string[]),
                getCheckboxProps: (t) => ({ disabled: !pickable(t) }),
              }
        }
        columns={[
          {
            title: mcp ? "工具" : found.kind === "rest" ? "接口" : "方法",
            render: (_, t) => (
              <span>
                <code>{t.label}</code>
                {t.stream && t.stream !== "unary" && <Tag style={{ marginLeft: 6 }}>{STREAM_LABEL[t.stream] ?? t.stream}</Tag>}
              </span>
            ),
          },
          ...(mcp
            ? []
            : [{ title: "工具名", width: 180, render: (_: unknown, t: GwDiscoveredTool) => (t.unsupported ? "—" : <code>{t.name}</code>) }]),
          {
            title: mcp ? "副作用（上游注解）" : "副作用",
            width: 130,
            render: (_, t) => (t.unsupported ? <span className={styles.muted}>不支持</span> : <EffectTag effect={t.effect} />),
          },
          {
            title: "说明",
            render: (_, t) => (
              <span className={styles.muted} style={{ fontSize: 12 }}>
                {t.unsupported ? `不支持：${t.unsupported}` : t.exists ? "服务里已有同名工具" : t.description}
                {t.issues.length > 0 && (
                  <Tag color="error" style={{ marginLeft: 6 }} title={t.issues.join("；")}>
                    定义不合规
                  </Tag>
                )}
              </span>
            ),
          },
        ]}
      />
      <div className={styles.muted} style={{ fontSize: 12, marginTop: 8 }}>
        {mcp
          ? `共 ${found.tools.length} 个工具，全部登记、拉到就能用；不需要的登记后停用。上游没标注只读的按「有副作用」处理`
          : `已选 ${picked.length} 个。副作用按${found.kind === "rest" ? " HTTP Method" : "方法名"}推断，描述、副作用登记后都能改${
              found.kind === "grpc" ? "；服务端流的消息合并成数组返回" : ""
            }`}
      </div>
    </>
  );
}

export function DiscoverError({ error }: { error: Error | null }) {
  if (!error) return null;
  return <Alert type="error" showIcon style={{ marginTop: 8 }} message="发现失败" description={error.message} />;
}
