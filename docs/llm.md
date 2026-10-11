# Локальный SRE-ассистент

LLM не лечит кластер. Диагноз и действия выполняет healer по правилам. Ассистент только читает уже собранные факты и готовит текст для человека.

Два режима:

- `POST /report` разбирает текущие метрики и выбирает одну следующую проверку.
- `POST /postmortem` готовит черновик отчёта по последнему закрытому инциденту из ConfigMap `healer-state`.

Симптомы, список действий и MTTR считает код. Модель не может их переписать: схема ответа это отклоняет. Следующую проверку она выбирает из заранее составленных вариантов по этому инциденту. В ответе `actions_applied` пустой, `human_review_required` равен true.

Ассистент — отдельный под `sre-assistant` в namespace `heal`, порт 8082. Манифест: `k8s/assistant.yaml`. Запрос к `/report` и `/postmortem` требует тот же Bearer-токен, что и webhook healer. Модель слушает `http://ollama.assist.svc:11434` в namespace `assist`.

Ассистент работает от отдельной учётки. Ей разрешено читать Deployment в `demo` и ConfigMap журнала. Менять Deployment, удалять поды, писать в журнал и читать Secret нельзя. В модель не передаются токены, id операторов и содержимое корзины. Место ассистента в общей схеме: [architecture.md](architecture.md).

Промпты лежат в `healer/prompts/incident.md` и `healer/prompts/postmortem.md`. Модель — Qwen2.5 1.5B через Ollama 0.35.1, только на CPU. Для обычного `make up-linux` она не нужна.

```sh
make llm
python3 scripts/verify-llm.py --kind report --cluster mtc --output .runtime/llm.json
python3 scripts/verify-llm.py --kind postmortem --cluster mtc --output .runtime/postmortem.json
python3 scripts/verify-llm-quality.py --cluster mtc --output .runtime/llm-quality.json
```

Набор из трёх инцидентов для повторной генерации лежит в `scripts/llm-cases.json`. Ответ ассистента никуда не сохраняется сам: его возвращает HTTP-запрос.
