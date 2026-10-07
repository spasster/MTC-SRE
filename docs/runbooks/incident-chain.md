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

В [браузерной проверке CPU-инцидента](../results/iteration-4/grafana-browser.json) подтверждены [exemplar](../results/iteration-4/grafana-exemplar-browser.jpg) и [трейс](../results/iteration-4/grafana-trace-browser.jpg). Запрос 166dcbd876005501ab900de21fa91559 выполнен 7 октября 2026 в 07:39:28 по Москве; длительность span checkout 301.43 мс.

Исходная ссылка дала [только payment](../results/iteration-4/grafana-logs-narrow-browser.jpg): правая граница 07:39:28.455, а журнал checkout поступил в 07:39:28.470943768. Одно ручное расширение времени показало [обе строки](../results/iteration-4/grafana-logs-expanded-browser.jpg). Это временный способ диагностики, а не успешная приемка автоматической ссылки. Исправление запаса окна Tempo выделено в [следующую цель](../iteration-6.md); фильтрация по точному trace_id сохраняется. Причина и рекомендуемые поля описаны в [документации Grafana](https://grafana.com/docs/grafana/latest/datasources/tempo/configure-tempo-data-source/configure-trace-to-logs/).

Для MTTR используются три последовательных измерения перекрывающихся минутных окон с интервалом около 5 секунд. Они не означают три независимые здоровые минуты. Момент приема API, подтверждение SLO healer и внешний конец MTTR следует различать.
