# CheckoutSlow

p99 `GET /work` за 5 минут выше 300 мс и держится 2 минуты.

Healer делает `action=scale`: Deployment `checkout` в namespace `demo` становится на 5 реплик.

Ручная проверка без Chaos Mesh: ConfigMap `checkout`, ключ `DELAY`, значение в секундах. После проверки вернуть `0` и перезапустить Deployment.
