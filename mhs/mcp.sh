#!/usr/bin/env bash
set -eo pipefail
exec /mnt/sda/omni-yezh/hospital-delivery-agent/agent/mhs-venv/bin/python -m mhs.mcp_server
