# 智慧医院系统 · 可复现运行指南

交付方式：**源码与固定构建输入、依赖锁、资产取得工具、启动入口和验证记录**。复现者在自己的 Linux GPU 主机运行，不需要作者的 Windows 电脑、服务器地址、用户名、SSH 私钥、旧容器或账号 home。运行源码包与历史参赛证据分别保留；不要把历史数据库、命令、ACK、PID 或会话复制到新工程冒充新运行。

本指南分成两阶段：先完成**安装与仿真**，再配置自己的账号验证**真实 Agent**。本次用户选择先核验安装与仿真，真实 Agent 项为 **NA／未执行**；不能用历史任务、设备注册成功或 HTTP 200 代替本次真实 Agent 验证。

## 运行前提

| 项目 | 要求或取得方式 |
|---|---|
| 主机 | Ubuntu 22.04，x86_64，可写的工程目录 |
| 仿真 | 通过 NVIDIA 官方渠道合法安装 Isaac Sim 6.0.1；本次安装实际VERSION为6.0.1-rc.7+release.42383.32955d8d.gl，按其兼容性检查准备 NVIDIA GPU、驱动与资源 |
| 容器 | 当前用户可以使用正常 Docker CLI/daemon；不要求作者的 rootless socket |
| Node | Node.js **24.14.0**，可调用 Corepack；固定 pnpm **10.34.5** |
| Python/构建 | 系统 Python 3.10、pip、git、C/C++ 编译工具及匹配 Node 的头文件；项目会建立新 SDK 虚拟环境 |
| 录像核验 | 本机 PATH 中的 ffmpeg；有限导航核验脚本用它保存真实 JPEG 录像，普通网页画面不依赖 ffmpeg |
| 路牌字体 | 至少提供布局支持的 Noto CJK 或 DejaVu 字体；Noto CJK 显示中文，只有 DejaVu 时使用英文路牌；全部缺失会阻止场景启动 |
| 外部资产 | 匹配的官方医院、Nova Carter、人物动作、材质与纹理；首次取得需要可访问的官方来源或本机合法资产库 |
| 真实 Agent | 后续使用自己的 Agnes 账号／provider 配置；凭据不在作品包中 |

建议解压到 `~/smart-hospital` 这样的短目录。AGH 使用本地 Unix socket，完整 `工程根/agent/agh-home-mhs/data/daemon/agnesd.sock` 的 UTF-8 长度必须 **≤107 字节**。带空格的目录受支持，但较深或中文很长的目录可能超过该协议限制，入口会明确拒绝；这不是对作者安装路径的绑定。

运行源码根目录应含 `portable/`、`reproduction/build-tools/`、`reproduction/nav-assets/`、`mhs/`、`configs/`、`maps/`。下列步骤都在该根目录执行。

Ubuntu 复现者可以自行选择安装中文字体：

```bash
sudo apt install fonts-noto-cjk
```

也可提供合法字体到工程 `assets/layout/NotoSansCJK-Regular.ttc`。场景识别的系统路径是 `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`、`/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf`。doctor 对 CJK 可用记 pass、只有 DejaVu 记 warn、全部缺失记 fail。本交付过程不修改验证服务器的系统字体。

## 八步快速开始

### 1. 解压到自己的短目录，声明自己的 Isaac 安装

将运行源码包解压，使上述目录直接位于 `~/smart-hospital` 下，然后执行：

```bash
cd "$HOME/smart-hospital"
export PROJECT_ROOT="$PWD"
export ISAAC_HOME="/path/to/your/isaac-sim-6.0.1"
export RUN_TAG="$(date -u +%Y%m%dT%H%M%SZ)"

test -f "$ISAAC_HOME/python.sh"
node --version
corepack --version
docker info --format '{{json .ServerVersion}}'
```

把 `/path/to/your/isaac-sim-6.0.1` 替换为自己的安装根目录。`ISAAC_HOME` 是本机运行条件，不会写成源码中的固定安装位置。所有下载工具不指定代理；如网络受限，由复现者准备自己的网络条件，不依赖作者的代理服务。

### 2. 初始化配置，再生成可迁移源码

```bash
if [ ! -f portable/config.json ]; then
  python3 portable/control.py init
fi
# 检查 portable/config.json；需要调整时在 prepare 前编辑。
python3 portable/control.py prepare
```

默认值如下。`instance_id` 由解包目录摘要生成；可改成自己唯一的短名称。

| 配置 | 默认值 |
|---|---|
| API / 聊天 / MHS Hub | 4288 / 4289 / 4292 |
| 统一工作台 / daemon WebSocket | 4294 / 4295 |
| ROS domain / GPU index | 74 / 0 |
| 导航镜像名 | `smart-hospital:humble-repro-v1` |
| 数据 | 当前工程的 `runs/smart-ward-v1` |
| AGH home | 当前工程的 `agent/agh-home-mhs` |

全部 Web 服务监听 `127.0.0.1`。同一主机已有其他 ROS 系统或端口被占用时，在 prepare 前选择独立端口、domain 和实例名称。安装完成后修改端口/domain/实例/镜像名需要用新解包副本重新 prepare/build，程序拒绝静默漂移。只修改 `ISAAC_HOME`、资产根或 GPU index 不需要重新生成源码。

### 3. 从固定官方源码构建完整 AGH/MHS 与新 SDK

```bash
python3 reproduction/build-tools/build_runtime.py \
  --root "$PROJECT_ROOT" --phase all
```

构建器优先使用附带的 `vendor/agnes-harness.bundle`，核验完整官方 tree 与提交，然后应用精确项目补丁；没有归档时从官方 Git 来源取得。它构建完整 runtime、MHS 插件、业务策略、统一网页，并创建新 `agent/mhs-venv`，不复用作者的运行包或虚拟环境。

如果系统没有可用的 `python3-venv`，可以显式使用项目内固定 bootstrap：

```bash
python3 reproduction/build-tools/build_runtime.py \
  --root "$PROJECT_ROOT" --phase all --venv-bootstrap
```

这两个命令是**同一步的两种选择**，不要在已有输出后执行第二个覆盖第一个。默认构建器遇到首次 stdlib venv 失败时也会保留本次 partial 环境后执行有限 bootstrap；未知的已存在 venv/bootstrap/runtime 一律拒绝。分阶段恢复可用 `fetch`、`patch`、`build`、`sdk`，先保留失败日志，再按实际失败阶段处理。不要运行历史 `mhs/build_runtime.py`、`mhs/prepare_runtime.py` 来替代此构建入口。

### 4. 用公开 ROS 基础镜像构建新的导航与业务容器

```bash
export HOSPITAL_IMAGE="$(python3 -c 'import json; print(json.load(open("portable/config.json"))["image"])')"
python3 reproduction/nav-assets/build_nav.py \
  --image "$HOSPITAL_IMAGE" \
  --base "ros@sha256:6b4abb5340e430c632bcf4710fb34996df4f80e502993e5755f8fc59a39c7b2e" \
  --output "runs/reproduction/nav-build-$RUN_TAG"
```

工具解析并记录官方 `ros:humble-ros-base-jammy` 的 digest，按锁定 apt/Python 依赖构建，不依赖旧镜像、私有 apt 缓存或其他 GPU 作业。`--output` 必须是新目录。若公开仓库无法提供锁定版本，构建明确失败，不静默换最新版。

上面的 linux/amd64 manifest 是本次验证的固定官方基础输入。Nav2 源版本仍为 1.1.20；本次显式选定官方仓库 20260916 的 Debian 构建，完整版本见 `apt-packages.lock`。旧 20260908 版本已经不可取得，其锁与实际构建失败分别保留，不混为同一镜像。

若 Docker daemon 无法连接公共 Registry，但运行主机或合法的中转机器可下载官方对象，可使用本包 `fetch_ros_archive.py` 按 manifest 与每层 SHA 制作公共 ROS 归档，再 `docker load`。这不是作者私有基础镜像；不要复制未知镜像或假造 RepoDigest。精确过程见 nav-assets/README，固定源可在工具 `--tag` 传入上述 `sha256:...`。

### 5. 取得并核验官方资产与地图输入

```bash
python3 reproduction/nav-assets/check_scene.py \
  --root "$PROJECT_ROOT" \
  --output "runs/reproduction/source-assets-$RUN_TAG.json"

python3 reproduction/nav-assets/prepare_assets.py \
  --root "$PROJECT_ROOT" --fetch --workers 4 \
  --output "runs/reproduction/people-fetch-$RUN_TAG.json"

python3 reproduction/nav-assets/verify_official_stage.py \
  --output "runs/reproduction/official-stage-$RUN_TAG.json"
```

`--fetch` 取得人物依赖闭包，并核验 194 个文件的大小与 SHA-256；只有完整通过才生成运行 manifest。省略 `--fetch` 只校验已有文件。下载失败、残缺或已有文件摘要错误会保留并报告，不把局部缓存当完整成功。

医院根 USD 小文件校验不代表所有医院、机器人、纹理依赖已离线取得。Isaac 首次启动仍需官方引用可达，或用户已有匹配的本地资产库。可通过 `HOSPITAL_ASSETS_ROOT` 或 `config.json` 中 `assets_root` 指定自己的官方资产根；保持该根下标准 `Isaac/...` 结构，不复制旧业务 scene 目录。

### 6. 正常安装项目插件，安装阶段无需模型账号

```bash
python3 reproduction/build-tools/install_plugins.py \
  --root "$PROJECT_ROOT" --execute
```

新独立 home 没有 profile 时，工具创建不含 provider/account/model 的合法项目 profile。随后按正常 `inspect → add → trust 本次 integrity/capability hash → enable` 流程安装 `@agnes/mhs` 和 `hospital-mhs-policy`，校验 MCP revision 并测试真实 transport/catalog。它不会复制旧账号 home、API Key 或审批设置，不构造模拟模型；协议安装测试不是 Agent 配送验收。

安装器建立本副本合法的智慧病区模式声明，核验实际八工具目录，并正常停止安装检查用的自身 daemon/Hub；插件信任、启用和 profile 状态保留。随后统一 start 从完整停止状态启动，不要求关闭公共服务或放宽端口占用检查。

### 7. 查看预检，再启动真实仿真与工作台

```bash
python3 portable/control.py doctor

export STARTED_WALL="$(python3 -c 'import time; print(time.time())')"
python3 portable/control.py start
python3 portable/control.py status
```

doctor 区分项目 profile 与账户认证：依赖和 profile 可通过，而 `account_model_authentication`、`real_agent_delivery` 标为 `not_executed`。doctor 不读取凭据、不运行仿真、不调用模型。确认依赖后 start 才启动本副本 Isaac、Nav2/API、真实 Agnes daemon/Hub、八设备适配器及统一工作台。

新启动可能需要资产加载和 navmesh 构建；以实际新鲜位姿、场景采样、Nav2 bootstrap 结果和真实 Hub 注册为准。只有 PID 或 HTTP 200 不算运行健康。

### 8. 打开统一网页，保存本轮安装与仿真证据

在**运行主机本机**的浏览器打开：

```text
http://127.0.0.1:4294/
```

确认医院地图、第一/第三人称画面、八设备新鲜反馈和默认暂停值守。没有导航任务时，独立 motion guard 应未 armed；真实 Hub 连接及续租可让其 `healthy=true`，这与获准运动不同。

再保存新生成场景的核验：

```bash
python3 reproduction/nav-assets/check_scene.py \
  --root "$PROJECT_ROOT" --generated --since "$STARTED_WALL" \
  --output "runs/reproduction/generated-scene-$RUN_TAG.json"
python3 portable/control.py status \
  > "runs/reproduction/runtime-status-$RUN_TAG.json"
```

此阶段保持“开始值守”关闭，不发送模型配送任务；核验的是安装、场景和设备接入。地图几何一致性不等于全站点导航通过，设备注册不等于真实 Agent 成功。

需要重复本次有限导航核验时，在新运行、无订单、值守关闭且机器人停稳的条件下执行：

```bash
agent/mhs-venv/bin/python reproduction/validate_runtime.py \
  --root "$PROJECT_ROOT" \
  --output "$PROJECT_ROOT/runs/reproduction/runtime-$RUN_TAG"
```

该检查不发送模型任务：读取八个精确设备及两个视角的新采样，真实到达物资库，重复同一操作验证不产生第二次导航，再观察真实运动、取消并停稳，之后更换目标返回物资库。使用原到达与停止门，上限 1500 墙钟秒、单腿 900 秒，工作结束前预留 150 秒清理。候选路线按确定性最短规则选择，不能把它算作 Agnes 的路径规划。失败后保留原件、其余项 NA，不自动重跑；完整智慧病区闭环仍需自己的真实账号另验。

## 远程运行时如何访问网页

SSH 转发是可选访问方式。代码和启动不依赖 SSH，也不需要作者的 PowerShell 脚本。在自己的访问电脑终端执行如下模板，**把 `USER`、`HOST`、`PORT`、`KEY` 替换为自己的授权连接信息**：

```bash
ssh -N -p PORT -i "KEY" \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:4294:127.0.0.1:4294 \
  -L 127.0.0.1:4289:127.0.0.1:4289 \
  -L 127.0.0.1:4292:127.0.0.1:4292 \
  -L 127.0.0.1:4295:127.0.0.1:4295 \
  USER@HOST
```

保持此终端打开，再在访问电脑浏览器打开 `http://127.0.0.1:4294/`。Windows PowerShell 需要将上面各行合并为一行，或使用其反引号续行；它不是仿真运行的必需平台。自定义端口时，转发和浏览器地址全部按自己的 config 同步。

## 后续配置自己的账号，验证真实 Agent

安装与仿真运行后，可在项目聊天网页的 Provider 设置配置自己的账号、测试连接、选择实际模型并保存；也可以打开固定上游的正常配置入口：

```bash
export AGH_HOME="$PROJECT_ROOT/agent/agh-home-mhs"
export AGNES_PROFILE=local-dev
node "$PROJECT_ROOT/agent/agh-runtime-smart-ward145/agnes.mjs" config
```

不要把 API Key 作为命令行参数、写入源码或提交包，不从作者机器拷贝账户。配置完成后再按演示步骤：新一轮 → 开始值守 → B 站放入标本，同时使 301 照明变暗 → 观察真实任务分解、选路、柜体交接、新读数恢复和最终核验。

完整结果必须关联本次真实模型请求、Hub/MHS 记录、订单/标本唯一编号、导航轨迹、场景 ACK、交接回执和新读数。模型调用失败、超预算、未交付及错误保留，不能把上一轮回执作为本轮成功。本次账号阶段由用户选择暂缓，**真实 Agent 复现验证保持 NA**。

## 停止、新一轮与归属

```bash
python3 portable/control.py status
python3 portable/control.py stop
# 需要保存当前任务并建立新运行时：
python3 portable/control.py reset
```

停止只处理当前副本记录的项目进程和容器，核验 PID、argv、cwd、AGH home、容器 label 与 `/project` mount 来源；保留独立运动守护、停止确认、交接未知状态对账和预算门。网页“停止本轮”停值守与当前任务；命令 `stop` 还停止该副本服务。关闭浏览器不停止后台运行。

容器内部 `/project` 是固定**挂载别名**，其来源为当前解包根目录；不是要求宿主机存在 `/project` 或原作者目录。ROS 需要 host 网络，但不使用 privileged、host PID 或固定 rootless socket，不管理公共 AGH、其他容器或其他 GPU 作业。

未知交接、真实停止未确认、端口占用或归属不匹配时保留原错误，先查清再操作。不要运行全局 `pkill`、删除 Docker 数据目录、覆盖失败运行或自动恢复旧导航。

## 版本、摘要与结果保存

| 内容 | 固定输入与实际输出 |
|---|---|
| AGH/MHS | 官方提交 `57d0c34b33527294042b7ab8a60d7c7c934e932e`；完整上游 bundle/tree 核验与项目 patch 摘要 |
| Node/pnpm | 24.14.0 / 10.34.5；安装机器的实际版本与构建命令另记日志 |
| SDK | 同一提交的 `packages/mhs/python`；主机依赖见 `reproduction/build-tools/requirements.lock.txt`，构建后执行 `pip check`、保存 freeze |
| 导航 | ROS Humble / Jammy，Nav2 1.1.20 锁定 apt 版本；业务容器 MCP 1.12.4、Pydantic 2.11.7 |
| Isaac | 6.0.1；合法安装与官方资产来源由复现者准备 |
| 人物 | 194 文件官方依赖闭包，逐文件 URL/大小/SHA-256 在资产 lock 中 |
| 配置/源码 | `portable/source-patch-manifest.json`、`portable/portability.patch` 记录精确前后摘要 |
| 图像 | 原始 JPEG 与采集时间/SHA，过期数据不能算新反馈 |

报告主要写入 `runs/reproduction/` 和 `portable/evidence/`；实际业务与协议数据写入本副本 `runs/smart-ward-v1/`。导航报告要分别列出官方基础镜像 RepoDigest、本地新构建 image ID；未推送镜像没有可公开拉取的 RepoDigest，不编造一个。

源码可在自己的短目录重新解包构建。已经安装的 Python venv、editable SDK 及临时服务记录带本机路径；移动已建目录后应从干净源码重新构建，不直接搬运安装产物。发行包不包含这些产物。

构建或资产取得失败时，保留本次报告、错误、partial 输出与版本锁；下一次使用新的输出名/运行目录。不得用旧运行包、旧缓存的完整标记、旧业务 DB 或模拟模型补成成功。安装输入、构建、场景、协议、导航、真实 Agent 要分项判断。

## 本次复现验证结论

2026年10月14日，独立目录的新源码构建、SDK、账号为空的插件安装、真实仿真与八设备反馈通过；有限导航未通过。本次使用同一台GPU主机和已有外部Isaac安装，不宣称另一台全新硬件或完整Agent复现通过。详细原件随“智慧医院系统-复现验证-20261014”交付，源码包内 `VERIFICATION.json` 给出分项摘要和索引。

| 项目 | 本轮状态 |
|---|---|
| 源码可迁移 | 两个带空格的不同解包路径 prepare 通过；240个源码跨路径一致，201个Python语法检查通过，20个保护核心未改，prepare后无作者服务器绝对根残留 |
| 完整AGH/MHS与新SDK | 官方固定tree独立构建通过；新SDK精确依赖、schema导入、pip check通过 |
| 新home普通插件安装 | 精确inspect/add/trust/enable通过；真实八工具目录成立；安装临时daemon正常退出，模型请求0 |
| 公开ROS基础镜像与Nav2 | 新镜像构建通过；镜像ID及所有实际apt/pip版本在原件中；Docker归档导入后RepoDigest为空，按NA保留 |
| 官方资产、地图、overlay | 194个文件444,304,007字节逐SHA通过；本轮新生成柜体地图与overlay校验通过 |
| 实际仿真与设备 | Isaac实际版本6.0.1-rc.7+release.42383.32955d8d.gl；Nav2九节点active，八个设备新鲜；第一/第三人称JPEG采集新鲜且摘要一致 |
| 有限导航 | 1 PASS、1 FAIL、3 NA；到物资库900墙钟秒超时，保留真实轨迹和录像，不延长预算或自动重跑 |
| 清理与停止 | 超时后真实停止确认，场景线速度0.001139m/s、角速度0.000044rad/s、连续10个静止样本；清理无错误，五个自身进程已退出且自身容器停止 |
| 本轮真实模型与完整Agent | **NA：按用户选择暂不配置账号；全新home真实模型请求计数0** |
| 官方完整MHS协议测试集、全站导航、配送与异常闭环 | NA／本轮未执行；八设备接入和安装通过不代替这些验收 |

首轮冷启动曾因45秒异步响应等待不足失败。原错误已保存，可复现版只把该等待改为150秒、总体就绪等待改为480秒；新运行的九节点激活记录通过。导航参数、业务预算、真实到站与静止条件保持原值。

导航有12次“无法取得进展”与恢复旋转，后来自行恢复真实前进，但仍未在原时限内到站。采样排除了对应时段的运动守护吞指令和明显定位平移错误；最初停滞的唯一根因尚未确定。不能将这份运行包描述为“导航或全部功能验收已通过”。

源码可以在自己的短目录重新构建；外部合法Isaac资产、兼容GPU、网络取得依赖与自己的Agnes账号仍是必要运行条件。已安装venv和editable SDK带本机路径，换目录时从干净源码重建。历史Agent成功、失败和v6记录与本轮安装验证分开保留。

作品表单“访问方式”可描述为：**提供可复现运行源码包，按本 README 在自有 Ubuntu 22.04／Isaac Sim 6.0.1／NVIDIA 环境启动；无需作者服务器登录。可先无账户核验场景与设备，再自行正常配置 Agnes 账号运行 Agent。附演示视频、历史真实执行证据及本次分项复验记录。**

协议证据边界：AGH普通mcp test成功，但其safe result的toolCount为0；最终8项工具目录来自随后SDK直接连接本副本stdio的list_tools。此处没有真实AGH模型会话的业务工具调用，不将目录读取视为Agent或完整协议验收。
