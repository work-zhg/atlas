/**
 * 知识库服务（atlas-knowledge）的类型与请求。字段与后端 api/serializers.py 一致。
 * 手写而非生成：知识库服务独立部署，不进运行时的 OpenAPI 生成链路。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { http, knowledgeHttp } from "@/lib/http";

/** 新建时的共享预设（只在新建时用，之后在「共享」里调整） */
export type Permission = "only_me" | "all_team" | "partial_members";
/** 库级权限 = 用户中心数据权限（AtlasKnowledgeBase） */
export type KbLevel = "NONE" | "READ" | "WRITE" | "OWNER";
export type DocForm = "text_model" | "hierarchical_model";
export type SearchMethod = "semantic_search" | "full_text_search" | "hybrid_search";

export interface RetrievalConfig {
  search_method: SearchMethod;
  top_k: number;
  score_threshold_enabled: boolean;
  score_threshold: number;
  reranking_enable: boolean;
  reranking_mode: "reranking_model" | "weighted_score";
  reranking_model: { provider: string; model: string };
  weights: { vector_setting: { vector_weight: number }; keyword_setting: { keyword_weight: number } };
}

export interface Dataset {
  id: string;
  name: string;
  description: string;
  icon: string;
  owner_id: string;
  /** service = 服务账号建的库：只有它自己能用，不能共享 */
  owner_kind: "user" | "service";
  /** 我对这个库的级别（用户中心数据权限，含「全部知识库」授权） */
  my_level?: KbLevel;
  indexing_technique: string;
  embedding_model: string;
  retrieval_config: RetrievalConfig;
  doc_form: DocForm;
  review_required: boolean;
  document_count: number;
  word_count: number;
  created_at: string;
  can_write?: boolean;
  can_admin?: boolean;
}

export interface KbDocument {
  id: string;
  name: string;
  batch: string;
  doc_form: DocForm;
  indexing_status: "waiting" | "parsing" | "splitting" | "indexing" | "completed" | "error" | "paused";
  error: string | null;
  enabled: boolean;
  archived: boolean;
  available: boolean;
  review_status: "none" | "pending" | "approved" | "rejected";
  word_count: number;
  segment_count: number;
  completed_segments: number;
  indexing_latency: number | null;
  data_source_info: { filename?: string; size?: number; extension?: string };
  created_at: string;
  process_rule?: { mode: string; rules: ProcessRules } | null;
}

export interface Segmentation {
  separator: string;
  max_tokens: number;
  chunk_overlap: number;
}

export interface StructureRule {
  max_heading_level: number;
  max_length: number;
  min_length: number;
}

export interface ProcessRules {
  pre_processing_rules: { id: string; enabled: boolean }[];
  segmentation?: Segmentation;
  /** 按标题分段（通用分段 mode=structure，或父子分段 parent_mode=structure） */
  structure?: StructureRule;
  parent_mode?: "structure" | "paragraph" | "full_doc";
  subchunk_segmentation?: Segmentation;
}

export interface ChildChunk {
  id: string;
  position: number;
  content: string;
  /** 建向量时拼在正文前的上下文（标题路径、表头） */
  context?: string | null;
  word_count: number;
  type: string;
}

export interface Segment {
  id: string;
  position: number;
  content: string;
  title_path: string[];
  word_count: number;
  hit_count: number;
  enabled: boolean;
  status: string;
  child_chunks?: ChildChunk[];
}

export interface HitRecord {
  dataset_name: string;
  document_id?: string;
  document_uuid: string;
  document_name: string;
  segment_uuid: string;
  position: number;
  content: string;
  title_path: string[];
  score: number;
  child_chunks: { content: string; score: number }[];
}

export interface Meta {
  supported_extensions: string[];
  upload_max_bytes: number;
  max_segmentation_length: number;
  automatic_rules: ProcessRules;
  hierarchical_default_rules: ProcessRules;
  structure_text_defaults: StructureRule;
  default_retrieval_config: RetrievalConfig;
  embedding_models: { provider: string; model: string }[];
  rerank_models: { provider: string; model: string }[];
  operations: string[];
  trace_url_template: string | null;
}

/** 一次检索的过程（漏斗）：GET /v1/retrievals/{id} */
export type FunnelStatus = "selected" | "truncated" | "below_threshold" | "missing" | "dropped";

export interface FunnelStage {
  name: string;
  dataset_uuid: string | null;
  dataset_name: string | null;
  elapsed_ms: number;
  attrs: Record<string, unknown>;
  count: number;
}

export interface FunnelRow {
  segment_uuid: string;
  deleted: boolean;
  dataset_uuid: string | null;
  dataset_name: string | null;
  document_uuid: string | null;
  document_name: string | null;
  position: number | null;
  title_path: string[];
  content: string;
  status: FunnelStatus;
  dropped_at: number | null;
  final_rank: number | null;
  cells: ({ rank: number; score: number; points: number } | null)[];
}

export interface RetrievalFunnel {
  id: string;
  query: string;
  source: string;
  source_ref: string | null;
  created_at: string;
  latency_ms: number;
  trace_id: string | null;
  trace_url: string | null;
  config: RetrievalConfig;
  datasets: { id: string; name: string | null }[];
  stages: FunnelStage[];
  rows: FunnelRow[];
  truncated: boolean;
}

interface Page<T> {
  data: T[];
  total: number;
  page: number;
  limit: number;
}

const k = {
  meta: ["kb", "meta"] as const,
  datasets: ["kb", "datasets"] as const,
  dataset: (id: string) => ["kb", "dataset", id] as const,
  documents: (id: string) => ["kb", "documents", id] as const,
  document: (ds: string, doc: string) => ["kb", "document", ds, doc] as const,
  segments: (ds: string, doc: string) => ["kb", "segments", ds, doc] as const,
  queries: (id: string) => ["kb", "queries", id] as const,
  retrieval: (id: string) => ["kb", "retrieval", id] as const,
};

export function useKbMeta() {
  return useQuery({ queryKey: k.meta, queryFn: () => knowledgeHttp.get<Meta>("/v1/meta"), staleTime: 300_000 });
}

export function useDatasets(keyword?: string) {
  return useQuery({
    queryKey: [...k.datasets, keyword ?? ""],
    queryFn: () => knowledgeHttp.get<Page<Dataset>>("/v1/datasets", { limit: 100, keyword }),
  });
}

export function useDataset(id: string) {
  return useQuery({ queryKey: k.dataset(id), queryFn: () => knowledgeHttp.get<Dataset>(`/v1/datasets/${id}`) });
}

export function useDocuments(id: string, params: { page: number; keyword?: string; status?: string }) {
  return useQuery({
    queryKey: [...k.documents(id), params],
    queryFn: () =>
      knowledgeHttp.get<Page<KbDocument>>(`/v1/datasets/${id}/documents`, { ...params, limit: 20 }),
    // 有文档在入库就轮询（同 Dify 的入库进度）
    refetchInterval: (q) =>
      q.state.data?.data.some((d) => ["waiting", "parsing", "splitting", "indexing"].includes(d.indexing_status))
        ? 2000
        : false,
  });
}

export function useKbDocument(ds: string, doc: string) {
  return useQuery({
    queryKey: k.document(ds, doc),
    queryFn: () => knowledgeHttp.get<KbDocument>(`/v1/datasets/${ds}/documents/${doc}`),
  });
}

export function useSegments(ds: string, doc: string, params: { page: number; keyword?: string }) {
  return useQuery({
    queryKey: [...k.segments(ds, doc), params],
    queryFn: () =>
      knowledgeHttp.get<Page<Segment> & { doc_form: DocForm }>(
        `/v1/datasets/${ds}/documents/${doc}/segments`,
        { ...params, limit: 20 },
      ),
  });
}

export function useQueries(id: string) {
  return useQuery({
    queryKey: k.queries(id),
    queryFn: () =>
      knowledgeHttp.get<{
        data: {
          id: string;
          query: string;
          source: string;
          source_ref: string | null;
          hits: number;
          retrieval_id: string | null;
          created_at: string;
        }[];
      }>(
        `/v1/datasets/${id}/queries`,
        { limit: 20 },
      ),
  });
}

export function useRetrieval(id: string | undefined) {
  return useQuery({
    queryKey: k.retrieval(id ?? ""),
    queryFn: () => knowledgeHttp.get<RetrievalFunnel>(`/v1/retrievals/${id}`),
    enabled: !!id,
    staleTime: Infinity, // 检索记录不会变
  });
}

/** 写操作之后统一失效知识库相关的缓存（量小，不必精确到某一条） */
export function useKbMutation<A, R>(fn: (args: A) => Promise<R>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["kb"] }),
  });
}

export interface KbGrant {
  id: string;
  permission: Exclude<KbLevel, "NONE">;
  subject: {
    type: "USER" | "DEPT";
    id: string;
    name?: string;
    account?: string;
    status?: string;
    user_type?: "human" | "digital";
  };
  include_sub: boolean;
  source?: string;
  granted_at?: string;
}

export interface KbGrants {
  data: KbGrant[];
  my_level: KbLevel;
  can_manage: boolean;
  shareable: boolean;
}

export interface DirUser {
  id: string;
  name: string;
  account: string;
  /** digital = 数字员工（某个 Agent 的身份，数字员工设计 §3） */
  user_type?: "human" | "digital";
}

export interface DirDept {
  id: string;
  parent_id: string | null;
  name: string;
}

export function useGrants(id: string) {
  return useQuery({ queryKey: ["kb", "grants", id], queryFn: () => knowledgeHttp.get<KbGrants>(`/v1/datasets/${id}/grants`) });
}

/** 通讯录（atlas-server 转发用户中心）：共享时选人、选部门 */
export const directory = {
  /** digital：连数字员工一起（把知识库共享给某个 Agent） */
  users: (q: string, digital = false) =>
    http.get<DirUser[]>("/v1/directory/users", { q, limit: 20, digital: digital ? "true" : undefined }),
  depts: () => http.get<DirDept[]>("/v1/directory/depts"),
};

export const kbApi = {
  createDataset: (body: Partial<Dataset> & { name: string; permission?: Permission }) =>
    knowledgeHttp.post<Dataset>("/v1/datasets", body),
  addGrants: (id: string, body: { level: Exclude<KbLevel, "NONE">; subjects: { type: "user" | "dept"; id: string }[]; include_sub: boolean }) =>
    knowledgeHttp.post(`/v1/datasets/${id}/grants`, body),
  updateGrant: (id: string, aclId: string, body: { level?: Exclude<KbLevel, "NONE">; include_sub?: boolean }) =>
    knowledgeHttp.patch(`/v1/datasets/${id}/grants/${aclId}`, body),
  revokeGrant: (id: string, aclId: string) => knowledgeHttp.del<void>(`/v1/datasets/${id}/grants/${aclId}`),
  updateDataset: (id: string, body: Record<string, unknown>) => knowledgeHttp.patch<Dataset>(`/v1/datasets/${id}`, body),
  deleteDataset: (id: string) => knowledgeHttp.del<void>(`/v1/datasets/${id}`),
  estimate: (form: FormData) =>
    knowledgeHttp.post<{
      total_segments: number;
      total_words: number;
      preview: {
        content: string;
        word_count: number;
        title_path: string[];
        child_chunks?: string[];
        child_count?: number;
      }[];
    }>("/v1/datasets/indexing-estimate", form),
  upload: (id: string, form: FormData) =>
    knowledgeHttp.post<{ batch: string; documents: KbDocument[] }>(`/v1/datasets/${id}/documents`, form),
  docAction: (ds: string, doc: string, action: "pause" | "resume" | "retry") =>
    knowledgeHttp.post<KbDocument>(`/v1/datasets/${ds}/documents/${doc}/${action}`),
  docsStatus: (ds: string, action: string, ids: string[]) =>
    knowledgeHttp.post(`/v1/datasets/${ds}/documents-status/${action}`, { document_ids: ids }),
  review: (ds: string, ids: string[], approve: boolean, note?: string) =>
    knowledgeHttp.post(`/v1/datasets/${ds}/documents-review`, { document_ids: ids, approve, note }),
  deleteDoc: (ds: string, doc: string) => knowledgeHttp.del<void>(`/v1/datasets/${ds}/documents/${doc}`),
  renameDoc: (ds: string, doc: string, name: string) =>
    knowledgeHttp.patch(`/v1/datasets/${ds}/documents/${doc}`, { name }),
  createSegment: (ds: string, doc: string, content: string) =>
    knowledgeHttp.post<Segment>(`/v1/datasets/${ds}/documents/${doc}/segments`, { content }),
  updateSegment: (ds: string, doc: string, seg: string, body: { content?: string; enabled?: boolean }) =>
    knowledgeHttp.patch<Segment>(`/v1/datasets/${ds}/documents/${doc}/segments/${seg}`, body),
  deleteSegment: (ds: string, doc: string, seg: string) =>
    knowledgeHttp.del<void>(`/v1/datasets/${ds}/documents/${doc}/segments/${seg}`),
  hitTesting: (ds: string, query: string, retrieval_config?: Partial<RetrievalConfig>) =>
    knowledgeHttp.post<{
      query: string;
      records: HitRecord[];
      elapsed_ms: number;
      retrieval_id: string;
      trace_id: string | null;
    }>(
      `/v1/datasets/${ds}/hit-testing`,
      { query, retrieval_config },
    ),
};

export const STATUS_LABEL: Record<KbDocument["indexing_status"], { label: string; color: string }> = {
  waiting: { label: "排队中", color: "default" },
  parsing: { label: "解析中", color: "processing" },
  splitting: { label: "分段中", color: "processing" },
  indexing: { label: "建索引", color: "processing" },
  completed: { label: "可用", color: "success" },
  error: { label: "失败", color: "error" },
  paused: { label: "已暂停", color: "warning" },
};

export const PERMISSION_LABEL: Record<Permission, string> = {
  only_me: "仅自己",
  all_team: "全部成员只读",
  partial_members: "部分成员",
};

export const LEVEL_LABEL: Record<KbLevel, string> = {
  NONE: "无权限",
  READ: "只读",
  WRITE: "可编辑",
  OWNER: "所有者",
};

export const METHOD_LABEL: Record<SearchMethod, string> = {
  semantic_search: "语义检索",
  full_text_search: "全文检索",
  hybrid_search: "混合检索",
};

/** 标题路径的展示形式 */
export function pathLabel(path: string[] | null | undefined): string {
  return (path ?? []).join(" > ");
}
