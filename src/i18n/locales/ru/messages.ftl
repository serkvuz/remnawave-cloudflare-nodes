# Жизненный цикл сервиса
service-started = <b>🚀 Сервис запущен</b>
    Мониторинг активен.

    { $summary }

service-stopped = <b>🛑 Сервис остановлен</b>
    Мониторинг выключен.

# Изменения статуса нод
node-became-healthy = <b>✅ Нода онлайн</b>
    { $name } ({ $address }) доступна.

    { $stats }

node-became-unhealthy = <b>❌ Нода офлайн</b>
    { $name } ({ $address }) недоступна.
    Причина: { $reason }

    { $stats }

# Операции DNS
dns-record-added = <b>📝 DNS обновлен</b>
    Добавлен { $ip } → { $domain }

dns-record-removed = <b>🗑️ DNS удален</b>
    Удален { $ip } из { $domain }

# Ошибки
dns-operation-error = <b>⚠️ Ошибка DNS</b>
    Не удалось { $action } { $ip } для { $domain }
    Ошибка: { $error }

health-check-error = <b>⚠️ Ошибка проверки</b>
    Ошибка при проверке: { $error }

    Вы получите уведомление, когда проверка восстановится.

health-check-recovered = <b>✅ Проверка восстановлена</b>
    Проверки снова работают.
    Сбой длился { $duration } ({ $cycles } циклов).
    Последняя ошибка: { $error }

# Критические состояния
all-nodes-down = <b>🔴 КРИТИЧНО: Все ноды недоступны</b>
    Все { $total } нод недоступны.
    Затронуты: { $nodes }

    DNS записи очищены. Требуется немедленное вмешательство.

all-nodes-recovered = <b>🟢 Восстановление: Ноды снова онлайн</b>
    { $online } из { $total } нод снова доступны.
    DNS записи восстановлены.

# Шаблоны форматирования
node-reason-unknown = неизвестно
node-stats-line = 📊 Ноды: { $online }/{ $total } онлайн, { $offline } офлайн, { $disabled } отключено
node-zone-line = • { $zone }: { $online }/{ $total } онлайн, { $offline } офлайн
ip-list-item = • { $ip }
service-summary-header = 📡 Домены:
service-no-zones = —
service-zone-line = • { $zone } — { $count } IP-адресов
service-api-info = 🔌 API: { $host }:{ $port }
domain-zone-line = • { $zone } (TTL: { $ttl }, Проксировано: { $proxied })
zone-added-details =
    IP-адреса:
            { $ip_list }
            TTL: { $ttl } | Проксировано: { $proxied }
zone-change-ips =
    IP-адреса:
            { $ip_list }
zone-change-ttl = TTL: { $value }
zone-change-proxied = Проксировано: { $value }

# События API
api-config-updated = <b>⚙️ Конфигурация обновлена через API</b>
    { $changes }
    От: { $ip }

api-domain-added = <b>➕ Домен добавлен через API</b>
    { $domain }
    { $details }
    От: { $ip }

api-domain-removed = <b>➖ Домен удален через API</b>
    { $domain }
    От: { $ip }

api-zone-added = <b>➕ Зона добавлена через API</b>
    { $fqdn }
    { $details }
    От: { $ip }

api-zone-updated = <b>⚙️ Зона обновлена через API</b>
    { $fqdn }
    { $changes }
    От: { $ip }

api-zone-removed = <b>➖ Зона удалена через API</b>
    { $fqdn }
    От: { $ip }

# Изменения состояния хостов
host-enabled = • { $remark } включен ✅
host-disabled = • { $remark } отключен ❌
host-group-enabled = <i>Некоторые ноды, управляющие { $address }, работают</i>
host-group-disabled = <i>Все ноды, управляющие { $address }, недоступны</i>
host-state-change = <b>🔧 Изменение состояния хостов</b>
    { $changes }

host-sync-failure = <b>⚠️ Сбой синхронизации хостов</b>
    Не удалось получить хосты из панели { $failures } раз подряд.
    Хосты больше не включаются и не отключаются.

    Ошибка: { $error }

# Автообнаружение
nodes-discovered = <b>🆕 Новые ноды добавлены в config.yml</b>
    { $changes }
discovery-line = • { $name } ({ $ip }) → { $fqdn }
discovery-line-new-zone = • { $name } ({ $ip }) → { $fqdn } (новая зона)

# Обслуживание и пауза
maintenance-changed = <b>🔧 Обслуживание</b>
    { $changes }
maintenance-started-line = • { $name } ({ $address }) на обслуживании: убрана из DNS, уведомления отключены
maintenance-ended-healthy = • { $name } ({ $address }) вышла из обслуживания и работает: возвращена в DNS
maintenance-ended-unhealthy = • { $name } ({ $address }) вышла из обслуживания, но недоступна: не добавлена в DNS
maintenance-ended-gone = • { $name } вышла из обслуживания (её больше нет в панели)

dns-paused = <b>⏸️ Изменения DNS приостановлены</b>
    Кем: { $by }
    { $until }

    Проверки и уведомления продолжаются. Записи DNS и хосты панели не изменяются.
dns-paused-until = До: { $until }
dns-paused-indefinite = До команды /resume
dns-resumed = <b>▶️ Изменения DNS возобновлены</b>
    Кем: { $by }
    Пауза длилась { $duration }. Записи DNS синхронизируются.
dns-resumed-auto = <b>▶️ Изменения DNS возобновлены</b>
    Пауза истекла через { $duration }. Записи DNS синхронизируются.

# Периодическая сводка
summary-daily = <b>📊 Сводка за день</b>
summary-weekly = <b>📊 Сводка за неделю</b>
summary-current = <b>📊 Текущая сводка</b>
summary-period = 🗓 { $start } → { $end }
summary-no-nodes = Ноды ещё не наблюдались.
summary-node-no-data = • { $name }: данных пока нет
summary-node-clean = • { $name }: { $uptime } онлайн, без сбоев
summary-node-line = • { $name }: { $uptime } онлайн, { $outages } { $outages ->
        [one] сбой
        [few] сбоя
       *[many] сбоев
    }, самый долгий { $longest }
summary-ongoing = (всё ещё недоступна)
summary-longest = ⏱ Самый долгий сбой: { $name }, { $duration }
summary-no-outages = ⏱ Сбоев за период не было
summary-dns = 🌐 Изменения DNS: { $added } добавлено, { $removed } удалено
summary-checks = 🩺 Проверки: { $failures } из { $cycles } с ошибкой

# /status
status-title = <b>📡 Статус</b>
status-last-check = Последняя проверка: { $time }
status-never = Ни одна проверка ещё не завершилась.
status-failing = ⚠️ Проверки не работают уже { $duration } ({ $cycles } циклов): { $error }
status-paused = ⏸️ Изменения DNS приостановлены ({ $by }), { $until }
status-paused-until = до { $time }
status-paused-indefinite = до /resume
status-nodes-header = <b>Ноды</b> ({ $online }/{ $total } в DNS)
status-node-online = ✅ { $name } ({ $address })
status-node-offline = ❌ { $name } ({ $address }): { $reasons }
status-node-maintenance = 🔧 { $name } ({ $address }): обслуживание
status-node-pending-down = ⏳ { $name } ({ $address }): сбой { $pending }, пока в DNS ({ $reasons })
status-node-pending-up = ⏳ { $name } ({ $address }): восстановление { $pending }, пока не в DNS
status-node-missing = ❔ { $address }: есть в config.yml, но нет в панели
status-dns-header = <b>DNS</b>
status-dns-line = • { $fqdn }: { $records } ({ $count }/{ $total })
status-dns-empty = • { $fqdn }: записей нет (0/{ $total })
status-dns-error = • { $fqdn }: ⚠️ { $error }

# Команды бота
cmd-help = <b>🤖 Команды</b>
    /status: ноды и текущие записи DNS
    /reload: перечитать config.yml и hosts.yml
    /pause [30m|2h|1d]: приостановить изменения DNS (без срока = до /resume)
    /resume: возобновить изменения DNS
    /maintenance: ноды на обслуживании
    /maintenance on &lt;нода&gt;: отправить ноду на обслуживание
    /maintenance off &lt;нода&gt;: вернуть ноду из обслуживания
    /summary: статистика доступности за текущий период
cmd-reload-ok = ✅ Конфигурация перечитана. Проверка запущена.
cmd-reload-failed = ❌ Не удалось перечитать конфигурацию, действует прежняя:
    { $error }
cmd-error = ❌ { $error }
cmd-resume-not-paused = Изменения DNS не приостановлены.
cmd-maintenance-list = <b>🔧 На обслуживании</b>
    { $entries }
cmd-maintenance-empty = Нод на обслуживании нет.
cmd-maintenance-entry = • { $name } ({ $address })
cmd-maintenance-entry-unmatched = • { $entry } (не найдена в панели)
cmd-maintenance-usage = Использование: /maintenance on &lt;нода&gt; или /maintenance off &lt;нода&gt;, где &lt;нода&gt; это имя, адрес или UUID ноды.
cmd-maintenance-unknown = ❌ В панели нет ноды «{ $node }». Ноды: { $nodes }
cmd-maintenance-no-data = ❌ Список нод ещё не загружен. Повторите после первой проверки.
cmd-maintenance-on = 🔧 { $name } отправляется на обслуживание. Проверка запущена.
cmd-maintenance-already = { $name } уже на обслуживании.
cmd-maintenance-off = ✅ { $name } выходит из обслуживания. Проверка запущена.
cmd-maintenance-not-in = { $name } не на обслуживании.
