# Linux worker на VPS

Это инструкция и подготовленный [systemd service](../deploy/hedge-bot.service). **Сервер пользователя пока не выбран и не развёрнут.** Локальный worker продолжает работу после закрытия окна desktop, но останавливается при выключении ПК. Для работы независимо от ПК нужен постоянно включённый VPS.

Ниже описан headless запуск без Docker, GUI и Tkinter: `run.py --worker`. На сервере нужен Linux x86_64 с systemd, Python **3.13** с `venv`, актуальные корневые сертификаты и синхронизация времени. Архитектура должна поддерживаться native signer официального Lighter SDK; ARM и другие комбинации здесь не проверены. Версии runtime фиксированы в `requirements-runtime.txt`; используйте проверенный commit проекта и не обновляйте SDK независимо от него.

## Код, пользователь и Python

Подготовьте архив выбранного commit без секретов через `git archive` на машине с доступом к приватному репозиторию и передайте его на VPS по SSH. Альтернатива — приватный checkout администратора. Торговому пользователю не нужен GitHub token; runtime не обращается к GitHub.

Следующие команды выполняет администратор VPS. `hedge-bot-source.tar` — путь к переданному архиву исходников; установка Python 3.13 зависит от выбранного дистрибутива и должна предшествовать этим командам.

```bash
sudo useradd --system --user-group --home-dir /var/lib/hedge-bot --shell /usr/sbin/nologin hedge-bot
sudo install -d -o root -g root -m 0755 /opt/hedge-bot/app
sudo tar --extract --file hedge-bot-source.tar --directory /opt/hedge-bot/app --no-same-owner
sudo python3.13 -m venv /opt/hedge-bot/.venv
sudo /opt/hedge-bot/.venv/bin/python -m pip install -r /opt/hedge-bot/app/requirements-runtime.txt
sudo /opt/hedge-bot/.venv/bin/python -m pip check
sudo install -d -o hedge-bot -g hedge-bot -m 0700 /var/lib/hedge-bot
sudo install -d -o root -g hedge-bot -m 0750 /etc/hedge-bot
```

Код и venv принадлежат администратору; worker работает как `hedge-bot` без прав root и не может переписывать исходники. Его изменяемое состояние находится только в `/var/lib/hedge-bot`. Базовый Python для venv должен находиться вне `/home`: service закрывает доступ к домашним каталогам.

## Master key и TLS

Linux vault шифруется AES-GCM. Файл `HEDGE_MASTER_KEY_FILE` должен содержать **32 случайных байта**, находиться вне каталога состояния, принадлежать `hedge-bot` и иметь права **0400**. Создайте его один раз; команда ниже откажется перезаписать существующий ключ и не выводит его содержимое:

```bash
sudo python3.13 - <<'PY'
import os
import secrets
import shutil
path = '/etc/hedge-bot/master.key'
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'wb') as stream:
    stream.write(secrets.token_bytes(32))
    stream.flush()
    os.fsync(stream.fileno())
shutil.chown(path, user='hedge-bot', group='hedge-bot')
os.chmod(path, 0o400)
PY
```

Потеря этого ключа делает существующий Linux vault нечитаемым. Замена ключа не расшифрует старые данные. Worker умеет создать отсутствующий master key автоматически, поэтому храните резервную копию и не запускайте его с неправильным путём ключа. Ключ, vault, TLS private key и токен управления не должны попадать в репозиторий или сообщения.

Для прямого удалённого подключения обязательны `--certificate`, `--certificate-key` и точный `--public-url`. Получите сертификат доверенного CA на собственный DNS-адрес, установите полную цепочку в `/etc/hedge-bot/fullchain.pem` и private key в `/etc/hedge-bot/tls.key`. Это обычные файлы, читаемые пользователем `hedge-bot`; TLS key — с правами 0400. Не ослабляйте проверку сертификата desktop. Продление сертификата и установка обновлённой цепочки требуют отдельной настройки на выбранном сервере; worker загружает их при запуске.

Создайте `/etc/hedge-bot/worker.env` с правами 0600, владелец root. Здесь хранятся пути и адрес, **не секреты**. Замените `hedge.example.com` своим DNS-адресом:

```ini
HEDGE_HOST=0.0.0.0
HEDGE_PORT=8765
HEDGE_PUBLIC_URL=https://hedge.example.com:8765
HEDGE_CERTIFICATE=/etc/hedge-bot/fullchain.pem
HEDGE_CERTIFICATE_KEY=/etc/hedge-bot/tls.key
```

Адрес в desktop должен в точности совпадать с `HEDGE_PUBLIC_URL`, включая порт. Worker проверяет HTTP Host и Origin. Без TLS HTTP разрешён только на loopback. Этот service использует прямое HTTPS-подключение; reverse proxy и его конфигурация сюда не включены. Настройте DNS и сетевой доступ к выбранному порту для своего клиента. Исходящие HTTPS/WebSocket должны доходить до Nado и `api.rh.lighter.xyz`; другая сеть Lighter автоматически не подставляется.

## Токен управления и запуск

Отдельный токен управления desktop не является signing key площадки. Worker хранит его внутри vault и не публикует через HTTP или штатные логи. Его можно получить **только явной локальной bootstrap-командой** в приватном SSH-терминале администратора:

```bash
sudo -u hedge-bot env HEDGE_MASTER_KEY_FILE=/etc/hedge-bot/master.key /opt/hedge-bot/.venv/bin/python /opt/hedge-bot/app/run.py --worker --state-dir /var/lib/hedge-bot --show-control-token
```

Эта команда намеренно показывает токен владельцу терминала. Скопируйте его прямо в desktop «Торговля → Подключить VPS» вместе с HTTPS-адресом. Не включайте `--show-control-token` в service, cron, записи терминала, CI или команды, результат которых публикуется. В этой инструкции нет действующего токена.

```bash
sudo install -o root -g root -m 0644 /opt/hedge-bot/app/deploy/hedge-bot.service /etc/systemd/system/hedge-bot.service
sudo systemd-analyze verify /etc/systemd/system/hedge-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now hedge-bot.service
sudo systemctl status hedge-bot.service
```

Service использует `WorkingDirectory=/opt/hedge-bot/app` и абсолютный headless entrypoint `/opt/hedge-bot/app/run.py --worker`. Он автоматически перезапускает упавший процесс с задержкой, ограничивает частоту неудачных запусков и закрывает доступ на запись к системным каталогам. Доступ к `/var/lib/hedge-bot` разрешён; каталог имеет права 0700. Master key остаётся отдельным файлом для чтения.

После подключения desktop настройте signing keys уже **на серверном worker**, проверьте доступ, сохраните стратегию и нажмите «Проверить без ордеров». Она проверяет реальные аккаунты, текущие рынки и ограничения, но не доказывает фактическое исполнение будущего ордера. Никаких ордеров до отдельной команды «Запустить реальные циклы» нет.

Удержание по умолчанию — 12 часов, затем закрытие обеих сторон и смена подходящего разрешённого актива. Работа продолжается до остановки либо срабатывания ограничений; при недоступном рынке worker ждёт. Участие аккаунта и списка активов в xPoints подтверждает пользователь по действующей программе. Это не автоматическая гарантия eligibility или начисления. Реальное исполнение на пополненных аккаунтах в ходе разработки не проверялось.

## Состояние, резервная копия и перенос

`vault.bin` содержит зашифрованные signing keys, токен управления и настройки. `hedge.sqlite` и возможные `hedge.sqlite-wal` / `hedge.sqlite-shm` содержат журнал, намерения ордеров и таймеры. Файл `.lock` обеспечивает одного владельца журнала. SQLite-журнал не шифруется приложением; каталог и резервная копия должны защищать эти сведения. Резервируйте vault **вместе с согласованным журналом**, а master key — отдельно в другом защищённом месте.

Для планового резервирования сначала в desktop запретите новые циклы и дождитесь полного закрытия; при необходимости используйте «Закрыть хедж и остановить». Убедитесь в `CLOSED` и отсутствии открытых ордеров/остаточной экспозиции на обеих площадках, затем остановите service. Копирование только `hedge.sqlite` во время работающего WAL-журнала может потерять записи. Для этой инструкции согласованная копия делается при остановленном worker.

Зашифруйте архив до записи на диск резервного хранения. Следующие команды требуют GnuPG и запускаются в доверенном терминале; `$STATE_BACKUP` и `$KEY_BACKUP` — выбранные вами разные выходные пути, а пароль вводится через защищённый запрос GnuPG, не в командной строке. Если используются другие средства шифрования резервных копий, сохраняйте те же требования к согласованности и разделению ключа/данных.

```bash
set -euo pipefail
: "${STATE_BACKUP:?Укажите путь зашифрованной копии состояния}"
: "${KEY_BACKUP:?Укажите другой путь зашифрованной копии ключа}"
umask 077
sudo systemctl stop hedge-bot.service
sudo tar --create --gzip --file - --directory /var/lib hedge-bot | gpg --symmetric --cipher-algo AES256 --output "$STATE_BACKUP"
sudo tar --create --gzip --file - --directory /etc/hedge-bot master.key | gpg --symmetric --cipher-algo AES256 --output "$KEY_BACKUP"
sudo systemctl start hedge-bot.service
```

Проверьте код завершения обеих операций и возможность расшифровать копии в защищённом месте до удаления старых копий. Не храните обе копии с единственным паролем рядом с VPS. При восстановлении используйте исходный master key и полный журнал соответствующей копии, восстановите владельцев/права и проверьте состояние площадок до разрешения новых циклов. Не восстанавливайте устаревшую копию поверх активного worker и не редактируйте journal вручную.

Windows vault привязан DPAPI к пользователю Windows и не расшифруется на Linux. Для переноса с локального ПК сначала остановите новые циклы и завершите текущий хедж. Настройте серверный доступ заново, затем проведите preflight. Не запускайте одновременно два worker на одних торговых аккаунтах: файловая блокировка защищает только один журнал на одной машине.

## Обновление и восстановление после сбоя

Плановое обновление: остановить новые циклы, завершить открытый хедж, остановить service и создать резервные копии. Установить выбранный проверенный commit и соответствующие фиксированные runtime-зависимости, выполнить `pip check`, затем запустить service. Не заменяйте `/var/lib/hedge-bot` и `/etc/hedge-bot/master.key` при обновлении. При изменении unit сначала выполните `systemd-analyze verify`, затем `daemon-reload`. После запуска проверьте состояние в desktop и preflight перед возобновлением циклов.

Аварийный restart во время открытого хеджа использует сохранённый журнал и сверяет неизвестные исполнения с площадками. Он не означает, что позиции автоматически закрылись или что новую торговлю можно начинать. В `RECOVERY` / `HALTED` проверьте фактическую экспозицию и журнал; не сбрасывайте состояние, не выдавайте повторный ордер вместо неизвестного и не удаляйте файлы ради запуска. При подтверждённой внешней смене ключа можно восстановить доступ ключом того же аккаунта; смена аккаунта при открытой экспозиции запрещена.

`systemctl stop`, остановка VPS и падение процесса **не закрывают позиции на биржах**. Для их закрытия нужна команда desktop и подтверждение исполнения обеих сторон. Service посылает SIGINT и даёт worker время закрыть локальные ресурсы; это не команда рыночного закрытия.

После фактического развёртывания последующие завершённые серверные обновления должны доставляться на этот VPS. Сейчас автоматический серверный deploy, сертификаты, DNS и резервное хранилище ещё не настроены.

Первичные источники: [Python 3.13 venv](https://docs.python.org/3.13/library/venv.html), [systemd service](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml), [systemd execution environment](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml), [GnuPG symmetric encryption](https://www.gnupg.org/documentation/manuals/gnupg/Operational-GPG-Commands.html). Ограничения стратегии и ключей: [TRADING.md](TRADING.md).
