#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
cfg=$(colima ssh-config)
host=$(printf '%s\n' "$cfg" | awk '/^[[:space:]]*Hostname /{print $2}')
port=$(printf '%s\n' "$cfg" | awk '/^[[:space:]]*Port /{print $2}')
user=$(printf '%s\n' "$cfg" | awk '/^[[:space:]]*User /{print $2}')
key=$(printf '%s\n' "$cfg" | awk '/^[[:space:]]*IdentityFile /{print $2}' | tr -d '"')
mkdir -p "$root/inventory"
cat > "$root/inventory/colima.ini" <<EOF
[colima]
colima ansible_host=${host} ansible_port=${port} ansible_user=${user} ansible_ssh_private_key_file=${key} ansible_python_interpreter=/usr/bin/python3
EOF
echo "Подготовлен $root/inventory/colima.ini"
