# 导航与 Isaac 外部资产

这个目录替换旧 Dockerfile 的私有基础镜像和 `vendor/apt-cache`。不需要作者的电脑、SSH 私钥、服务器地址或旧容器。Isaac Sim 及其官方资产由复现者从 NVIDIA 官方渠道安装；此处只交付项目地图、配置、代码，以及公开资产的 URL 和摘要。

## 官方依赖与构建

Nav2 使用 Ubuntu 22.04 / ROS 2 Humble 官方镜像 `ros:humble-ros-base-jammy`。`build_nav.py` 先解析并记录公开仓库 digest，再按该 digest 构建新镜像；直接 apt 和 Python 版本锁在两个 lock 文件中，全量解析结果写入新报告。构建不使用原服务器 apt 缓存，不启动 GPU 或 ROS 服务。

```bash
python3 reproduction/nav-assets/build_nav.py \
  --image smart-hospital:humble-repro-v1 \
  --output runs/reproduction/nav-build-01
```

`--output` 必须是新目录。仓库不再提供某个锁定版本时构建明确失败，保留原因；不要静默替换为任意最新版。生成的 Docker image ID 是本地构建摘要，未推送的镜像不会有可公开拉取的 RepoDigest；报告中把它与官方基础镜像 RepoDigest 分别列出。

如果官方仓库已删除历史 build-suffix 版本，可明确建立新的复现依赖版本提议：

```bash
python3 reproduction/nav-assets/resolve_nav_lock.py \
  --output runs/reproduction/nav-lock-proposal-01
# 已导入官方 archive 时另外提供 --archive-proof <该来源证明JSON>
```

解析器只在新建的项目标记临时容器中刷新 apt metadata，记录官方 sources、policy、madison 和九项 exact candidate，输出 `apt-packages.proposed.lock`；不安装任何包，也不替换发行 lock。维护者检查版本差异后，有意选定新 lock 并保存提议及旧构建失败，再进行新的镜像构建和真实 Nav2 验收。APT 候选变化不能自动获得旧实验的导航成功结论。

容器构建是否通过以根目录新生成的实际验证报告为准，不能把这份构建脚本当作构建已通过的证据。构建通过也不代替真实仿真、导航和 Agent 验收。

### Docker daemon 无法连接官方仓库时

正常网络优先使用上面的 `docker pull`。如果 daemon 无法连接 Docker Hub，而普通 HTTPS 可以访问官方 Registry，可在任何联网的 Windows/Linux 电脑下载同一公开镜像，搬运到目标 Linux 主机导入；这只是依赖文件传输，不依赖作者机器或私有镜像。

```bash
python3 reproduction/nav-assets/fetch_ros_archive.py --root "$PWD" \
  --cache "$PWD/vendor/official-ros-cache" \
  --output "$PWD/runs/reproduction/public-ros-fetch-01.json" --no-load
# 把 cache/official-ros-image.tar 及上述 proof JSON 传到目标机器；独立比对 archive_sha256
docker load -i vendor/official-ros-cache/official-ros-image.tar
python3 reproduction/nav-assets/build_nav.py --archive-proof runs/reproduction/public-ros-fetch-01.json \
  --image smart-hospital:humble-repro-v1 --output runs/reproduction/nav-build-02
```

PowerShell 中用 `python` 和 `$PWD.Path` 表示解释器和当前目录即可；不需要 SSH，也可以用 USB/文件共享传输。工具遵循标准 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY` 环境变量，不固定代理端口。匿名 public-pull token 只存内存；配置和镜像层来自 `docker.io/library/ros`。manifest/config/压缩层 digest、解压层 `rootfs.diff_ids` 均校验。生成归属明确的新缓存，可复用已校验对象；不会使用旧项目私有基础镜像。

`docker load` 后本地别名为 `smart-hospital-upstream/ros:humble-base`。构建前后都要求这个 tag 的 image ID 等于官方 config digest。回退构建只在当前子进程设置 `DOCKER_BUILDKIT=0`，以已验证本地 tag 和 `--pull=false` 使用经典 Docker builder；不会修改全局 daemon 设置。裸 config ID 不能直接用于 BuildKit 的 `FROM`，否则可能被解析为 registry 名称并再次联网。导入后的 RepoDigest 可能为空，此时报告为 NA，同时保留官方 Registry manifest digest、config SHA 和构建前后 image ID；不把本地别名伪称为可公开拉取的 digest。正常联网的公开 RepoDigest 路径不使用该回退设置。

## 人物资源取得与完整性

人物动作用固定官方 Motion Library 依赖闭包：194 个 USD，444,304,007 字节（约 424 MiB）。`people-assets.lock.json` 含每个官方 URL、相对缓存路径、大小、SHA-256。它不含作者的绝对目录或缓存二进制。取得官方资产仍须遵守 NVIDIA 许可和网络条件。

```bash
# 先校验项目源地图和配置（不用 GPU）
python3 reproduction/nav-assets/check_scene.py --root "$PWD" \
  --output runs/reproduction/source-assets-01.json

# 下载缺失项，并核验所有 194 项；只有完整通过才创建运行所需 manifest
python3 reproduction/nav-assets/prepare_assets.py --root "$PWD" --fetch \
  --workers 4 --output runs/reproduction/people-fetch-01.json

# 无网络的再次校验
python3 reproduction/nav-assets/prepare_assets.py --root "$PWD" \
  --output runs/reproduction/people-verify-01.json

# 官方医院 USD 根文件的小文件摘要校验，不下载整个场景依赖
python3 reproduction/nav-assets/verify_official_stage.py \
  --output runs/reproduction/official-stage-01.json
```

下载工具只访问 lock 中的官方 HTTPS URL，不导入 `pxr` 或调用 Isaac。部分取得不生成完整缓存 manifest。已有文件大小/摘要错误会失败并保留原文件，要求查清缓存或上游变化。

根场景为 Isaac 6.0 资产库中的 `Isaac/Samples/ROS2/Scenario/multiple_robot_carter_hospital_navigation.usd`，运行时由 Isaac 6.0.1 的资产根解析器定位。根文件固定为 23,869 字节、SHA-256 `133f9c5feeb8e2e33b8d0cb558e91c0b8cf62e8ab68e1ba9df78ab80e6f62b2d`。机器人、医院模型、材质和纹理通过官方资产引用加载；这次小文件校验并不证明所有引用已离线取得。首次启动需要对应官方资产可访问，或复现者已安装匹配的本地资产库。

## 地图和新场景生成

原导航图固定为 1532 × 836 像素、0.05 米/像素、原点 `[-49.625, -4.675, 0]`；PNG SHA-256 为 `ed6424b156c9acc3c09631f154c1e789376f4c43a453f585df893e1d4471412b`。配置中的位置是该官方医院地图坐标，与安装目录无关。

智慧病区柜体、内滑门、灯具、标本箱、路牌在每次 Isaac 启动时由项目代码新建到项目 USD session sublayer。`SmartWardScene` 遍历实际 USD instance proxy 和碰撞/几何包围盒，重新选择柜体位置，并把柜体占地投影到专用地图副本。不要把旧 `runs/smart-ward-v1/scene`、数据库、命令/ACK、回执、PID、会话或导航证据复制进可运行项目。

调用启动命令前记录墙钟时间：

```bash
startup_time=$(python3 -c 'import time; print(time.time())')
# 此处使用根 README 的启动命令，等待真实 fresh pose / 八设备 / Nav2 ready
python3 reproduction/nav-assets/check_scene.py --root "$PWD" --generated \
  --since "$startup_time" --output runs/reproduction/new-scene-01.json
```

校验要求地图、柜体布局和 overlay 都是这个时间之后生成，且 overlay 摘要、三柜封套与固定场景一致。占地灰度像素固定为 SHA-256 `9bb7b21f30668583ab88f0808d87d507e0bbd0e1a89c26d785a18a54a5b0ee0a`。PNG 压缩器版本可能改变文件字节，因此生成图同时校验实际 PNG SHA 与本次 manifest 一致、灰度占地像素与冻结值一致；原始输入 PNG 字节摘要仍严格固定。历史生成 PNG 的字节 SHA `64938c41757eca06fed3c9d5eeeb76c952e6d31f4a2198b49fbe4dd44ab8184f` 保留作 provenance，不能用它代替新场景、新导航或 Agent 成功。完整验收还要检查新的真实 `/clock`、位姿、Nav2 状态、订单/交接回执、模型调用和真实停稳。

无需安装相同用户名或目录；所有工具接受任意 `--root`。中文路牌优先使用系统或项目本地 Noto CJK 字体，没有时使用英文 DejaVu 路牌，不影响业务；可以安装系统 `fonts-noto-cjk` 以显示中文。物理灯光增强是项目独立 session overlay，不保存或改写 NVIDIA 官方 root layer。

## 来源与验证边界

- [Docker 官方 ROS 2 镜像使用指南](https://docs.docker.com/guides/ros2/)：使用官方 ROS 镜像及 apt 扩展。
- [ROS Humble 安装说明](https://docs.ros.org/en/humble/Installation/Ubuntu-Install-Debs.html)：Ubuntu Jammy / Humble 的上游配置。该文档当前有反爬访问限制；构建以公开官方 ROS 镜像的签名 apt 配置为准。
- [Isaac Sim 6.0.1 安装](https://docs.isaacsim.omniverse.nvidia.com/6.0.1/installation/install_workstation.html)：按复现者设备满足官方硬件、驱动和许可要求。
- [Isaac Sim 6.0.1 医院导航](https://docs.isaacsim.omniverse.nvidia.com/6.0.1/ros2_tutorials/tutorial_ros2_multi_navigation.html)：官方医院场景和单机器人命名空间基础。

地图和资产完整性、纯 Python 几何检查、容器构建、真实仿真启动、真实导航、真实 Agnes 任务要分别报告。缺少硬件/账号或官方资产获取失败时记为未执行或阻塞，不用旧 PID、旧回执或脚本动画代替。
