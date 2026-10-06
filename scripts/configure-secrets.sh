#!/bin/sh
set -eu
umask 077
mkdir -p .runtime
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

# Общий токен остаётся в Kubernetes и не выводится в терминал.
for ns in obs heal; do
  kubectl create namespace "$ns" --dry-run=client -o yaml > "$tmp/namespace.yaml"
  kubectl apply -f "$tmp/namespace.yaml" >/dev/null
done
if kubectl get secret healer-webhook -n heal >/dev/null 2>&1; then
  kubectl get secret healer-webhook -n heal -o jsonpath='{.data.token}' > "$tmp/encoded"
  base64 -d "$tmp/encoded" > "$tmp/token"
else
  python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")' > "$tmp/token"
fi
for ns in obs heal; do
  kubectl create secret generic healer-webhook -n "$ns" --from-file=token="$tmp/token" \
    --dry-run=client -o yaml > "$tmp/secret.yaml"
  kubectl apply -f "$tmp/secret.yaml" >/dev/null
done

python3 - "$tmp" <<'PY'
import os
import sys
from pathlib import Path

import yaml

tmp = Path(sys.argv[1])
token, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
values = {}
if bool(token) != bool(chat):
    raise SystemExit("Для Telegram нужны оба параметра TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
if token and chat:
    try:
        chat_id = int(chat)
    except ValueError:
        raise SystemExit("TELEGRAM_CHAT_ID должен быть числом") from None
    (tmp / "telegram-token").write_text(token, encoding="utf-8")
    (tmp / "telegram-chat").write_text(chat, encoding="utf-8")
    base = yaml.safe_load(Path("observability/kube-prom-values.yaml").read_text(encoding="utf-8"))
    receivers = base["alertmanager"]["config"]["receivers"]
    receivers[-1]["telegram_configs"] = [{
        "bot_token_file": "/etc/alertmanager/secrets/telegram/token",
        "chat_id": chat_id,
        "send_resolved": True,
        "parse_mode": "HTML",
        "message": "{{ range .Alerts }}{{ .Status }}: {{ .Annotations.summary }}\n{{ .Annotations.description }}\n{{ end }}",
    }]
    values = {"alertmanager": {"alertmanagerSpec": {"secrets": ["healer-webhook", "telegram"]},
                               "config": {"receivers": receivers}}}
Path(".runtime/notification-values.yaml").write_text(
    yaml.safe_dump(values, allow_unicode=True), encoding="utf-8")
PY
if [ -s "$tmp/telegram-token" ]; then
  for ns in obs heal; do
    kubectl create secret generic telegram -n "$ns" \
      --from-file=token="$tmp/telegram-token" --from-file=chat_id="$tmp/telegram-chat" \
      --dry-run=client -o yaml > "$tmp/secret.yaml"
    kubectl apply -f "$tmp/secret.yaml" >/dev/null
  done
fi
printf '%s\n' 'Секрет webhook настроен; значения не выведены.'
