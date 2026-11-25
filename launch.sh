#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo 'Run Setup.command first.'; exit 1
fi
case "${1:-}" in
  camera) exec .venv/bin/python camera_setup/server.py ;;
  calibration) exec .venv/bin/python calibration_check/server.py ;;
  dataset) exec .venv/bin/python dataset_collection/server.py ;;
  configure) exec .venv/bin/python configure.py ;;
  *) echo 'Choose camera, calibration, dataset, or configure.'; exit 1 ;;
esac
