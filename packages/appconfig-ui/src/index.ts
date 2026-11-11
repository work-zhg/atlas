/**
 * @atlas/appconfig-ui —— 应用配置（infra-platform/libs/appconfig，atlas_appconfig）的管理界面。
 *
 * 任何挂了 ConfigRoutes 的应用都能直接嵌入：组件只调用库自带的五个管理接口（列表、查看、改值、历史、
 * 回滚），接口根地址与请求头由宿主注入，样式跟着宿主的 antd 主题走。
 *
 *   <AppConfigPanel endpoint={`${API_BASE}/v1/configs`} headers={authHeaders} readOnly={!canEdit} />
 *
 * 宿主要有 antd 5 与 @tanstack/react-query 5（QueryClientProvider）。
 */
export { AppConfigPanel, type AppConfigPanelProps } from "./AppConfigPanel";
export {
  AppConfigError,
  appConfigApi,
  type AppConfigApi,
  type AppConfigClient,
  type ConfigOut,
  type RevisionOut,
  type RollbackIn,
  type SetIn,
} from "./api";
export { parseType, type ConfigValue, type ValueType } from "./value";
