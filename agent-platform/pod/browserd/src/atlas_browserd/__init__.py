"""浏览器守护（c4-model/agent/browser/code-browser-daemon.html）：浏览器镜像里的 Python 程序。

    paths.py       本地盘上的目录和文件：profile、就绪标记、画面口令、接管记录
                   （都在 /tmp，不在 /data）
    spec.py        ProcessSpec：虚拟屏幕、Chromium、画面服务三个进程的参数
    supervisor.py  Supervisor：拉起并看着三个进程，挂了就重启；就绪 = CDP 能连上
    cdp.py         CdpSession：经 Playwright 连到本机的 Chromium（connect_over_cdp），用默认上下文
    state.py       StateIO：登录态进出 Chromium（导入、按站点导出、清站点）；SiteMatch
    windows.py     TakeoverWindows：接管窗口的开关和前后比较；SiteDiff
    __main__.py    PlatformCommands：Atlas 经 execd 执行的命令行（atlas-browserd <子命令>）
    command.py     BrowserCommand：模型用的 browser 命令（带会话名转给 Playwright CLI）

★ 平台命令和模型命令连同一个 Chromium，但不共用代码路径：导入导出、接管窗口是平台的事。
★ 不保存登录态：只在本地盘上放一会儿，Atlas 取走就删；也不判断谁能看画面（Atlas 转发前核对过）。
★ 可注册域名不在这里算（公共后缀表只在 Atlas 一处）：这里按「站点或它的子域名」匹配，
  变了的只报域名。
"""
