# atlas-eng：工程场景集

doc/agent-platform-eval-plan.html §5。每个场景 = 模型稳能做成的基础任务 + 一个注入条件 + 期望行为。
基础任务刻意简单：失败只能归因于工程（平台 / harness），而不是模型不会做。

| 目录 | 计划编号 | 考察 |
|---|---|---|
| big-output | E02 | 命令输出被沙箱截断（只留首尾各 50KB）后，能发现截断并换 grep 定位 |
| approval-denied | E07 | 第一次审批被拒，改用别的办法完成，不假装成功 |
| parallel-approval | E09 | 一批并行的有副作用命令都要审批（挂起再续跑），每条只执行一次 |
| parallel-delegation | E12 | 同一轮并行派给 ephemeral 子智能体，汇总正确 |
| bad-path | E16 | 读目录 / 非法路径收到错误后自行纠正，run 不中断 |

副作用场景用计数文件检测重复执行：每执行一次往文件追加一行，校验时数行数。
