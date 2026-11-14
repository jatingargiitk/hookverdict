#!/bin/bash
# Refuse recursive deletes.
cmd=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["tool_input"].get("command",""))')
if [[ "$cmd" == *"rm -rf"* ]]; then
  echo "Blocked: no rm -rf in this repo" >&2
  exit 2
fi
exit 0
