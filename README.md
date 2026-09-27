# MTC SRE

Учебный контур: метрики checkout, алерты, Telegram и автоматическое лечение. Мишень — сервис `checkout`. Его ломаем задержкой сети или флагом `ERROR`, Prometheus поднимает алерт, healer меняет Deployment.

SLO: 99.9% успешных `GET /work` и p99 быстрее 300 мс.

## Сценарии

1. Задержка сети у checkout → алерт `CheckoutSlow`, `action=scale` (5 реплик). Прогнан.
2. Много 5xx → `CheckoutHighErrorRate`, `action=restart`. Правило и healer есть. Запасное действие `rollback`.
3. Убит Redis → `RedisDown`, `action=fix-redis`. Ещё не сделано. YAML убийства пода уже лежит в `chaos/redis-kill.yaml`.

## Где что лежит

| Путь | Зачем |
|---|---|
| `apps/checkout` | Мишень. FastAPI: `/health`, `/work` (ходит в Redis), `/metrics`. |
| `healer` | Принимает вебхук Alertmanager и чинит Deployment checkout. |
| `k8s/` | Namespace `demo`, Redis, checkout, ServiceMonitor, healer и его RBAC. |
| `observability/rules.yaml` | PrometheusRule: `CheckoutHighErrorRate` и `CheckoutSlow`. |
| `observability/dashboards/` | Четыре дашборда Grafana. |
| `observability/kube-prom-values.yaml` | Значения Helm для Prometheus, Grafana и Alertmanager. Без секрета. |
| `observability/telegram.local.yaml` | Токен бота и id чата. В git не входит. |
| `chaos/checkout-delay.yaml` | NetworkChaos: 2 секунды задержки на исходящий трафик checkout, 10 минут. |
| `chaos/redis-kill.yaml` | PodChaos: убить Redis. Не применять, пока нет Ansible. |
| `docs/runbooks/` | Что делать по каждому алерту. |

## Как это крутится

Один ноутбук. Colima даёт Docker, k3d внутри Docker поднимает кластер `mtc` (одна нода).

| Namespace | Что внутри |
|---|---|
| `demo` | Два пода checkout и один Redis. |
| `obs` | Prometheus, Grafana, Alertmanager. |
| `heal` | Healer. |
| `chaos-mesh` | Chaos Mesh. |

Checkout ходит в Redis. Флаги лежат в ConfigMap `checkout` и читаются только при старте пода. После правки нужен `kubectl rollout restart deployment/checkout -n demo`.

- `ERROR=1` — `/work` отвечает 500.
- `DELAY` — сон в секундах перед ответом.
- `DEGRADED_MODE=1` — ответ без Redis.
- `REDIS_SOCKET_TIMEOUT` — таймаут Redis, по умолчанию 3 секунды. Так задержка сети успевает попасть в гистограмму, а не оборваться ошибкой.

`/health` всегда 200, чтобы Kubernetes не перезапускал под из-за мёртвого Redis.

Метрики: `http_requests_total` (method, path, status) и `http_request_duration_seconds`. Лог — одна JSON-строка в stdout с `trace_id`.

Healer на `POST /webhook`:

- игнорирует всё, кроме `firing`;
- берёт только `CheckoutHighErrorRate` и `CheckoutSlow`;
- один и тот же алерт не чаще раза в 5 минут;
- `scale` ставит 5 реплик, `restart` делает rollout restart, `rollback` откатывает на предыдущую ревизию.

Права healer — только Deployment и ReplicaSet в `demo`.

Alertmanager шлёт алерты с `service=checkout` в Telegram и на `http://healer.heal.svc:8080/webhook`.

## Поднять с нуля

```bash
brew install kubectl k3d helm
colima start --cpu 4 --memory 8 --disk 40
docker context use colima
k3d cluster create mtc --agents 0
```

Образы и мишень:

```bash
docker build -t checkout:dev apps/checkout
docker build -t healer:dev healer
k3d image import checkout:dev healer:dev -c mtc
kubectl apply -f k8s/namespace.yaml -f k8s/redis.yaml -f k8s/checkout.yaml
```

Наблюдаемость. Файл с токеном должен уже лежать локально, в репозитории его нет.

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install kube-prom prometheus-community/kube-prometheus-stack \
  -n obs --create-namespace \
  -f observability/kube-prom-values.yaml \
  -f observability/telegram.local.yaml
kubectl apply -f k8s/checkout-servicemonitor.yaml -f observability/rules.yaml -f k8s/healer.yaml
```

Дашборды:

```bash
kubectl create configmap mtc-dashboards -n obs \
  --from-file=observability/dashboards/ \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl label configmap mtc-dashboards -n obs grafana_dashboard=1 --overwrite
```

Grafana: `kubectl port-forward -n obs svc/kube-prom-grafana 3000:80`, логин `admin` / `admin`. В поиске: **Checkout SLO**, **Checkout RED**, **Обзор сервисов**, **Инцидент**. На защите открывать **Checkout SLO**.

Chaos Mesh:

```bash
helm repo add chaos-mesh https://charts.chaos-mesh.org
helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh -n chaos-mesh --create-namespace \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/k3s/containerd/containerd.sock \
  --set dashboard.securityMode=false
```

## Прогнать задержку

Нужен живой трафик на `/work`, иначе p99 не из чего считать. Алерт становится firing через 2 минуты после того, как p99 за 5 минут выше 300 мс.

```bash
kubectl apply -f chaos/checkout-delay.yaml
kubectl port-forward -n demo svc/checkout 8080:80
# в другом окне, пару минут
while true; do curl -s -o /dev/null http://127.0.0.1:8080/work; done
```

Ожидание: Telegram, в логе healer строка `CheckoutSlow` / `scale`, у Deployment checkout 5 реплик.

```bash
kubectl logs -n heal deploy/healer
kubectl get deploy checkout -n demo
kubectl delete -f chaos/checkout-delay.yaml
kubectl scale deployment/checkout -n demo --replicas=2
```

Задержка вешается на исходящий трафик пода, а не на IP Redis. Иначе пакеты к Service ClusterIP мимо правила.

## Секреты

`observability/telegram.local.yaml` в `.gitignore`. В `kube-prom-values.yaml` стоят заглушки `PASTE_BOT_TOKEN` и `chat_id: 0`. Не коммить файл с настоящим токеном. В репозитории нет заполненного примера: скопируй блок `receivers` из `kube-prom-values.yaml` в `telegram.local.yaml` и подставь токен и id чата.

## Чего ещё нет

Трейсы и Loki, алерт `RedisDown` и Ansible `fix-redis`, GitHub Actions, React-пульт. Storefront и inventory не нужны для лечения.
