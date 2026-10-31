"""技能应用：技能包的唯一所有者（上传、扫描、审查、发布、停用、下架）。

在用户中心是独立应用「技能」（应用编码 skill，register.py），操作码登记在它名下。
运行时（atlas_agent）只读消费这里的已发布内容，调用方向只有 server → skill。
对外契约只有两样：HTTP 接口（api/internal.py）与 `atlas_skill.schemas`。
"""
