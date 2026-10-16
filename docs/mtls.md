# mTLS между сервисами

Политика и исключения портов лежат в `k8s/mtls.yaml`, параметры Istio — в `observability/istio-values.yaml`. Место mTLS в схеме: [architecture.md](architecture.md).

Istio 1.30.5 добавляет Envoy к подам `demo`. Политика `PeerAuthentication` требует STRICT mTLS для checkout, payment и Redis. k6 тоже входит в mesh. Healer получает sidecar для обращения к Redis; его webhook отдельно проверяет токен Alertmanager.

У checkout и payment выделен порт 8081, обслуживающий только метрики. Для этого порта разрешён HTTP от Prometheus, а рабочий порт 8080 остаётся под STRICT mTLS. Исключение не открывает `/work` и `/pay`. Сведения о механизме - в [документации Istio](https://istio.io/latest/docs/tasks/security/authentication/authn-policy/).

Используются native sidecar: Envoy запускается как перезапускаемый init-контейнер, поэтому его работа не удерживает завершившийся Job k6. Ресурсы proxy ограничены в `observability/istio-values.yaml`. Официальное описание [аннотации nativeSidecar](https://istio.io/latest/docs/reference/config/annotations/).

После добавления mesh в существующий кластер нужно пересоздать поды demo и healer, затем дождаться rollout. Команда `sh scripts/verify-mtls.sh` проверяет успешный `/work` из meshed-пода, отказ на рабочем порту для клиента без sidecar и доступ к порту метрик. Наличие политики в YAML само по себе не подтверждает защиту.


Chaos Mesh работает с шифрованным трафиком на уровне сети. Задержка одного checkout направлена на поды payment: после внедрения Envoy исходящие пакеты адресованы endpoint IP, а не ClusterIP Service.
