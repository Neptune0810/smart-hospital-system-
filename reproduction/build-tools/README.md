# 智慧医院系统：从完整固定源码构建 AGH/MHS

这个目录只包含构建工具、精确源码补丁、版本锁和非秘密元数据。不会分发旧运行包、账号 home、API Key、原业务数据库或服务器登录信息。目标平台是 Ubuntu 22.04／Linux，Python 3.10 以上；完整仿真另需兼容 NVIDIA GPU、驱动和 Isaac Sim 6.0.1。

## 固定输入

- 上游：https://github.com/AgnesAI-Labs/agnes-harness.git
- 提交：`57d0c34b33527294042b7ab8a60d7c7c934e932e`
- Node.js：`v24.14.0`，pnpm：`10.34.5`，使用 `corepack pnpm`。
- 上游 `pnpm-lock.yaml` SHA256：`fc8dfe6d4946cca91dfe6af54ef093ab1c19e24b947f1f82240dc1c3d19db9ff`。
- 八个原始源码补丁保留智慧病区实际请求预算、通知激活兼容、打包 worker 兼容及嵌入会话只读检查。构建器再做两项有摘要的可迁移派生：Web只允许配置的workbench origin；智慧病区worker由自身打包入口目录推导项目ROOT，继续核验环境ROOT、profile、home和版本权威，取消旧服务器路径字面绑定。MHS 客户端只增加部署的 Hub 地址；原始与修改后 SHA 均见 `source-patch-provenance.json`。
- Python SDK 从同一完整 checkout 的 `packages/mhs/python` 以 editable 方式构建安装；固定 SDK 的 schema.py 从源码父目录读取消息 schema，不能把普通 wheel 独立复制替代。官方 `packages/mhs/schema/mhs-v1.json` SHA256 为 `592628fc4e8cadf99a6558266189ea65def23de59984c5753f1ae812c4e7564c`，构建时核验并记录。包版本、构建后端固定在 `requirements.lock.txt`。JPEG 首版不需要 PyAV。

本地的 `patches/web-app.ts` 和 `patches/mhs-client.json` 是用于核对的原始非秘密源码参考，构建以统一 diff 与完整固定上游为准。附带的官方归档由浅checkout生成，首次导入需精确注册本提交的shallow边界；构建器已执行gitinit→注册边界→fetch→强核验完整tree，不通过普通clone忽略父提交缺失。禁止把七个 TS 片段目录当作完整 AGH 源码，也不要执行历史 `mhs/build_runtime.py` 覆盖智慧病区运行包。

## 构建

安装 Node、Corepack、git、C 编译器、与 Node 相配的头文件和 Python venv 支持（Ubuntu推荐 `sudo apt install python3-venv`，由使用者决定系统安装）。不能改系统包时，`--venv-bootstrap` 会从固定版本锁全新安装项目内 virtualenv21.14.6；不会复制旧venv。默认stdlibvenv失败也仅保留本次新partial输出与日志，再使用该有限fallback。已存在的venv或bootstrap一律拒绝复用；需保留失败后换新目录或由操作者明确移入对应失败证据目录。设置你自己的解压位置，路径不含任何固定用户名或服务器地址；构建临时 Unix socket 有长度限制，建议较短位置。Linux Unix socket路径有107字节上限；本作品不宣称任意长路径可用。

```bash
PROJECT_ROOT="$PWD"
python3 reproduction/build-tools/build_runtime.py --root "$PROJECT_ROOT" --phase all
```

如包含 `vendor/agnes-harness.bundle`，优先从作品附带的完整官方 Git 源归档检出，并记录归档 SHA；该归档不含已编译 runtime 或账号。没有归档时使用官方 URL。默认检出到 `agent/agh-source-mhs`，构建完整运行包到 `agent/agh-runtime-smart-ward145`、MHS 插件、项目业务策略和统一工作台，并创建全新的 `agent/mhs-venv`。这些输出存在时拒绝覆盖。失败日志保留在 `runs/reproduction/build-*`，不自动删失败证据或切换旧 bundle。`fetch`、`patch`、`build`、`sdk` 可单独执行；`build`／`sdk` 需要已完整检出并应用本补丁的 checkout。

如预先从完整源归档检出固定上游且 checkout 未修改，调用 `--phase patch` 应用并核验补丁；随后调用 `--phase build` 与 `--phase sdk`。`--hub-url` 可配置 Hub WebSocket 地址；默认从 `portable/config.json` 读取 Hub 端口，无该配置时使用4192。`--workbench-port` 同理：只派生一个精确允许的父页面origin，并记录派生源码SHA；历史聊天只读检查保持。浏览器和SSH转发须使用该配置端口。

## 账号与插件

可以先创建无账号独立home，完成插件安装、业务服务与仿真验证；没有账号时真实模型/Agent闭环记为NA。每个复现者在运行真实Agent前使用自己的 Agnes 账号或 API Key。按系统使用指南打开 Web Provider 设置，在密码输入框配置账号、测试连接、选择实际模型并保存；或执行：

```bash
export AGH_HOME="$PROJECT_ROOT/agent/agh-home-mhs"
export AGNES_PROFILE=local-dev
node "$PROJECT_ROOT/agent/agh-runtime-smart-ward145/agnes.mjs" config
```

不要从本作品或原服务器复制凭据，不把 API Key 作为命令行参数或放在代码中。固定官方版本没有跨独立 home 引用另一个 home 模型账户的公开配置流程；没有正常配置账户时，真实 Agent 验收必须记为未执行，而不能使用模拟模型替代。

正常安装项目插件不要求先复制或配置模型账号；入口只会创建无provider的基础profile，并补充本实例 `configs/runtime-profile.json` 固定smart-ward-v1；已有不同模式时拒绝更改。MCP标准stdio子进程不继承HOSPITAL_MODE，因而该文件是保证业务MCP与策略闭合目录一致的实际配置依赖。全流程还包含 MCP stdio transport/catalog 测试，该测试要求新项目Python SDK和 `mhs/mcp.sh` 可运行；它验证本地服务通信与目录，不发送模型请求。若服务启动依赖业务API，先按主使用指南启动本项目业务服务并以实际日志为准。然后执行：

```bash
python3 reproduction/build-tools/install_plugins.py --root "$PROJECT_ROOT" --execute
```

安装前需完成主指南的portable prepare和合法本机 `portable/config.json`（包括Isaac位置）；installer通过该配置读取runtime_env，新SSH终端不必预先设置ISAAC_HOME。开始前核验新实例daemon PID、argv、cwd和home；如之前包操作启动的是本实例临时随机端口，通过官方daemon stop命令正常停止后，由项目manage以配置的固定端口启动并核验。不会停止或调整公共daemon。写好最终preset/profile后，仅正常重启本副本daemon一次使其激活；核验固定端口和新PID归属，独立读取实际MCP八工具catalog，并只统计新会话账本request/sent及inference intent数，不读取内容、账号秘密或调用模型。全部协议检查和零模型调用计数核验完成后，installer通过官方daemon stop正常关闭仅本副本安装用daemon/Hub，核验真实进程退出与两端口释放；保留enabled/trust/profile落盘状态。随后用主指南control start启动完整实例，不能放宽端口占用检查。所有阶段无论成功失败均保存report和原日志。该入口先 inspect，再 add，再针对本次预览的 integrity 与 capability hash trust，最后 enable；校验 MCP revision 并进行真实连接测试，保留日志。仅针对已审阅的两个项目源码包及精确项目MCP路径，在正常 `[y/N]` 提示时确认一次，不关闭账号审批。不要将插件协议测试等同于完整运输、灯光闭环通过。之后自行配置模型账号，测试模型连接与真实Agent请求可能产生provider开销。

固定上游文档依据：`docs/guide/install.md`、`docs/guide/quickstart.md`、`docs/guide/security.md` 和 `packages/mhs/python/pyproject.toml`。这些步骤的执行结果以新复现目录实际报告为准；脚本本身存在不代表冷启动已成功。
