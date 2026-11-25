#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ "$(uname -s)" != Darwin ]]; then
  echo 'This release requires macOS.'; exit 1
fi
if ! xcode-select -p >/dev/null 2>&1; then
  echo 'Install Xcode Command Line Tools with: xcode-select --install'; exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Install Python 3.10 or newer first"'
python3 -m venv .venv
.venv/bin/python -m pip --isolated install --index-url https://pypi.org/simple -r requirements.txt
echo 'Setup complete. Next open Configure Devices.command. No devices were opened.'
