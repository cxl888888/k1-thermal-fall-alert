#!/usr/bin/env bash
set -euo pipefail
collection_tool_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
collection_python="${K1_CARE_PYTHON:-$HOME/venv-k1-care/bin/python}"
if [[ ! -x "$collection_python" ]]; then
  printf '%s\n' '未找到 venv-k1-care；可通过 K1_CARE_PYTHON 指定 Python。' >&2
  exit 1
fi
if [[ "$#" -eq 0 ]]; then
  exec "$collection_python" "$collection_tool_dir/open_k1_workbench.py"
fi
exec "$collection_python" -u "$collection_tool_dir/app.py" --capture --open "$@"
