#!/bin/sh
set -eu
expected=k3d-${1:-mtc}
actual=$(kubectl config current-context 2>/dev/null || true)
if [ "$actual" != "$expected" ]; then
  printf '%s\n' "Ожидался контекст $expected, выбран $actual. Команда не выполнена." >&2
  exit 1
fi
