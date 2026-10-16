# Команда

| Участник | Роль | Задачи |
|---|---|---|
| Нелин Данила | Технический руководитель | Архитектура, SLO, правила диагностики и допустимые действия, техническое ревью |
| Захаров Максим | Инженер сервисов и наблюдаемости | checkout и payment, метрики, журналы, OpenTelemetry, дашборды и промпты ассистента |
| Шестаков Данила | Инженер испытаний надежности | Chaos Mesh, k6, baseline/auto, ступени нагрузки и возврат масштаба |
| Митрюшкина Жанна | Ведущий DevOps/SRE-инженер | Kubernetes и CI, Prometheus, Grafana, Loki, Tempo, Alloy, mTLS, RBAC, healer |

Схема контура: [architecture.md](architecture.md). Сверка с текстом кейса: [requirements.md](requirements.md).

| Участник | Где смотреть его часть |
|---|---|
| Нелин Данила | [architecture.md](architecture.md), пороги в `healer/rules.yaml`, границы в [requirements.md](requirements.md) |
| Захаров Максим | `apps/checkout`, `apps/payment`, дашборды `observability/dashboards/`, промпты `healer/prompts/` |
| Шестаков Данила | `chaos/`, `k6/`, `scripts/experiment.py`, `scripts/capacity.py`, [demo.md](demo.md) |
| Митрюшкина Жанна | `k8s/`, `ansible/`, `healer/`, `.github/workflows/`, [runbooks](runbooks/) |
