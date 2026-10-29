# Deployment guide

This guide sets up an independent instance on your own Linux GPU host. You need your own Isaac Sim installation, official assets and Agnes account; none are included in the repository. Commands below run from the project root unless stated otherwise.

## Requirements

- Ubuntu 22.04 x86-64, a compatible NVIDIA GPU and driver, and Isaac Sim 6.0.1 installed through NVIDIA's official channels.
- Docker with a working daemon, Python 3.10, pip, git, C/C++ build tools and Node-compatible headers.
- Node.js 24.14.0 and Corepack with pnpm 10.34.5.
- Access to the official hospital, Nova Carter and character assets, or a compatible local asset library. First startup can require network access to referenced assets.
- Noto CJK or DejaVu fonts for scene signs. Noto CJK is needed for Chinese labels; DejaVu permits English labels. `ffmpeg` is required only for the optional navigation recording check.
- Your own Agnes provider credentials for a real agent run. Do not commit keys, copy another installation's account directory or paste a key into a terminal command.

Choose a short directory such as `~/smart-hospital`. The pinned Agnes version has limits on Unix socket paths: the project daemon path must be at most 107 UTF-8 bytes and `agent/agh-home-mhs/data/daemon/workers.sock` at most 103 bytes, including the full project path. A longer path can cause the daemon to choose a fallback socket that the workbench cannot use. A directory with spaces is supported, but check the byte length when using long or non-ASCII paths.

## 1. Prepare the project directory

Clone the repository or extract the source package into your chosen directory, then set the path to **your** Isaac Sim installation:

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

Replace the example Isaac path. The repository root should contain `portable/`, `reproduction/`, `mhs/`, `configs/` and `maps/`.

## 2. Configure the instance

```bash
if [ ! -f portable/config.json ]; then
  python3 portable/control.py init
fi
# Review portable/config.json and change settings before prepare if needed.
python3 portable/control.py prepare
```

Defaults are API `4288`, chat `4289`, Hub `4292`, workbench `4294`, daemon WebSocket `4295`, ROS domain `74` and GPU index `0`. The navigation image defaults to `smart-hospital:humble-repro-v1`. If ports or the ROS domain are in use, select distinct values in the configuration before preparation. Services bind to loopback by default.

Changing ports, domain, instance ID or image after installation requires a clean source copy and a fresh prepare/build. Isaac and asset paths and GPU index can be configured for your host. An installed Python virtual environment contains paths from its build machine; rebuild it from clean source when moving the project.

## 3. Build Agnes Harness, MHS and the SDK

```bash
python3 reproduction/build-tools/build_runtime.py \
  --root "$PROJECT_ROOT" --phase all
```

The builder verifies the pinned upstream source and applies the project patch. It builds the runtime, plugins and workbench and creates a new project virtual environment. If `python3-venv` is unavailable, use the following **instead of** the command above on a clean source copy:

```bash
python3 reproduction/build-tools/build_runtime.py \
  --root "$PROJECT_ROOT" --phase all --venv-bootstrap
```

Do not run both commands over the same output. If a build fails, keep the error report and partial output; the builder provides `fetch`, `patch`, `build` and `sdk` phases for recovery after identifying the failed phase.

## 4. Build the navigation container

```bash
export HOSPITAL_IMAGE="$(python3 -c 'import json; print(json.load(open("portable/config.json"))["image"])')"
python3 reproduction/nav-assets/build_nav.py \
  --image "$HOSPITAL_IMAGE" \
  --base "ros@sha256:6b4abb5340e430c632bcf4710fb34996df4f80e502993e5755f8fc59a39c7b2e" \
  --output "runs/reproduction/nav-build-$RUN_TAG"
```

The output directory must be new. The build uses the pinned public ROS Humble base and locked dependencies; it reports unavailable dependencies instead of silently changing versions. For a public ROS archive alternative, see [the navigation asset guide](../reproduction/nav-assets/README.md).

## 5. Obtain and check assets

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

The character asset tool checks all 194 files against the lock. Omit `--fetch` to check an existing cache. Checking the hospital root USD alone does not prove that every referenced robot, material and texture is available offline. If you use a local official asset library, set `assets_root` in `portable/config.json` or use `HOSPITAL_ASSETS_ROOT` as supported by the project; preserve the standard `Isaac/...` structure.

## 6. Install the project plugins

```bash
install -d -m 700 "$PROJECT_ROOT/agent/agh-home-mhs"
python3 reproduction/build-tools/install_plugins.py \
  --root "$PROJECT_ROOT" --execute
```

If the directory already exists, first confirm it belongs to this instance. The installer creates an independent profile, installs and enables the project plugins, and checks the tool catalog. No model account is needed at this stage. A successful catalog check does not establish that an agent task or the full MHS protocol test suite has passed.

## 7. Check and start

```bash
python3 portable/control.py doctor
export STARTED_WALL="$(python3 -c 'import time; print(time.time())')"
python3 portable/control.py start
python3 portable/control.py status
```

The first start may take time while assets and the navigation mesh load. Check fresh robot pose and device samples, Nav2 readiness and actual Hub registration. A running PID or HTTP response alone does not prove the robot or agent is ready. `doctor` checks prerequisites; it does not itself run a model task.

## 8. Open and check the workbench

On the host running the project, open `http://127.0.0.1:4294/`. Check the map, camera views and fresh device feedback. Keep watch mode off until you are ready to run an agent task. Save the generated scene and status records:

```bash
python3 reproduction/nav-assets/check_scene.py \
  --root "$PROJECT_ROOT" --generated --since "$STARTED_WALL" \
  --output "runs/reproduction/generated-scene-$RUN_TAG.json"
python3 portable/control.py status \
  > "runs/reproduction/runtime-status-$RUN_TAG.json"
```

An optional bounded navigation check can be run with no orders, watch mode off and the robot confirmed stopped:

```bash
agent/mhs-venv/bin/python reproduction/validate_runtime.py \
  --root "$PROJECT_ROOT" \
  --output "$PROJECT_ROOT/runs/reproduction/runtime-$RUN_TAG"
```

This is a navigation and device check, not an Agnes route-planning test. Retain failures and unexecuted checks as such; do not substitute results from a different run.

## Remote access

For a remote host, run this template on your access computer, replacing `USER`, `HOST`, `PORT` and `KEY` with your own SSH details. Keep the terminal open and visit `http://127.0.0.1:4294/` in your browser. If you changed the project ports, change the corresponding forwards and URL too.

```bash
ssh -N -p PORT -i "KEY" \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -L 127.0.0.1:4294:127.0.0.1:4294 \
  -L 127.0.0.1:4289:127.0.0.1:4289 \
  -L 127.0.0.1:4292:127.0.0.1:4292 \
  -L 127.0.0.1:4295:127.0.0.1:4295 \
  USER@HOST
```

## Run a real agent task

Use the project chat page's Provider settings to enter your own credentials, test the connection, choose the model and save. Then start a new run, enable watch, place a specimen at B and dim room 301. Check the current run's model calls, device feedback, navigation, matching specimen receipt and final verification. Simulated handling is a scripted handoff; lighting feedback is a simulated relative index, not a physical lux reading.

## Stop and reset

```bash
python3 portable/control.py status
python3 portable/control.py stop
# To save the current task and create a new run:
python3 portable/control.py reset
```

The project lifecycle commands manage the instance's own recorded processes and container. The workbench's stop button stops the current task and watch; the command above also stops the instance services. Closing a browser does not stop background execution. If a handoff or stop cannot be confirmed, preserve its state and investigate before continuing.

## Scope of verification

The October 14 reproduction on a separate directory of the same GPU server reached the simulator and device checks, but a bounded navigation attempt timed out and its real-agent test was not run. A later short-path instance on that server completed one real Agnes specimen-and-lighting scenario; it is not a different-machine reproduction or an exhaustive test. Preserve both results, including failures, rather than treating a working installation as proof of every scenario. See [VERIFICATION.json](../VERIFICATION.json), the [project README](../README.md) and the [Chinese deployment guide](DEPLOYMENT.md) for the recorded scope and details.
