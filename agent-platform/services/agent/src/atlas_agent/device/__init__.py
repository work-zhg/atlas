"""设备（c4-model/agent/device/code-device.html）：本机 CLI 智能体 —— CLI 跑在使用者自己的电脑上。

三个领域：

    设备登记    registry（DeviceService：配对码换设备令牌、默认设备、吊销）
    设备会话    sessions（DeviceSessions：定运行位置、每轮经设备网关交接能力；CLI 运行时的 CliHost）
                · gateway（DeviceGateway：设备网关的内部接口）
    本机文件    files（DeviceFiles：网页上只读查看本机会话的项目目录，打开时经设备网关现读）

电脑主动连出到设备网关（atlas_devicegw），平台通过这条连接找到电脑；平台和 bridge 之间的协议、
审批、断线续传都和会话 Pod 一样，网关和电脑上的连接器只转发。

★ 这里不导入任何子模块（同 cli/__init__）：包一导入就把数据库、HTTP 客户端一起拖进来。
"""
