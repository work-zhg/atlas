/**
 * 知识库服务（atlas-knowledge）的类型与请求。字段与后端 api/serializers.py 一致。
 * 手写而非生成：知识库服务独立部署，不进运行时的 OpenAPI 生成链路。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { knowledgeHttp } from "@/lib/http";

export type Permission = "only_me" | "all_team" | "partial_members";
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
  permission: Permission;
  indexing_technique: string;
  embedding_model: string;
  retrieval_config: RetrievalConfig;
  doc_form: DocForm;
  review_required: boolean;
  document_count: number;
  word_count: number;
  created_at: string;
  members?: { id: string; level: "read" | "write" }[];
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

export interface ProcessRules {
  pre_processing_rules: { id: string; enabled: boolean }[];
  segmentation?: Segmentation;
  parent_mode?: "paragraph" | "full_doc";
  subchunk_segmentation?: Segmentation;
}

export interface ChildChunk {
  id: string;
  position: number;
  content: string;
  word_count: number;
  type: string;
}

export interface Segment {
  id: string;
  position: number;
  content: string;
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
  score: number;
  child_chunks: { content: string; score: number }[];
}

export interface Meta {
  supported_extensions: string[];
  upload_max_bytes: number;
  max_segmentation_length: number;
  automatic_rules: ProcessRules;
  hierarchical_default_rules: ProcessRules;
  default_retrieval_config: RetrievalConfig;
  embedding_models: { provider: string; model: string }[];
  rerank_models: { provider: string; model: string }[];
  operations: string[];
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
      knowledgeHttp.get<{ data: { id: string; query: string; source: string; hits: number; created_at: string }[] }>(
        `/v1/datasets/${id}/queries`,
        { limit: 20 },
      ),
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

export const kbApi = {
  createDataset: (body: Partial<Dataset> & { name: string }) => knowledgeHttp.post<Dataset>("/v1/datasets", body),
  updateDataset: (id: string, body: Record<string, unknown>) => knowledgeHttp.patch<Dataset>(`/v1/datasets/${id}`, body),
  deleteDataset: (id: string) => knowledgeHttp.del<void>(`/v1/datasets/${id}`),
  estimate: (form: FormData) =>
    knowledgeHttp.post<{
      total_segments: number;
      total_words: number;
      preview: { content: string; word_count: number; child_chunks?: string[]; child_count?: number }[];
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
    knowledgeHttp.post<{ query: string; records: HitRecord[]; elapsed_ms: number }>(
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
  all_team: "全部成员",
  partial_members: "部分成员",
};

export const METHOD_LABEL: Record<SearchMethod, string> = {
  semantic_search: "语义检索",
  full_text_search: "全文检索",
  hybrid_search: "混合检索",
};
