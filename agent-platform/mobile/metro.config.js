/**
 * Metro 配置：在 Expo 默认配置上，接入 packages/chat-ui 的源码（对话的事件归约、SSE、接口 hooks）。
 *
 * ★ chat-ui 在 App 目录之外（package.json 里 link: 过去），要加进 watchFolders 才会被打包。
 * ★ chat-ui 里的第三方 import（react、@tanstack/react-query）一律从 App 自己的 node_modules 取：
 *   chat-ui 旁边还有网页前端装的那一份，取到它就是两个 React，hooks 直接报错。
 */
const path = require("path");
const { getDefaultConfig } = require("expo/metro-config");

const config = getDefaultConfig(__dirname);
const chatUi = path.resolve(__dirname, "../../packages/chat-ui");

config.watchFolders = [...(config.watchFolders ?? []), chatUi];

const appAnchor = path.join(__dirname, "package.json");
const isBare = (name) => !name.startsWith(".") && !path.isAbsolute(name);
config.resolver.resolveRequest = (context, moduleName, platform) => {
  if (isBare(moduleName) && context.originModulePath.startsWith(chatUi + path.sep)) {
    return context.resolveRequest({ ...context, originModulePath: appAnchor }, moduleName, platform);
  }
  return context.resolveRequest(context, moduleName, platform);
};

module.exports = config;
