# Third-party components

Agnes Harness and its MHS source are provided as the fixed official Git commit `57d0c34b33527294042b7ab8a60d7c7c934e932e` in `vendor/agnes-harness.bundle`. The upstream Apache-2.0 license is extracted verbatim from that commit under `third-party/agnes-harness/`. Build patches are separately identified in `reproduction/build-tools/source-patch-provenance.json`.

Isaac Sim, the hospital scene, robot, people, animation and texture assets must be obtained legally from NVIDIA under their applicable terms. This source stage includes acquisition URLs and hashes, not those runtime scene assets or Isaac Sim itself. The provided occupancy map is an input from the existing Isaac hospital navigation scenario; provenance and hashes are recorded in `reproduction/nav-assets/scene-inputs.lock.json`. This file does not grant rights beyond the original component terms.

ROS, Ubuntu, Nav2 and Python/Node dependencies are obtained from their identified upstream repositories and retain their respective licenses. The container build records exact resolved package versions.
