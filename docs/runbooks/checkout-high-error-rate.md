# CheckoutHighErrorRate

Доля ответов 5xx на `GET /work` за 5 минут выше 1% и держится 2 минуты.

Healer делает `action=restart`: rollout restart Deployment `checkout` в namespace `demo`.

Запасное действие, если рестарт не помог: `action=rollback`, rollout undo того же Deployment.

Ручная проверка: ConfigMap `checkout`, ключ `ERROR`. Значение `1` включает 500. После проверки вернуть `0` и перезапустить Deployment, иначе алерт не погаснет.
