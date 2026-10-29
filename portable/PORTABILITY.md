# 智慧医院系统：可迁移运行配置

这些工具放在**新解包工程根目录的 `portable/`**。工程可以解压在另一台 Ubuntu 22.04 机器的可写目录，包括带空格的目录；不需要原 Windows 电脑、原 SSH 私钥、原服务器地址、原 Unix 用户名或原 Docker socket。AGH 使用本地 Unix socket：`工程根/agent/agh-home-mhs/data/daemon/agnesd.sock` 的 UTF-8 长度须不超过 107 字节，固定上游版本还要求 `工程根/agent/agh-home-mhs/data/daemon/workers.sock` 不超过 103 字节。请使用足够短的目录；原版预检只检查前者，因此通过预检不保证满足后者。外部条件仍须准备：Isaac Sim 6.0.1、兼容 NVIDIA GPU/驱动、Docker、Node 24、自己的 Agnes 账号，以及官方医院、Nova Carter 和人物资产。

## 配置与顺序

1. `python3 portable/control.py init`，生成本机 `portable/config.json`，不覆盖已有文件。`instance_id` 默认由当前解包目录摘要生成，也可填一个独立短名称。
2. 在 shell 中设置 `ISAAC_HOME`，指向**自己的 Isaac Sim 6.0.1 安装根目录**（包含 `python.sh`）；或填写配置中的 `isaac_home`。`gpu_index` 默认 `0`，用本机 `nvidia-smi` 选择允许使用的 GPU。验证服务器采用 GPU 1 只是复验环境选择，不写成其他用户的运行要求。
3. 检查 `ports`、`ros_domain_id`、`instance_id`、`image`。默认服务端口：API 4288、聊天 4289、MHS Hub 4292、统一网页 4294、daemon WebSocket 4295；全部只监听 `127.0.0.1`。默认 ROS domain 为 74，机器人 namespace 保持官方场景的 `carter1`。
4. **先 `python3 portable/control.py prepare`，再执行复现构建和项目 setup。** prepare 按冻结来源 SHA 校验，只处理源码目录，不读取账户内容或原业务数据库；端口同时替换服务、Hub 客户端、浏览器 origin、SDK facade、MCP 与模型请求准入接口。安装阶段可以正常创建无账号的项目 profile 并安装项目插件，随后启动真实 Hub/网页/设备；不构造假 provider、假模型或假反馈。
5. 完成对应构建和资产下载后，执行 `python3 portable/control.py doctor`。它只读检查依赖、安装位置、GPU 元数据、Docker 访问、镜像、构建产物、项目 profile 存在性、人物资产摘要及端口；不会运行仿真或调用模型。profile 与账号验证分开报告：`account_model_authentication`、`real_agent_delivery` 为 `not_executed`，不会把配置文件存在当作账户或模型调用成功。依赖缺失按失败报告，不把语法检查算运行成功。
6. `python3 portable/control.py start`；在同一台主机的浏览器打开 `http://127.0.0.1:4294/`。如选择其他端口，使用 `status` 返回的实际地址。

**安装与仿真核验阶段**保留默认暂停值守，核验真实场景、八设备、地图、相机新鲜度及运动守护；guard 连接真实 Hub 后可以 `healthy=true`，但没有导航授权时仍应 `armed=false`。不要点击“开始值守”或发模型任务来代替安装验收。**真实 Agent 阶段**在自己的项目账号配置完成后再执行自然语言任务，并保存真实模型请求、设备事件、轨迹、交接及停止证据。未配置账号的 Agent 项按 NA，安装与协议通过不能代替配送通过。

`python3 portable/control.py status`、`stop`、`reset` 使用同一份配置。远程 GPU 主机的 SSH 只是可选浏览器连接方式，由访问者填写自己的主机与身份。直接在目标机器运行时不需要 PowerShell，也不需要建立原作者电脑的 SSH 转发。

`status` 与 `stop` 不要求 Isaac 安装仍然可访问；即使配置中的端口被误改，也委托已生成的生命周期管理器查询或停止原本副本，仍核验进程与容器归属。`start/reset` 拒绝配置与已生成源码不一致，避免控制到其他实例。

源码生成后修改**端口、ROS domain、容器名称或镜像名**须使用新解包副本重新 prepare/build；现有清单会拒绝静默更改。仅修改本机 `ISAAC_HOME`、资产根和 GPU index 不要求重新生成源码。`HOSPITAL_ASSETS_ROOT` 或 `assets_root` 可指定官方 Isaac 资产根；空值使用安装的官方资产根解析。它不是用于重新命名或改造医院地图的选项。

## 隔离与安全生命周期

- 工程根由 Python 文件位置求出；编译后的 JS 运行代码从明确的 `HOSPITAL_ROOT` 环境读取，避免 bundler 输出位置导致错误。MCP shell wrapper 按自己的脚本位置求根，引用路径加引号。
- AGH home、venv、运行包、资产缓存、日志与业务数据都位于当前工程目录。每个解包副本拥有自己的 `agent/agh-home-mhs`；不导入原服务器账号 home、业务 DB、PID、session、账号审批或旧设备状态。
- Docker 使用当前操作者正常可访问的 CLI/daemon，不设置固定 rootless socket，不需要 privileged 或 host PID。保留 ROS 所需 host 网络；已去除不必要的 host IPC，UDP-only FastDDS 配置保持。
- 活动容器必须同时匹配项目 label 与 `/project` 挂载来源。原来的 PID、argv、cwd、AGH home、运行模式检查继续有效。端口被占用且不能证实是本副本完整服务时拒绝启动。
- 运动守护、租约、幂等、容量、站点、交接 ACK、单会话控制、事件去重、新鲜度与模型预算算法均沿用原实现。停止和新一轮仍要求真实停止及未决交接对账；不能把仅有 PID 或 HTTP 200 当成验收通过。
- 不设置服务器 IP、SSH 身份、代理或 provider key。网络受限用户自行选择合法网络/代理；账号按正常登录流程准备，密钥不放入配置文件、源码或提交包。

## 来源与当前验证范围

`baseline-sha256.json` 绑定实际最新非敏感服务器源码 snapshot；`source-patch-manifest.json` 与 `portability.patch` 记录每个改动的前后摘要和完整 diff。最初离线检查在带空格目录完成：189 个 Python 源文件 compile、23 个 JS/MJS syntax check、重复 prepare 幂等、变更配置拒绝和旧服务器绝对根路径零残留均通过。**这只是离线校验，完整机器上的构建、导航、设备与真实 Agnes 验证须另存报告**。

该目录不负责上游 AGH 的七个正式 patch、Docker 镜像构建、账号配置和实机验收；它们由复现入口的对应步骤执行，并保留实际版本、摘要和失败结果。
