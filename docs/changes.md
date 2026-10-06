# Что исправлено и проверено

Ветка fix/sre-case-completion подготовлена от 06b7496. Первые пять дефектов исправлены до первого аудита, каждый отдельным коммитом. После аудита закрыты дополнительные ошибки и требования кейса; повторный аудит оформлен отдельно.

## Первые пять исправлений

| Было | Стало | Коммит |
|---|---|---|
| Payment ServiceMonitor применялся до CRD на чистом кластере | Стек поднимается последовательно; монитор применяется после Established CRD | bfe6ea9 |
| Старая кнопка могла подтвердить новый инцидент | ID запроса, чат, срок 5 минут и актуальная ревизия проверяются повторно | 0120f36 |
| Ошибка или пропуск Kubernetes выглядели как успешное действие | Разделены применённое, пропущенное и неудачное действие | f8a271a |
| Отсутствующие и некорректные метрики считались здоровьем | Нет достоверных запросов, ошибок и задержки - нет закрытия и уменьшения реплик | 6fe44e9 |
| MTTR заканчивался на принятии команды; worker зависел от Telegram | Отдельно измеряются API и восстановление SLO, проверяются rollout и синтетика без Telegram | 2e89a2d |

## Дополнительные исправления и требования

- Ошибки payment, ошибки checkout и сетевые таймауты различаются. Необоснованного отката checkout из-за HTTP 500 payment нет.
- Опрос Telegram переживает ошибки без вывода токена. Подтверждённое действие повторно проверяет причину, Deployment и resourceVersion.
- Webhook закрыт токеном; Grafana получила случайный пароль, секреты сохраняются только локально и в Kubernetes.
- Без данных запросов, синтетики или CPU нет необоснованного автоматического изменения. Разные алерты используют одно окно ожидания; запоздавший firing игнорируется при здоровом SLO.
- Уменьшается только собственное масштабирование до согласованного значения. Новое число реплик оператора проверяется перед записью, ошибочный scale-down повторяется.
- Версии закреплены, Linux make запускается по умолчанию, порядок выдерживается при make -j; мутации проверяют контекст Kubernetes.
- Ansible настраивает rsyslog и auditd, сохраняя чужие правила. Alloy заменяет Promtail и собирает логи Kubernetes и Ubuntu.
- Istio требует mTLS на рабочих портах; отдельный порт метрик остаётся доступным Prometheus. Blackbox проходит всю цепочку checkout -> Redis -> payment.
- Проверен реальный exemplar -> Tempo -> Loki с одним trace_id. Локальный LLM запущен отдельно с RBAC только на чтение; вычисляемая оценка SLO отделена от ошибочного текста модели.
- Добавлены одинаковые baseline/auto эксперименты, клиентский итог k6, графики мощности и неуспешной перегрузки, проверки отказов зависимости и видео реального восстановления.
- README, runbook, план, два аудита, результаты и описание изменений приведены к текущему поведению.

## Проверки и ограничения

157 тестов прошли: healer 139, checkout 12, payment 6. На отдельном стенде проверены mTLS, метрика -> трейс -> лог, под, сеть, ошибки релиза, Redis, CPU и локальный LLM. Чистый Ubuntu CI подтвердил установку и реальные события syslog/audit в Loki.

В одном парном прогоне MTTR медленного пода уменьшился с 147.9 до 119.7 с. При 6 запросах/с scale 2 -> 5 восстановил SLO за 148.4 с. При 19 запросах/с пять реплик не удержали SLO; этот результат сохранён. Видео длится 148.7 с.

Имена и роли команды ожидают данных владельца. Внешний Telegram не проверялся реальными сообщениями. Месячный SLO, HA и точность модели не подтверждены; подробные границы находятся в audit.md.

## Коммиты по отдельным пунктам

| Коммит | Изменение |
|---|---|
| [5e7aaf7](https://github.com/spasster/MTC-SRE/commit/5e7aaf7ba66bf843f90f8e22327f2b32ce8fedac) | Подготовлен план завершения SRE-платформы |
| [bfe6ea9](https://github.com/spasster/MTC-SRE/commit/bfe6ea9ea47a29d5558865773a90b9bafb969911) | Исправлен порядок установки приложений и CRD мониторинга |
| [0120f36](https://github.com/spasster/MTC-SRE/commit/0120f36279a02bc339a9c6e65ac572640288a207) | Подтверждения Telegram привязаны к запросу и сроку действия |
| [f8a271a](https://github.com/spasster/MTC-SRE/commit/f8a271a03ed1563e0a0ac17fd83645e3e162551f) | Ошибки и пропуски действий Kubernetes отделены от успешного применения |
| [6fe44e9](https://github.com/spasster/MTC-SRE/commit/6fe44e909bda0604814016a9214e12091fec7ad6) | Восстановление запрещено при отсутствующих и некорректных метриках |
| [2e89a2d](https://github.com/spasster/MTC-SRE/commit/2e89a2d8042af9b5bf77f1e407564a1a71ef31e9) | MTTR завершается после проверки метрик и готовности Deployment |
| [3a82621](https://github.com/spasster/MTC-SRE/commit/3a8262158ecc047883627e170b59795547061a58) | Зафиксирован аудит после пяти приоритетных исправлений |
| [6d7dbb6](https://github.com/spasster/MTC-SRE/commit/6d7dbb613d495ba58dedea0768ccde5dae64d253) | Ошибки payment отделены от неисправного релиза checkout |
| [a70b76a](https://github.com/spasster/MTC-SRE/commit/a70b76a5edfab68e76569649b292ffaf748ef434) | Опрос Telegram сохраняется после ошибок без раскрытия токена |
| [c5e1040](https://github.com/spasster/MTC-SRE/commit/c5e1040acd94e1a85893c3de996c68588d0aa384) | Подтверждённое действие проверяет метрики и ревизию перед применением |
| [e77c72f](https://github.com/spasster/MTC-SRE/commit/e77c72fd3c6fbd7c705a0abfb34ed357791efb98) | Webhook восстановления закрыт токеном Alertmanager |
| [a644970](https://github.com/spasster/MTC-SRE/commit/a6449700544df1938d1b7140cb4b03dd286df9c2) | Завершён сбор системных журналов Ubuntu через Ansible |
| [78d2172](https://github.com/spasster/MTC-SRE/commit/78d2172a1d29bb5765217b3a454164c471d18b25) | Закреплены версии инфраструктуры и закрыт стандартный пароль Grafana |
| [42a3a13](https://github.com/spasster/MTC-SRE/commit/42a3a131480a1da5311682c1920a934a02cc06da) | Добавлен и проверен строгий mTLS между сервисами demo |
| [9e60131](https://github.com/spasster/MTC-SRE/commit/9e6013189999d4f40e0791e476364777b737edad) | Добавлена синтетическая проверка рабочей цепочки checkout |
| [57dfb7c](https://github.com/spasster/MTC-SRE/commit/57dfb7cb00ef59bd6d16d1109f55c123a211f3b4) | Promtail заменён поддерживаемым сборщиком Alloy |
| [1f46431](https://github.com/spasster/MTC-SRE/commit/1f4643155d5d691465bee6b92fa874d138f664c4) | Автоматические действия запрещены без достоверных метрик запросов |
| [63bf753](https://github.com/spasster/MTC-SRE/commit/63bf7537fc0317ee1e23a612b992cdfac377beaa) | Разные алерты используют общее окно ожидания после действия |
| [b3e355b](https://github.com/spasster/MTC-SRE/commit/b3e355bb34f78e22ab8a97a0f1084b5b06f071a6) | Добавлен локальный LLM-ассистент с доступом только на чтение |
| [d1519be](https://github.com/spasster/MTC-SRE/commit/d1519bede6db6f437d806666cd1f1db490ef1003) | Сохранены тайминги завершённых инцидентов для проверки MTTR |
| [e1d86e0](https://github.com/spasster/MTC-SRE/commit/e1d86e098844c0c422bb67ee7fdeb263e4a0ac87) | Healer уменьшает только собственное масштабирование и повторяет неудачный запрос |
| [edefe65](https://github.com/spasster/MTC-SRE/commit/edefe653a46cf09d258a406aef909c3101e4f094) | Установка проверяет контекст Kubernetes и готовность полного стека |
| [480875f](https://github.com/spasster/MTC-SRE/commit/480875f96bde6336023216381e18a871a253a662) | Добавлена проверка полного контура и системных журналов на Ubuntu |
| [3a2a0fd](https://github.com/spasster/MTC-SRE/commit/3a2a0fdc97f7c7c0d292828070502c36c81eaadb) | Сокращено ожидание новых тревог между циклами проверки |
| [ad56ba1](https://github.com/spasster/MTC-SRE/commit/ad56ba109695a0fad479f90580d67ec2ca62b833) | Запоздавшая тревога не начинает инцидент при здоровом SLO |
| [0b3b3ba](https://github.com/spasster/MTC-SRE/commit/0b3b3ba4fa5103beb45ec1b5fef4f8ae297e5166) | Добавлены одинаковые эксперименты для сравнения MTTR до и после автоматизации |
| [bcd6c81](https://github.com/spasster/MTC-SRE/commit/bcd6c81f4735a4744a4be9da2c6501a549ed4654) | Добавлена проверка реального перехода от метрики к трейсу и логам |
| [992df23](https://github.com/spasster/MTC-SRE/commit/992df23f734ed67fced7ea3b66e67e5f99457a9b) | Уточнены русские сообщения, комментарии и описания метрик |
| [4fca04d](https://github.com/spasster/MTC-SRE/commit/4fca04dd0773a14a72721e606b2a7cf00ea6d042) | Обновлены инструкции дежурного по итоговому поведению healer |
| [1cec3c0](https://github.com/spasster/MTC-SRE/commit/1cec3c0e0aed6d4ed05651cd2122dbf4f9d6670a) | Добавлен экран демонстрации с реальными данными стенда |
| [08cdf5a](https://github.com/spasster/MTC-SRE/commit/08cdf5a1c97d3e1f20c2147762f4a37981566659) | Проверка чистого Ubuntu запускается из рабочей ветки до слияния |
| [f69d21e](https://github.com/spasster/MTC-SRE/commit/f69d21eec4041f934dfc7364f261687256f7a900) | Проверенная оценка SLO отделена от ошибочного текста локальной модели |
| [a7336e5](https://github.com/spasster/MTC-SRE/commit/a7336e5afca117204a137cd2ad25ad50fd9ba48e) | Отсутствующие метрики CPU не разрешают удаление пода |
| [f020df6](https://github.com/spasster/MTC-SRE/commit/f020df6ee292a9670efc6467cee433d757534e27) | Масштабирование проверяет число реплик и не затирает изменения оператора |
| [44d5ced](https://github.com/spasster/MTC-SRE/commit/44d5cedabc4b320fd4abdec0c4fedf4770844206) | Зафиксированы реальные MTTR, графики нагрузки и предел мощности стенда |
| [ffe4a5a](https://github.com/spasster/MTC-SRE/commit/ffe4a5af01317a65aac7cdb8a72f7bc7101adf3e) | Подготовлена русская видео-демонстрация автоматического восстановления |
| [ba88e0d](https://github.com/spasster/MTC-SRE/commit/ba88e0dfdb0fae62211374d7f60297ecf452b244) | Проверены локальный LLM и запрет изменения кластера |
| [fc0c8fc](https://github.com/spasster/MTC-SRE/commit/fc0c8fc435a4a19a0b18941b9c7b419cbeccf5d6) | Сохранены результаты чистой установки и доставки Linux-журналов из CI |
| [bc78282](https://github.com/spasster/MTC-SRE/commit/bc78282e111d69d706df81e9f287d36082adb747) | Согласованы название дашборда, окна метрик и порог ошибок |
| [d6b1537](https://github.com/spasster/MTC-SRE/commit/d6b1537caa81b7bf3d0412bd05a54c24605d2ea1) | Проверено масштабирование через настоящий Kubernetes API |
| [07be559](https://github.com/spasster/MTC-SRE/commit/07be5597a07081c4df8c297a4596778864a211e3) | Проверены убийство пода и безопасная реакция на отказы зависимостей |
| [50798f3](https://github.com/spasster/MTC-SRE/commit/50798f363b2e069999ed689a359760d0f1ba8405) | Сохранено подтверждение защиты масштаба от Kubernetes API |
| [9c3266f](https://github.com/spasster/MTC-SRE/commit/9c3266f93c52e7d02cdd8daed2c3ff81c05ae7cb) | Обновлены выводы по повторным экспериментам и отказам зависимостей |
| [28717d8](https://github.com/spasster/MTC-SRE/commit/28717d8d2425943ca117c1c4595cbbe34c14f00e) | README приведён к завершённому контуру и проверенным ограничениям |
| [cd01848](https://github.com/spasster/MTC-SRE/commit/cd01848bb59ab6013204b69d6d0b4d1e21d155d9) | План дополнен результатами выполненных этапов |
| [bf9e2d9](https://github.com/spasster/MTC-SRE/commit/bf9e2d957156b04224ba056197d9a58686c43cc7) | Завершён повторный аудит кода и требований кейса |
| [5402945](https://github.com/spasster/MTC-SRE/commit/5402945ee8c1cf4dcc8321404191856096835df2) | Описаны исправления и отдельные коммиты по пунктам |
