#!/usr/bin/env bash
set -euo pipefail
exec docker exec -i hospital-delivery-runtime python3 -m service.mcp_server
