/**
 * @atlas/jobs-ui —— 定时任务（infra-platform/libs/jobs，atlas_jobs）的管理界面。
 *
 * 任何挂了 JobRoutes 的应用都能直接嵌入：组件只调用库自带的管理接口，接口根地址与请求头由宿主注入，
 * 样式跟着宿主的 antd 主题走。这一版只读、只有实例任务（系统机制）。
 *
 *   <JobsPanel endpoint={`${API_BASE}/v1/jobs`} headers={authHeaders} />
 *
 * 宿主要有 antd 5 与 @tanstack/react-query 5（QueryClientProvider）。
 */
export { JobsPanel, type JobsPanelProps } from "./JobsPanel";
export { JobsError, listJobs, type InstanceJobOut, type JobsClient, type JobsOut } from "./api";
