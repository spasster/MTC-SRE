# Системные журналы Ubuntu

`make logs-linux` устанавливает Loki, запускает Ansible для текущего Ubuntu-хоста и подключает Alloy к `/var/log/syslog` и `/var/log/audit/audit.log`. Команда требует sudo и ядро с поддержкой audit. WSL подходит для проверки Kubernetes, но наличие audit в его ядре нужно проверить отдельно: отсутствие события audit не считается успешной проверкой.

```sh
make logs-linux
sudo auditctl -l | grep mtc-marker
logger -t mtc-sre 'Проверка доставки syslog'
sudo touch /etc/mtc-sre-audit-marker
sudo ausearch -k mtc-marker -ts recent
```

В Grafana Explore выбрать Loki и проверить `{job="syslog",host="linux"} |= "mtc-sre"`, затем `{job="audit",host="linux"} |= "mtc-marker"`. Это проверяет доставку событий, а не только наличие контейнера сборщика.

Ansible создаёт наблюдаемый файл до загрузки правила и не удаляет уже установленные правила auditd. Linux использует отдельный inventory с локальным подключением; Colima сохраняет SSH inventory. Значения журналов не добавляются в Git: для результатов достаточно времени и счётчика найденных контрольных событий.

Promtail достиг EOL 2 марта 2026 года, поэтому вместо него используется Alloy. Источник: [официальная документация Grafana](https://grafana.com/docs/loki/latest/send-data/promtail/). Позиции чтения хранятся в отдельном Docker volume, а каталог журналов подключён только на чтение.

При обновлении старой установки сначала проверить доставку через Alloy, затем удалить старые сборщики: `helm uninstall promtail -n obs` и остановить контейнер `promtail-host`. Автоматически удалять старый сборщик до проверки доставки не требуется.
