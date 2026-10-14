# 智慧医院系统

医院仿真中的标本取送与照明反馈调度原型。开启值守后，设备发现需求并通知 Agnes；Agent 安排机器人、柜体和灯具执行任务，再根据新反馈确认交接或照明恢复。

## 功能

- 标本柜检测待取箱体，机器人执行取件、送检和回执核验。
- 301、303 病房的照明监测、远程调节和故障反馈。
- Agnes 分解任务、安排顺序，并在配置的候选路线中选择。
- 统一工作台显示聊天、任务安排、地图、载货、设备状态和双视角仿真画面。
- 容量、地点、标本身份、幂等和运动停止检查。

## 运行

环境要求：Ubuntu 22.04、兼容的 NVIDIA GPU 与驱动、Isaac Sim 6.0.1、Docker、Node.js 24.14.0、pnpm 10.34.5、Python 3.10。

先阅读 [部署指南](docs/DEPLOYMENT.md)，在短目录中依次初始化配置、prepare、构建运行环境、取得官方资产、安装插件，再启动工作台。项目需要用户自行配置合法 Agnes 账号；API Key、Isaac 安装包及官方运行资产不随仓库提供。

```bash
python3 portable/control.py init
python3 portable/control.py prepare
# 按部署指南完成构建、资产准备和插件安装
python3 portable/control.py doctor
python3 portable/control.py start
```

默认本机工作台地址为 `http://127.0.0.1:4294/`。停止及状态使用 `python3 portable/control.py stop` 和 `status`；完整步骤以部署指南为准。

## 验证状态

| 范围 | 结果 |
|---|---|
| 独立源码构建、仿真启动、八设备反馈 | 已验证 |
| 独立部署的有限导航检查 | 到物资库 900 秒超时；后续项目未执行 |
| 独立部署的真实 Agnes 完整任务 | 未执行；独立配置未设置账号 |

已有另一运行中的标本送检与照明恢复成功记录，但不等同于本仓库在全新部署中的完整验收。分项结果见 [VERIFICATION.json](VERIFICATION.json)。箱体交接采用程序仿真，相对照明指数不是实际照度；尚未验证真实医院或临床效果。

## 目录

- `mhs/`：设备、事件、业务与工作台。
- `portable/`：可迁移配置及启动管理。
- `reproduction/`：固定构建输入、依赖锁、资产获取和验证工具。
- `configs/`、`maps/`：仿真和导航配置。
- `vendor/`：固定官方 AGH 源码 Git bundle。

## 来源与许可

AGH/MHS 基于官方提交 `57d0c34b33527294042b7ab8a60d7c7c934e932e`，上游 LICENSE 和 NOTICE 保留在 `third-party/`。Isaac Sim、官方资产及其他组件按各自许可取得和使用，详见 [THIRD_PARTY.md](THIRD_PARTY.md)。团队项目代码的单独开源许可尚未确定，本仓库不以统一许可替代第三方条款。

开发和材料整理使用 Codex 辅助；运行时任务调度使用 `agnes-3.0-flash`。
