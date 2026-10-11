# Сверка с ТЗ кейса 2

Кейс 1 в эту сдачу не входит. Ниже соответствие пунктов кейса 2 и того, где это лежит в репозитории. Краткое описание контура — в [architecture.md](architecture.md).

| Требование кейса 2 | Где лежит | Граница |
|---|---|---|
| Prometheus, Grafana, Alertmanager и централизованные логи | `observability/`, Alloy вместо filebeat/fluentd | Журналы подов и Ubuntu собирает Alloy: [linux-logs.md](linux-logs.md) |
| SLI/SLO: доступность, p95 и ошибки | Blackbox, счётчики GET /work, дашборд SLO, [slo-budget.md](runbooks/slo-budget.md) | Короткие окна измерены. Месячный SLO не доказан: истории стенда меньше месяца |
| rsyslog/auditd через Ansible | `ansible/roles/node-logs`, `ansible/roles/sre-stack` | Журналы Ubuntu доставляются в Loki |
| Severity, дедупликация, маршруты, эскалация и runbook | Alertmanager в `observability/kube-prom-values.yaml`, [runbooks](runbooks/) | Повтор одного алерта не обходит паузу и лимит действий |
| Убийство пода, разрыв сети и нагрузка CPU | `chaos/`, Istio, резервный payment, `k6/` | Короткий сценарий не включает полный разрыв всех путей. Область: [chaos-limited.md](runbooks/chaos-limited.md) |
| Webhook, restart/scale/rollback, dry-run и подтверждение | `healer/`, Lease и ConfigMap в `k8s/healer.yaml`, [telegram.md](telegram.md) | Неизвестный результат API не считается успехом |
| mTLS и связка метрика, трейс, лог | [mtls.md](mtls.md), Tempo, Loki, [incident-chain.md](runbooks/incident-chain.md) | Связка проверяется на запросе конкретной поломки |
| Синтетический SLI, k6 и мощность | Blackbox, `scripts/capacity.py`, `k6/` | Точный промышленный максимум не заявляется |
| LLM и описанные промпты | [llm.md](llm.md), `healer/prompts/`, `k8s/ollama.yaml` | Модель выбирает из готовых вариантов и ничего не применяет |
| Демонстрация «детект, затем восстановление» | [Видео 4:12](demo.md) | В кадрах нет токенов и личных чатов. Цифры видео и прогона CI разные |
| Репозиторий с IaC | Makefile, `k8s/`, Helm values, Ansible | Роль sre-stack ставит тот же контур, что и make |
| README | Проблема, цель, команда, запуск, архитектура, версии | Роли: [team.md](team.md). Схема: [architecture.md](architecture.md). Личный опыт участников не заполнен |

Полный стек на Ubuntu: `cd ansible && ansible-playbook -i localhost, playbooks/stack.yaml -e sre_cluster=mtc`. Обычный запуск: `make up-linux`.
