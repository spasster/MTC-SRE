#!/bin/sh
set -eu
for app in checkout payment redis; do
  kubectl rollout status deployment/"$app" -n demo --timeout=180s >/dev/null
done
mesh=mtls-mesh-$$
plain=mtls-plain-$$
cleanup() {
  kubectl delete pod "$mesh" -n demo --ignore-not-found --wait=false >/dev/null
  kubectl delete pod "$plain" -n obs --ignore-not-found --wait=false >/dev/null
}
trap cleanup EXIT HUP INT TERM
kubectl run "$mesh" -n demo --image=curlimages/curl:8.12.1 --restart=Never \
  --labels=app=mtls-check --overrides='{"spec":{"automountServiceAccountToken":false}}' \
  --command -- sleep 600 >/dev/null
kubectl run "$plain" -n obs --image=curlimages/curl:8.12.1 --restart=Never \
  --labels='app=mtls-check,sidecar.istio.io/inject=false' \
  --overrides='{"spec":{"automountServiceAccountToken":false}}' --command -- sleep 600 >/dev/null
kubectl wait --for=condition=Ready pod/"$mesh" -n demo --timeout=180s >/dev/null
kubectl wait --for=condition=Ready pod/"$plain" -n obs --timeout=180s >/dev/null
kubectl exec "$mesh" -n demo -c "$mesh" -- curl -fsS --max-time 15 http://checkout.demo.svc/work >/dev/null
if kubectl exec "$plain" -n obs -- curl -fsS --max-time 5 http://checkout.demo.svc/health >/dev/null 2>&1; then
  echo 'Ошибка: рабочий HTTP-порт доступен без mTLS.'
  exit 1
fi
kubectl exec "$plain" -n obs -- curl -fsS --max-time 5 http://checkout.demo.svc:8081/metrics >/dev/null
echo 'Проверено: запрос в mesh успешен, HTTP без mTLS отклонён, метрики доступны.'
