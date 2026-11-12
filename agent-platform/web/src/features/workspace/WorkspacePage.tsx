"use client";

/**
 * 我的工作空间（agent-platform/doc/prototype/workspace.html）：一个人的全部文件，普通的目录树。
 *
 * ★ 不按对话分、不显示对话信息。和对话的关系只有一处：文件夹上「新建对话」—— 新对话以这个目录为
 *   工作目录（建对话时定下、之后不能换）。所以有对话在用的目录不能改名，正在运行的不能删；
 *   确认框里会列出是哪些对话。
 * ★ 地址栏记当前目录和预览的文件（?dir=…&file=…）：刷新、分享链接都回到同一个位置。
 * ★ 点开头的是对话的内部记录（.atlas）：默认隐藏，打开后只读。
 */
import {
  App as AntdApp,
  Alert,
  Button,
  Dropdown,
  Empty,
  Input,
  Modal,
  Result,
  Skeleton,
  Switch,
  Table,
  Tooltip,
  type TableColumnsType,
} from "antd";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import {
  downloadWorkspaceFile,
  downloadWorkspaceZip,
  fetchWorkspaceRefs,
  useDeleteEntry,
  useMakeFolder,
  useMoveEntry,
  useWorkspaceDir,
  useWorkspaceOverview,
  useWorkspaceSearch,
  type WorkspaceFile,
} from "@/api/workspace";
import { Icon, type IconName } from "@/components/Icon";
import { formatBytes } from "@/features/files/format";
import { extOf } from "@/features/files/previewKind";
import { relativeTime } from "@/lib/format";
import { copyText } from "@atlas/chat-ui/src/lib/clipboard";

import { filesFromDrop, useUploads, type PickedFile } from "./uploads";
import { WorkspacePreview } from "./WorkspacePreview";
import styles from "./workspace.module.css";

interface Row {
  key: string;
  kind: "dir" | "file";
  name: string;
  path: string;
  size: number;
  modified_at?: string | null;
  /** 文件夹里的文件数 */
  files?: number;
  file?: WorkspaceFile;
}

type Dialog =
  | { type: "folder" }
  | { type: "rename"; row: Row }
  | { type: "delete"; rows: Row[] };

const join = (a: string, b: string) => (a ? `${a}/${b}` : b);
const parentOf = (p: string) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
const isHidden = (p: string) => p.split("/").some((s) => s.startsWith("."));

export function WorkspacePage() {
  const router = useRouter();
  const params = useSearchParams();
  const dir = params.get("dir") ?? "";
  const previewPath = params.get("file") ?? "";
  const { message } = AntdApp.useApp();
  const qc = useQueryClient();

  const [hidden, setHidden] = useState(false);
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [dialog, setDialog] = useState<Dialog | null>(null);

  const overview = useWorkspaceOverview();
  const listing = useWorkspaceDir(dir, hidden);
  const mkdir = useMakeFolder();
  const search = useWorkspaceSearch(q, hidden);
  const refresh = useCallback(() => void qc.invalidateQueries({ queryKey: ["workspace"] }), [qc]);
  const uploads = useUploads({ maxBytes: overview.data?.upload_max_bytes ?? 0, onChanged: refresh });

  const writable = !isHidden(dir);
  const searching = q.trim().length > 0;

  const go = useCallback(
    (to: string, file?: string) => {
      const sp = new URLSearchParams();
      if (to) sp.set("dir", to);
      if (file) sp.set("file", file);
      const qs = sp.toString();
      router.push(qs ? `/workspace?${qs}` : "/workspace");
    },
    [router],
  );
  useEffect(() => setSelected([]), [dir, hidden]);

  const rows = useMemo<Row[]>(() => {
    const d = listing.data;
    if (!d) return [];
    return [
      ...d.dirs.map((x) => ({ key: `d:${x.path}`, kind: "dir" as const, name: x.name, path: x.path, size: x.size, modified_at: x.modified_at, files: x.files })),
      ...d.files.map((f) => ({ key: `f:${f.path}`, kind: "file" as const, name: f.name, path: f.path, size: f.size, modified_at: f.modified_at, file: f })),
    ];
  }, [listing.data]);
  const files = useMemo(() => rows.filter((r) => r.kind === "file"), [rows]);
  const previewIndex = files.findIndex((r) => r.path === previewPath);
  const previewFile = files[previewIndex]?.file;

  // ── 上传：按钮（文件 / 文件夹）和拖放 ─────────────────────────────────────
  const pickFiles = useRef<HTMLInputElement>(null);
  const pickDir = useRef<HTMLInputElement>(null);
  const enqueue = (picked: PickedFile[]) => void uploads.enqueue(picked, dir);
  const [drag, setDrag] = useState(false);
  const canDrop = writable && !searching && overview.data?.configured !== false;
  useEffect(() => {
    let depth = 0;
    const hasFiles = (e: DragEvent) => [...(e.dataTransfer?.types ?? [])].includes("Files");
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      if (depth++ === 0) setDrag(true);
    };
    const over = (e: DragEvent) => {
      if (hasFiles(e)) e.preventDefault();
    };
    const leave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      if (--depth <= 0) {
        depth = 0;
        setDrag(false);
      }
    };
    const drop = (e: DragEvent) => {
      if (!hasFiles(e) || !e.dataTransfer) return;
      e.preventDefault();
      depth = 0;
      setDrag(false);
      if (!canDrop) return;
      void filesFromDrop(e.dataTransfer).then((picked) => uploads.enqueue(picked, dir));
    };
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragover", over);
    window.addEventListener("dragleave", leave);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragover", over);
      window.removeEventListener("drop", drop);
      window.removeEventListener("dragleave", leave);
    };
  }, [canDrop, dir, uploads]);

  // ── 每一行的操作 ───────────────────────────────────────────────────────────
  const newChat = (path: string) => router.push(`/?dir=${encodeURIComponent(path)}`);
  const download = (row: Row) =>
    (row.kind === "dir" ? downloadWorkspaceZip(row.path) : downloadWorkspaceFile(row.path)).catch((e: Error) =>
      message.error(e.message),
    );
  const copyPath = async (path: string) => {
    if (await copyText(path)) message.success(`已复制 ${path}`);
    else message.error("复制失败，请手动复制");
  };
  const open = (row: Row) => (row.kind === "dir" ? go(row.path) : go(dir, row.path));

  const opButton = (title: string, icon: IconName, onClick: () => void, className?: string) => (
    <Tooltip title={title}>
      <Button
        size="small"
        type="text"
        className={className}
        aria-label={title}
        icon={<Icon name={icon} size={15} />}
        onClick={(e) => {
          e.stopPropagation();
          onClick();
        }}
      />
    </Tooltip>
  );
  const moreMenu = (row: Row) => {
    const rw = !isHidden(row.path);
    return (
      <Dropdown
        trigger={["click"]}
        menu={{
          items: [
            ...(row.kind === "dir" && rw ? [{ key: "chat", icon: <Icon name="chat" size={14} />, label: "在这个目录新建对话" }, { type: "divider" as const }] : []),
            row.kind === "dir"
              ? { key: "zip", icon: <Icon name="download" size={14} />, label: "下载（zip）" }
              : { key: "download", icon: <Icon name="download" size={14} />, label: "下载" },
            { key: "copy", icon: <Icon name="copy" size={14} />, label: "复制路径" },
            ...(rw
              ? [
                  { type: "divider" as const },
                  { key: "rename", icon: <Icon name="pencil" size={14} />, label: "重命名" },
                  { key: "delete", icon: <Icon name="trash" size={14} />, label: "删除", danger: true },
                ]
              : []),
          ],
          onClick: ({ key, domEvent }) => {
            domEvent.stopPropagation();
            if (key === "chat") newChat(row.path);
            if (key === "zip" || key === "download") void download(row);
            if (key === "copy") void copyPath(row.path);
            if (key === "rename") setDialog({ type: "rename", row });
            if (key === "delete") setDialog({ type: "delete", rows: [row] });
          },
        }}
      >
        <Button size="small" type="text" aria-label="更多" icon={<Icon name="more" size={15} />} onClick={(e) => e.stopPropagation()} />
      </Dropdown>
    );
  };

  const columns: TableColumnsType<Row> = [
    {
      title: "名称",
      key: "name",
      sorter: (a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name, "zh") : a.kind === "dir" ? -1 : 1),
      render: (_, row) => (
        <div className={styles.name}>
          <FileIcon row={row} />
          <span className={styles.text}>{row.name}</span>
          {row.kind === "dir" && <span className={styles.count}>{row.files} 项</span>}
          {isHidden(row.name) && (
            <span className={styles.lock}>
              <Icon name="lock" size={12} />
              对话内部记录
            </span>
          )}
        </div>
      ),
    },
    {
      title: "大小",
      key: "size",
      width: 110,
      sorter: (a, b) => a.size - b.size,
      render: (_, row) => <span style={{ color: "var(--fg-3)" }}>{row.kind === "dir" && !row.files ? "—" : formatBytes(row.size)}</span>,
    },
    {
      title: "修改时间",
      key: "modified",
      width: 120,
      responsive: ["lg"],
      sorter: (a, b) => (a.modified_at ?? "").localeCompare(b.modified_at ?? ""),
      render: (_, row) => <span style={{ color: "var(--fg-3)" }}>{row.modified_at ? relativeTime(row.modified_at) : "—"}</span>,
    },
    {
      title: "",
      key: "ops",
      width: 116,
      render: (_, row) => (
        <div className={styles.ops}>
          {row.kind === "dir"
            ? !isHidden(row.path) && opButton("在这个目录新建对话", "chat", () => newChat(row.path), styles.chatOp)
            : opButton("预览", "eye", () => go(dir, row.path))}
          {opButton(row.kind === "dir" ? "下载（zip）" : "下载", "download", () => void download(row))}
          {moreMenu(row)}
        </div>
      ),
    },
  ];

  // ── 页面 ───────────────────────────────────────────────────────────────────
  if (overview.data && !overview.data.configured) {
    return (
      <div className={styles.page}>
        <Result status="info" title="工作空间暂不可用" subTitle="这个部署没有配置文件存储，请联系管理员" style={{ margin: "auto" }} />
      </div>
    );
  }

  const parts = dir ? dir.split("/") : [];
  const head = dir ? (
    <div className={styles.titleRow}>
      <nav className={styles.crumbs} aria-label="位置">
        <button type="button" onClick={() => go("")}>
          我的工作空间
        </button>
        {parts.map((p, i) => (
          <span key={i} style={{ display: "contents" }}>
            <span className={styles.sep}>
              <Icon name="right" size={16} />
            </span>
            {i === parts.length - 1 ? (
              <span className={styles.cur}>{p}</span>
            ) : (
              <button type="button" onClick={() => go(parts.slice(0, i + 1).join("/"))}>
                {p}
              </button>
            )}
          </span>
        ))}
      </nav>
      {!previewFile && <SearchBox value={q} onChange={setQ} />}
    </div>
  ) : (
    <div className={styles.titleRow}>
      <div style={{ flex: 1 }}>
        <h1 className={styles.title}>我的工作空间</h1>
        <div className={styles.usage}>
          <Icon name="folder" size={13} />
          {overview.data ? (
            <>
              已用 <b>{formatBytes(overview.data.used_bytes)}</b>
              {overview.data.quota_bytes > 0 && <> / {formatBytes(overview.data.quota_bytes)}</>}
              <span>·</span>
              {overview.data.files} 个文件
            </>
          ) : (
            "…"
          )}
        </div>
      </div>
      <SearchBox value={q} onChange={setQ} />
    </div>
  );

  const toolbar = selected.length ? (
    <>
      <span style={{ fontWeight: 600, fontSize: 13 }}>已选 {selected.length} 项</span>
      {writable && (
        <Button size="small" danger icon={<Icon name="trash" size={14} />} onClick={() => setDialog({ type: "delete", rows: rows.filter((r) => selected.includes(r.key)) })}>
          删除
        </Button>
      )}
      <Button size="small" type="text" onClick={() => setSelected([])}>
        取消选择
      </Button>
    </>
  ) : (
    writable && (
      <>
        <Dropdown
          trigger={["click"]}
          menu={{
            items: [
              { key: "files", icon: <Icon name="file" size={14} />, label: "上传文件" },
              { key: "dir", icon: <Icon name="folder" size={14} />, label: "上传文件夹" },
            ],
            onClick: ({ key }) => (key === "files" ? pickFiles : pickDir).current?.click(),
          }}
        >
          <Button type="primary" icon={<Icon name="upload" size={14} />}>
            上传 <Icon name="down" size={12} />
          </Button>
        </Dropdown>
        <Button icon={<Icon name="folder" size={14} />} onClick={() => setDialog({ type: "folder" })}>
          新建文件夹
        </Button>
        {dir && (
          <Tooltip title="以这个目录为工作目录开始一个新对话">
            <Button icon={<Icon name="chat" size={14} />} onClick={() => newChat(dir)}>
              在此新建对话
            </Button>
          </Tooltip>
        )}
      </>
    )
  );

  let content: ReactNode;
  if (searching) {
    content = <SearchResults q={q} result={search} onOpenDir={(p) => (setQ(""), go(p))} onOpenFile={(p) => (setQ(""), go(parentOf(p), p))} />;
  } else if (listing.error) {
    content = <Alert type="error" showIcon message="读取失败" description={(listing.error as Error).message} />;
  } else if (listing.isPending) {
    content = <Skeleton active />;
  } else if (!rows.length) {
    content = (
      <Empty
        style={{ marginTop: 60 }}
        description={
          dir
            ? writable
              ? "这个文件夹是空的：把文件拖到这里，或点「上传」"
              : "这个文件夹是空的"
            : "还没有文件：上传文件、新建文件夹，或者直接开始对话 —— 智能体生成的文件都会出现在这里"
        }
      />
    );
  } else {
    content = (
      <Table<Row>
        size="middle"
        rowKey="key"
        columns={columns}
        dataSource={rows}
        pagination={false}
        rowSelection={writable ? { selectedRowKeys: selected, onChange: (keys) => setSelected(keys as string[]) } : undefined}
        rowClassName={(row) => `${styles.row} ${row.path === previewPath ? styles.current : ""}`}
        onRow={(row) => ({ onClick: () => open(row) })}
        footer={listing.data?.truncated ? () => "文件太多，统计只看了前一部分" : undefined}
      />
    );
  }

  return (
    <div className={styles.page}>
      <div className={styles.main}>
        <header className={styles.head}>
          {head}
          {dir && isHidden(dir) && (
            <div className={styles.meta}>
              <Icon name="lock" size={13} />
              对话的内部记录，只读
            </div>
          )}
          {!searching && (
            <div className={styles.toolbar}>
              {toolbar}
              <span style={{ flex: 1 }} />
              <label style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 12.5, color: "var(--fg-3)", cursor: "pointer" }}>
                <Switch size="small" checked={hidden} onChange={setHidden} />
                显示隐藏文件
              </label>
            </div>
          )}
        </header>
        <div className={styles.body}>{content}</div>
      </div>

      {previewFile && !searching && (
        <WorkspacePreview
          file={previewFile}
          canPrev={previewIndex > 0}
          canNext={previewIndex < files.length - 1}
          onStep={(d) => go(dir, files[previewIndex + d]?.path)}
          onClose={() => go(dir)}
        />
      )}

      <input
        ref={pickFiles}
        type="file"
        multiple
        hidden
        onChange={(e) => {
          const picked = [...(e.target.files ?? [])].map((file) => ({ file, rel: file.name }));
          e.target.value = "";
          enqueue(picked);
        }}
      />
      <input
        ref={pickDir}
        type="file"
        multiple
        hidden
        {...{ webkitdirectory: "" }}
        onChange={(e) => {
          const picked = [...(e.target.files ?? [])].map((file) => ({ file, rel: file.webkitRelativePath || file.name }));
          e.target.value = "";
          enqueue(picked);
        }}
      />

      {drag && (
        <div className={styles.drop}>
          <div className={`${styles.dropIn} ${canDrop ? "" : styles.dropNo}`}>
            <Icon name={canDrop ? "upload" : "folder"} size={36} />
            {canDrop ? `松开上传到「${dir || "我的工作空间"}」` : "这里不能上传"}
          </div>
        </div>
      )}
      {uploads.panel}
      {uploads.conflict}

      {dialog?.type === "folder" && (
        <NameModal
          title="新建文件夹"
          initial="新建文件夹"
          okText="创建"
          taken={rows.map((r) => r.name)}
          onClose={() => setDialog(null)}
          onSubmit={(name) => makeFolder(join(dir, name))}
        />
      )}
      {dialog?.type === "rename" && <RenameModal row={dialog.row} taken={rows.map((r) => r.name)} onClose={() => setDialog(null)} onDone={(to) => previewPath === dialog.row.path && go(dir, to)} />}
      {dialog?.type === "delete" && (
        <DeleteModal
          rows={dialog.rows}
          onClose={() => setDialog(null)}
          onDone={() => {
            setSelected([]);
            if (dialog.rows.some((r) => r.path === previewPath)) go(dir);
          }}
        />
      )}
    </div>
  );

  async function makeFolder(path: string) {
    await mkdir.mutateAsync(path);
    message.success("已创建文件夹");
  }
}

function FileIcon({ row }: { row: Row }) {
  if (row.kind === "dir") {
    return (
      <span className={`${styles.fi} ${styles.fiDir}`}>
        <Icon name="folder" size={16} />
      </span>
    );
  }
  const ext = extOf(row.path);
  const [cls, icon]: [string | undefined, IconName] = ["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(ext)
    ? [styles.fiImg, "file"]
    : ["csv", "xlsx", "xls", "tsv"].includes(ext)
      ? [styles.fiSheet, "grid"]
      : ["md", "markdown", "txt", "pdf", "docx", "doc", "html"].includes(ext)
        ? [styles.fiDoc, "file"]
        : ["", "file"];
  return (
    <span className={`${styles.fi} ${cls}`}>
      <Icon name={icon} size={16} />
    </span>
  );
}

function SearchBox({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <Input
      allowClear
      style={{ width: 260 }}
      prefix={<Icon name="search" size={14} style={{ color: "var(--fg-4)" }} />}
      placeholder="搜索文件和文件夹"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

function SearchResults({
  q,
  result,
  onOpenDir,
  onOpenFile,
}: {
  q: string;
  result: ReturnType<typeof useWorkspaceSearch>;
  onOpenDir: (path: string) => void;
  onOpenFile: (path: string) => void;
}) {
  if (result.isPending) return <Skeleton active />;
  if (result.error) return <Alert type="error" showIcon message="搜索失败" description={(result.error as Error).message} />;
  const data = result.data!;
  const rows: Row[] = [
    ...data.dirs.map((d) => ({ key: `d:${d.path}`, kind: "dir" as const, name: d.name, path: d.path, size: 0 })),
    ...data.files.map((f) => ({ key: `f:${f.path}`, kind: "file" as const, name: f.name, path: f.path, size: f.size, modified_at: f.modified_at })),
  ];
  if (!rows.length) return <Empty style={{ marginTop: 60 }} description={`没有找到「${q.trim()}」：只按名称搜索，不搜文件内容`} />;
  return (
    <Table<Row>
      size="middle"
      rowKey="key"
      pagination={false}
      dataSource={rows}
      rowClassName={() => styles.row ?? ""}
      onRow={(row) => ({ onClick: () => (row.kind === "dir" ? onOpenDir(row.path) : onOpenFile(row.path)) })}
      footer={data.truncated ? () => "结果太多，只显示了一部分：换个更具体的关键词" : undefined}
      columns={[
        {
          title: `搜索「${q.trim()}」`,
          key: "name",
          render: (_, row) => (
            <div className={styles.name}>
              <FileIcon row={row} />
              <span className={styles.text}>{row.name}</span>
            </div>
          ),
        },
        {
          title: "所在位置",
          key: "where",
          width: 260,
          render: (_, row) => <span style={{ color: "var(--fg-3)" }}>{parentOf(row.path) || "我的工作空间"}</span>,
        },
        {
          title: "大小",
          key: "size",
          width: 110,
          render: (_, row) => <span style={{ color: "var(--fg-3)" }}>{row.kind === "file" ? formatBytes(row.size) : "—"}</span>,
        },
      ]}
    />
  );
}

/** 名称校验：和后端同一套规则（不能空、不能有 /、不能点开头、不能重名） */
function nameError(name: string, taken: string[], original?: string): string | undefined {
  if (!name) return "名称不能为空";
  if (/[\\/]/.test(name)) return "名称里不能有 / 或 \\";
  if (name.startsWith(".")) return "不能以 . 开头（这类名称是隐藏文件）";
  if (name !== original && taken.includes(name)) return "已有同名的文件或文件夹";
  return undefined;
}

function NameModal({
  title,
  initial,
  okText,
  taken,
  original,
  extra,
  blocked,
  onClose,
  onSubmit,
}: {
  title: string;
  initial: string;
  okText: string;
  taken: string[];
  original?: string;
  extra?: ReactNode;
  blocked?: boolean;
  onClose: () => void;
  onSubmit: (name: string) => Promise<unknown>;
}) {
  const { message } = AntdApp.useApp();
  const [value, setValue] = useState(initial);
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    const name = value.trim();
    const err = nameError(name, taken, original);
    if (err) return setError(err);
    if (name === original) return onClose();
    setBusy(true);
    try {
      await onSubmit(name);
      onClose();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal open title={title} okText={okText} cancelText="取消" onCancel={onClose} onOk={() => void submit()} confirmLoading={busy} okButtonProps={{ disabled: blocked }}>
      {!blocked && (
        <>
          <Input
            autoFocus
            value={value}
            onChange={(e) => {
              setValue(e.target.value);
              setError(undefined);
            }}
            onPressEnter={() => void submit()}
            status={error ? "error" : undefined}
          />
          {error && <div style={{ color: "var(--danger)", fontSize: 12.5, marginTop: 6 }}>{error}</div>}
        </>
      )}
      {extra}
    </Modal>
  );
}

function useRefs(paths: string[]) {
  return useQuery({
    queryKey: ["workspace", "refs", ...paths],
    queryFn: async () => (await Promise.all(paths.map(fetchWorkspaceRefs))).flatMap((r) => r.threads),
  });
}

const threadNames = (ts: { title: string }[]) =>
  ts
    .slice(0, 3)
    .map((t) => `「${t.title || "未命名对话"}」`)
    .join("、") + (ts.length > 3 ? ` 等 ${ts.length} 个` : "");

function RenameModal({ row, taken, onClose, onDone }: { row: Row; taken: string[]; onClose: () => void; onDone: (to: string) => void }) {
  const move = useMoveEntry();
  const refs = useRefs(row.kind === "dir" ? [row.path] : []);
  const users = refs.data ?? [];
  const blocked = row.kind === "dir" && users.length > 0;
  return (
    <NameModal
      title={`重命名${row.kind === "dir" ? "文件夹" : "文件"}`}
      initial={row.name}
      original={row.name}
      okText="确定"
      taken={taken}
      blocked={blocked}
      extra={
        blocked && (
          <Alert
            type="warning"
            showIcon
            message={`${threadNames(users)}对话以这里为工作目录，不能改名`}
            description="对话的工作目录在建对话时定下、之后不能换（沙箱里要挂载它）。要用新名字，就在新目录里开始新对话。"
          />
        )
      }
      onClose={onClose}
      onSubmit={async (name) => {
        const to = join(parentOf(row.path), name);
        await move.mutateAsync({ src: row.path, dst: to });
        onDone(to);
      }}
    />
  );
}

function DeleteModal({ rows, onClose, onDone }: { rows: Row[]; onClose: () => void; onDone: () => void }) {
  const { message } = AntdApp.useApp();
  const del = useDeleteEntry();
  const dirs = rows.filter((r) => r.kind === "dir");
  const refs = useRefs(dirs.map((r) => r.path));
  const users = refs.data ?? [];
  const busy = users.filter((t) => t.running);
  const count = rows.reduce((n, r) => n + (r.kind === "dir" ? (r.files ?? 0) : 1), 0);
  const [deleting, setDeleting] = useState(false);
  const submit = async () => {
    setDeleting(true);
    try {
      for (const r of rows) await del.mutateAsync(r.path);
      message.success(`已删除 ${rows.length} 项`);
      onDone();
      onClose();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setDeleting(false);
    }
  };
  return (
    <Modal
      open
      title={`删除 ${rows.length} 项？`}
      okText="删除"
      cancelText="取消"
      okButtonProps={{ danger: true, disabled: busy.length > 0 || refs.isPending }}
      confirmLoading={deleting}
      onCancel={onClose}
      onOk={() => void submit()}
    >
      <p>{count > rows.length ? `连同文件夹里的内容共 ${count} 个文件，` : ""}删除后无法恢复。</p>
      <div style={{ padding: "8px 12px", background: "var(--bg-2)", borderRadius: 9, fontSize: 13 }}>
        {rows.slice(0, 5).map((r) => (
          <div key={r.key} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {r.path}
            {r.kind === "dir" ? "/" : ""}
          </div>
        ))}
        {rows.length > 5 && <div style={{ color: "var(--fg-4)" }}>…还有 {rows.length - 5} 项</div>}
      </div>
      {busy.length > 0 ? (
        <Alert style={{ marginTop: 12 }} type="error" showIcon message={`${threadNames(busy)}正在运行，它的工作目录在要删除的范围里，等它结束再删`} />
      ) : (
        users.length > 0 && (
          <Alert style={{ marginTop: 12 }} type="info" showIcon message={`${users.length} 个对话以这里为工作目录（${threadNames(users)}）：对话保留，下次运行时从空目录开始`} />
        )
      )}
    </Modal>
  );
}
