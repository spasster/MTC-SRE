# Проверка метрики, трейса и логов одного инцидента

Проверять следует запрос после начала конкретной инъекции и до подтверждения восстановления. Сохранить времена инцидента, trace_id и код проверяемой версии. Здоровый запрос другого опыта не подтверждает наблюдаемость этого отказа.

1. Открыть Grafana через локальный port-forward и войти учетной записью собственного стенда. Пароль не включать в команду, URL или снимки.
2. Проверить дашборды SLO и RED. Указать выбранный интервал и окна расчета. Короткий опыт не доказывает месячный SLO.
3. В Explore выбрать Prometheus и запрос ниже. В Options включить Exemplars. Для короткого интервала выбрать Range и шаг 1s, затем выполнить запрос.
4. Открыть маркер exemplar и ссылку «Трейс». Сверить trace_id и время с инцидентом. В Tempo должны быть span checkout GET /work, Redis, вызов POST /pay и span payment.
5. Открыть span checkout, затем Related logs. Проверить подставленный trace_id и строки обоих сервисов, а не только число HTTP 200.

```promql
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{namespace="demo",job="checkout",path="/work"}[1m])))
```

```logql
{namespace="demo"} | json | trace_id="<trace_id из exemplar>"
```

Окно логов вокруг span расширено на 2 секунды в обе стороны, поэтому в Loki попадают строки и checkout, и payment с тем же trace_id. Поля перехода описаны в [документации Grafana](https://grafana.com/docs/grafana/latest/datasources/tempo/configure-tempo-data-source/configure-trace-to-logs/).

Для MTTR используются три последовательных измерения перекрывающихся минутных окон с интервалом около 5 секунд. Они не означают три независимые здоровые минуты. Момент приема API, подтверждение SLO healer и внешний конец MTTR следует различать.
