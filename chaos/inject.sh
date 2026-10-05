#!/bin/sh
set -eu

ns=demo

running_pod() {
  app=$1
  name=$(kubectl get pods -n "$ns" -l "app=$app" --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
  if [ -z "$name" ]; then
    echo "нет Running пода app=$app в $ns" >&2
    exit 1
  fi
  echo "$name"
}

apply_named() {
  file=$1
  app=$2
  name=$(running_pod "$app")
  sed "s/POD_NAME/$name/g" "$file" | kubectl apply -f -
  echo "$file -> $name"
}

usage() {
  echo "usage: sh chaos/inject.sh kill|slow-pod|spike|redis|bad-release|undo-release|partition|clear" >&2
  exit 1
}

cmd=${1:-}
case "$cmd" in
  kill)
    kubectl apply -f chaos/checkout-kill.yaml
    ;;
  slow-pod)
    apply_named chaos/checkout-slow-pod.yaml checkout
    ;;
  spike)
    kubectl delete job k6-spike -n "$ns" --ignore-not-found
    kubectl apply -f k8s/k6-spike.yaml
    ;;
  redis)
    apply_named chaos/redis-slow.yaml redis
    ;;
  bad-release)
    kubectl set env "deployment/checkout" -n "$ns" FAIL_RATE=1
    ;;
  undo-release)
    kubectl rollout undo "deployment/checkout" -n "$ns"
    ;;
  partition)
    kubectl apply -f chaos/partition-payment.yaml
    ;;
  clear)
    kubectl delete networkchaos,podchaos --all -n "$ns" --ignore-not-found
    kubectl delete job k6-spike -n "$ns" --ignore-not-found
    ;;
  *)
    usage
    ;;
esac
