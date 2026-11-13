/**
 * 文件的下载、分享、用其他应用打开。
 *
 * ★ 都从预览站点取文件（令牌在路径里，不需要登录 Cookie）：先下到 App 的缓存目录，再交给系统。
 * ★ 分享 = 系统分享面板（发给微信、邮件……），不生成公开链接。
 * ★ 保存到手机 = 系统的目录选择器（SAF），用户选一个文件夹，文件拷过去。
 */
import { Directory, File, Paths } from "expo-file-system";
import * as Sharing from "expo-sharing";

import { baseName } from "./look";

/** 下载到缓存目录（同名覆盖） */
async function toCache(url: string, path: string): Promise<File> {
  const dir = new Directory(Paths.cache, "files");
  if (!dir.exists) dir.create({ intermediates: true });
  const target = new File(dir, baseName(path) || "file");
  return File.downloadFileAsync(url, target, { idempotent: true });
}

export async function shareFile(url: string, path: string): Promise<void> {
  const file = await toCache(url, path);
  if (!(await Sharing.isAvailableAsync())) throw new Error("这台手机不支持分享");
  await Sharing.shareAsync(file.uri, { dialogTitle: baseName(path) });
}

/** 交给手机上的其他应用打开（Office 文档、压缩包这类 App 里不能预览的） */
export async function openWithOtherApp(url: string, path: string): Promise<void> {
  const file = await toCache(url, path);
  if (!(await File.canPreview(file))) throw new Error("手机上没有能打开这种文件的应用");
  await File.preview(file);
}

/** 保存到手机：让用户选一个文件夹 */
export async function saveToDevice(url: string, path: string, mimeType: string | null = null): Promise<string> {
  const file = await toCache(url, path);
  const dir = await Directory.pickDirectoryAsync();
  const target = dir.createFile(baseName(path), mimeType);
  await target.write(await file.bytes());
  return dir.name;
}
