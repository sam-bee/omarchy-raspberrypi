#!/bin/bash

set -euo pipefail
if (( $# != 0 )); then
  echo "Usage: $0" >&2
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "$script_dir/stage-user-session.sh" --rollback
