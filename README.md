# Vast HLS Orchestrator

Production-oriented Python-orchestrator для временного HLS-транскодинга на NVIDIA GPU из маркетплейса [Vast.ai](https://vast.ai/). Исходное видео хранится на origin-сервере, GPU-instance существует только во время обработки, а готовый ABR HLS возвращается на origin и публикуется с атомарной заменой каталога.

> Orchestrator запускается **только на Binary Racks origin/storage VPS**. Приватный SSH-ключ origin, `VAST_API_KEY` и любые административные credentials никогда не копируются в Vast instance.

## Навигация

- [Архитектура](#архитектура)
- [Структура проекта](#структура-проекта)
- [Полный жизненный цикл job](#полный-жизненный-цикл-job)
- [Доступы и модель безопасности](#доступы-и-модель-безопасности)
- [Как получить и настроить доступы](#как-получить-и-настроить-доступы)
- [Требования](#требования)
- [Установка](#установка)
- [Запуск](#запуск)
- [Поиск, фильтрация и рейтинг машин](#поиск-фильтрация-и-рейтинг-машин)
- [Создание Vast instance](#создание-vast-instance)
- [Bootstrap и remote job](#bootstrap-и-remote-job)
- [Загрузка исходного видео](#загрузка-исходного-видео)
- [Проверка GPU и диска](#проверка-gpu-и-диска)
- [FFmpeg и ABR HLS](#ffmpeg-и-abr-hls)
- [Прогресс и логи](#прогресс-и-логи)
- [Получение и публикация результата](#получение-и-публикация-результата)
- [Cleanup, watchdog и обработка ошибок](#cleanup-watchdog-и-обработка-ошибок)
- [Параметры командной строки](#параметры-командной-строки)
- [Библиотеки и внешние инструменты](#библиотеки-и-внешние-инструменты)
- [Эксплуатационные рекомендации](#эксплуатационные-рекомендации)

## Архитектура

```text
Binary Racks origin
  │
  │  1. Vast API: search + create
  ▼
Temporary Vast.ai NVIDIA instance
  │
  │  2. aria2 загружает публичный source MP4 с origin
  │  3. один NVDEC decode → GPU split → 4× scale_cuda → 4× NVENC → 4 HLS rendition
  │  4. instance сам push'ит результат на origin: до 16 параллельных staggered
  │     rsync-воркеров с ephemeral deploy-ключом → staging-каталог
  │
Binary Racks staging → atomic publish /video/<video_id>/abr/
  │
  └─ 5. DELETE Vast instance в обязательном finally
```

Vast instance никогда не получает приватный административный ключ Binary Racks и не может выполнять на origin произвольные команды. Направление передачи результата — **push с Vast**, а не pull с origin: bulk-данные (сегменты, playlists, master) идут не через Vast'овский `sshN.vast.ai` relay (control-plane канал, ограниченный по скорости), а напрямую наружу с самого instance, с отдельным ephemeral deploy-ключом (см. [Доступы и модель безопасности](#доступы-и-модель-безопасности)), у которого нет прав ни на что, кроме file-transfer в один конкретный staging-каталог.

Пакет: [`src/vast_hls_orchestrator/`](src/vast_hls_orchestrator/) (см. [Структура проекта](#структура-проекта)).

## Структура проекта

Каждый модуль имеет одну ответственность и не превышает 300 строк. Логика сгруппирована по пакетам:

```text
src/vast_hls_orchestrator/
├── cli.py                  # argparse: разбор аргументов командной строки
├── pipeline.py              # оркестрация полного жизненного цикла job (rent → ... → destroy)
├── __main__.py               # точка входа: parse_args → configure_logging → pipeline.run
│
├── core/                      # общие основы, без сетевых/файловых side effects
│   ├── constants.py             # API endpoints, allow-list GPU, имена renditions
│   ├── errors.py                 # иерархия исключений (VastError и наследники)
│   ├── console.py                 # общий Rich Console (stderr)
│   ├── logging_setup.py            # Loguru → Rich sink (обычная построчная печать)
│   ├── models.py                     # dataclasses: EncodeProgress, RemoteSnapshot, JobContext
│   └── validation.py                  # validate_inputs() для CLI-аргументов
│
├── vast_api/                  # HTTP-клиент Vast.ai marketplace
│   ├── client.py                 # VastClient: search/create/show/destroy instance
│   └── offers.py                  # оценка размера source, ranking, поиск+таблица offers
│
├── remote/                    # всё, что выполняется на/через временный Vast instance
│   ├── job_script.py             # bash-скрипт: download, GPU preflight, ABR encode, upload-стадия
│   ├── upload_batching.py          # LPT size-balanced батчинг сегментов; embedded в job_script.py
│   ├── onstart.py                  # bootstrap (`onstart`): запись SSH-ключа, watchdog, запуск job
│   ├── ssh.py                       # non-interactive ssh_run/wait_for_ssh (spinner, один endpoint)
│   └── snapshot.py                   # парсинг FFmpeg `-progress`, upload-статуса и nvidia-smi по SSH
│
├── ui/
│   └── formatting.py            # format_duration/format_bytes/format_cost — общие форматтеры лога
│
└── orchestration/              # жизненный цикл instance и результата
    ├── provisioning.py            # rent_instance, wait_for_running, read_public_key
    ├── deploy_key.py                # ephemeral per-job deploy-ключ для push Vast → origin
    ├── job_monitor.py               # wait_for_job: SSH-поллинг + периодические строки прогресса
    ├── publish.py                    # atomic_exchange_dirs, finalize_push_publish (только локально)
    ├── local_state.py                 # file lock, prepare_staging_dir, recovery прерванного publish
    └── diagnostics.py                  # tail удалённых логов при сбое
```

Зависимости идут в одну сторону: `core` ни от чего не зависит; `vast_api`, `remote`, `ui` зависят только от `core`; `orchestration` зависит от `core`/`vast_api`/`remote`/`ui`; `pipeline.py` и `cli.py` — самый верхний уровень. Никакого постоянного полноэкранного режима нет: вывод — обычный последовательный лог, спиннеры (`console.status`) на время конкретной операции (поиск offers, ожидание running/SSH) и периодические строки прогресса во время encoding — так же, как повёл бы себя любой обычный CLI-инструмент.

## Полный жизненный цикл job

1. Orchestrator проверяет аргументы, URL, `video_id`, API key, локальный SSH key и `--origin-ssh-host`.
2. Для обычного запуска получает эксклюзивный file lock для конкретного `video_id`, затем сметает просроченные записи предыдущих deploy-ключей в `authorized_keys` (backstop на случай `kill -9` прошлого запуска — см. [Доступы и модель безопасности](#доступы-и-модель-безопасности)).
3. Генерирует `job_token` (тот же паттерн, что раньше был у `create_label`) и создаёт staging-каталог `abr.staging.<job_token>` заранее — instance должен иметь куда push'ить весь job, а не только в конце. Тут же генерирует ephemeral deploy-keypair и добавляет публичную половину в локальный `authorized_keys`.
4. Выполняет `HEAD` source URL, если сервер поддерживает его, и получает ожидаемый размер MP4.
5. Запрашивает у Vast.ai подходящие offers по allow-list GPU (по умолчанию — только RTX 4080).
6. Объединяет, дедуплицирует и ранжирует offers по ожидаемой полной стоимости job.
7. Принимает лучший доступный offer через Vast API и получает `instance_id`.
8. Ждёт `actual_status=running`, затем отдельно ждёт появления рабочего SSH endpoint; если SSH так и не поднялся, делает `reboot` инстанса и повторяет ожидание — до нескольких таких циклов подряд, прежде чем считать job проваленным (см. [SSH-ключ пишется в контейнер напрямую](#ssh-ключ-пишется-в-контейнер-напрямую-а-не-через-аккаунтную-инъекцию-vast)).
9. Как только admin SSH-канал подтверждён живым, origin пушит приватную половину deploy-ключа на instance тем же каналом (только через stdin, не через `onstart` и не в аргументах команды).
10. Vast выполняет переданный `onstart`: запускает watchdog, устанавливает `rsync` и стартует remote encode job.
11. Origin через SSH (relay-эндпоинт) опрашивает stage, download size, GPU telemetry, encode/upload progress и tails логов; сам upload progress считает по факту роста staging-каталога на своей стороне, без дополнительных SSH round-trip'ов.
12. Remote job скачивает source, проверяет диск и GPU pipeline, затем одним FFmpeg-процессом (общий decode, GPU-side split) кодирует все четыре rendition, после чего сам (`stage=uploading`) пушит результат напрямую на origin — см. [Upload: direct parallel push](#upload-direct-parallel-push-vast--origin).
13. Origin делает `staging/master.m3u8.tmp → staging/master.m3u8` и атомарно публикует staging.
14. Независимо от результата Python `finally` вызывает DELETE instance и убирает deploy public key из локального `authorized_keys`.

## Доступы и модель безопасности

### `VAST_API_KEY`

- Хранится только в environment Binary Racks.
- Передаётся Vast API как `Authorization: Bearer ...` библиотекой Requests.
- Не вставляется в `onstart`, remote job, SSH-команды или логи.
- Нужен для поиска offers, создания, просмотра и удаления instances.

### SSH key `/root/.ssh/vast_encoder`

- Приватная часть существует только на Binary Racks.
- Публичная часть передаётся в `onstart` и пишется в `authorized_keys` контейнера напрямую (см. [SSH-ключ пишется в контейнер напрямую](#ssh-ключ-пишется-в-контейнер-напрямую-а-не-через-аккаунтную-инъекцию-vast)) — добавлять её в аккаунт Vast.ai заранее больше не обязательно для доступа, хотя это остаётся частью первоначальной настройки и не вредит.
- Используется локальным `ssh` для control-plane доступа к Vast. Direct endpoint всегда собирается только как `public_ipaddr + ports["22/tcp"][0]["HostPort"]`; proxy endpoint — только как `ssh_host + ssh_port`. Поля этих двух маршрутов не смешиваются.
- Временный Vast instance видит только соответствующий public key.
- Для каждого job создаётся отдельный ephemeral `known_hosts`; это устраняет конфликты при повторном использовании Vast IP/port.

### `CONTAINER_API_KEY`

Vast автоматически внедряет внутрь instance ограниченный ключ `CONTAINER_API_KEY` и `CONTAINER_ID`. Этот ключ может управлять только данным instance и используется исключительно failsafe-watchdog для self-destroy. Это не `VAST_API_KEY` пользователя и не credential origin.

### Ephemeral deploy-ключ `/root/.ssh/vast_deploy_key` (Vast → origin, только для push)

Отдельный, специально урезанный по privilege ключ — направление доверия здесь обратное по сравнению с админским `/root/.ssh/vast_encoder` выше: он даёт **Vast'у** возможность что-то сделать **на origin**, а не наоборот. Именно поэтому его жизненный цикл спроектирован так, чтобы урезать возможный ущерб на каждом шаге:

- **Генерируется на origin**, не на Vast: `ssh-keygen -t ed25519` во временный файл, оба байта сразу читаются в память, временный файл удаляется. Приватная половина никогда не проходит через `onstart`/Vast API — она передаётся на instance напрямую через уже установленный admin SSH-канал, **только через stdin**, никогда не как аргумент команды и никогда не логируется (`orchestration/deploy_key.py`).
- **Публичная половина** дописывается в `--origin-authorized-keys` (по умолчанию — `~/.ssh/authorized_keys` того же OS-пользователя, что запускает orchestrator, поскольку он уже имеет права писать в `--origin-root`; можно указать выделенного ограниченного deploy-пользователя явно — это не обязательно, но рекомендуется как дальнейшее укрепление) под уникальным тегом `vast-deploy-<job_token>-<unix_ts>`, под `flock`.
- **Живёт только во время job**: на Vast — до `finish()` в конце job_script.py (`rm -f /root/.ssh/vast_deploy_key`; полная гарантия — DELETE instance в `finally`); на origin — публичная запись удаляется в `finally` пайплайна безусловно.
- **Backstop на случай `kill -9` orchestrator'а**: `finally` тогда не выполнится, а origin — персистентный сервер, так что просроченная запись иначе осталась бы навсегда. При каждом старте (рядом с `recover_local_publish_state`) `deploy_key.sweep_stale_keys()` вычищает записи со своим же тегом старше `STALE_DEPLOY_KEY_MAX_AGE_S` (24 часа) — никогда не трогает чужие строки в `authorized_keys`.
- **Урезан по возможностям, не только по времени жизни**: используется только для `rsync`/file-transfer на один конкретный staging-каталог, никогда для выполнения произвольных команд на origin (см. [Upload: direct parallel push](#upload-direct-parallel-push-vast--origin)). SSH `command=`-форсинг (жёстко ограничить ключ конкретно на `rsync --server ...`) **не реализован** в этой версии — hand-crafting точного разрешённого rsync-server invocation достаточно хрупок, чтобы сломать легитимные transfer'ы при малейшей неточности; это осознанный компромисс, а не недосмотр, и разумный шаг для дальнейшего укрепления, если понадобится.

### Source URL

URL должен быть доступен из интернета по HTTP или HTTPS. По умолчанию предполагается публичный read-only MP4, например:

```text
https://origin.example.com/video/test.mp4
```

Не помещайте в URL административные credentials. Если в будущем понадобятся signed URLs, учитывайте, что сам URL неизбежно будет передан remote job.

## Как получить и настроить доступы

### 1. Vast API key

1. Войдите в [Vast.ai Console](https://console.vast.ai/).
2. Откройте настройки API Keys/Keys.
3. Создайте ключ с правами, необходимыми для чтения offers и управления собственными instances.
4. На Binary Racks задайте его через environment:

```bash
export VAST_API_KEY='your-vast-api-key'
```

Для постоянного запуска лучше использовать защищённый systemd `EnvironmentFile` с mode `0600`, а не добавлять ключ в shell history или исходный код.

### 2. Отдельный SSH key

На Binary Racks:

```bash
install -d -m 700 /root/.ssh
ssh-keygen -t ed25519 -f /root/.ssh/vast_encoder -C vast-hls-encoder
chmod 600 /root/.ssh/vast_encoder
```

Содержимое `/root/.ssh/vast_encoder.pub` добавьте в SSH Keys аккаунта Vast.ai. Приватный `/root/.ssh/vast_encoder` никуда не загружайте.

### 3. Origin directories

```bash
install -d -m 755 /var/www/html/video
```

Пользователь, запускающий orchestrator, должен иметь права создавать каталоги, lock, staging и `abr` внутри `/var/www/html/video/<video_id>/`.

## Требования

### Binary Racks origin

- Linux и Python 3.10+;
- [uv](https://docs.astral.sh/uv/) для управления зависимостями и запуска;
- исходящий HTTPS к `console.vast.ai`;
- исходящий SSH к endpoint, выданному Vast (control-plane: мониторинг, SSH-recovery);
- **входящий** SSH на `--origin-ssh-port` (по умолчанию 22), доступный с временного Vast instance — сюда instance пушит результат напрямую, в обход Vast relay;
- sshd `MaxStartups`/`MaxSessions` должны допускать до `--upload-workers` (по умолчанию 16) параллельных подключений; при дефолтном stagger в 0.5s между стартами это уже проверено на стандартном `MaxStartups 10:30:100`, но если он занижен на конкретном хосте — либо ослабьте его, либо увеличьте `--upload-worker-stagger`. Sshd-конфигурацию Binary Racks orchestrator не трогает автоматически;
- `rsync` и OpenSSH client (и server — для upload push);
- права на `/var/www/html/video` и на редактирование `--origin-authorized-keys` (по умолчанию `~/.ssh/authorized_keys` пользователя, запускающего orchestrator);
- Python packages (ставятся через uv): Requests, Rich и Loguru.

### Vast host/offer

Фильтры по умолчанию:

| Параметр | Значение |
|---|---:|
| GPU | **только RTX 4080** (`--gpus`, без fallback на другую модель) |
| GPU count | 1 |
| Offer type | on-demand |
| Verified | да |
| Reliability | ≥ 0.98 |
| Effective CPU | ≥ 4 cores |
| RAM | ≥ 16384 MB (16 GB) |
| Disk | ≥ 150 GB |
| Disk bandwidth | ≥ 500 MB/s |
| Download speed (`inet_down`) | ≥ 500 Mbps (желательно 1 Gbps+, влияет на ранжирование) |
| Upload speed (`inet_up`) | ≥ 500 Mbps (желательно 1 Gbps+, влияет на ранжирование) |
| CUDA compatibility | ≥ 12.6 |
| Total price | ≤ $0.80/hour |
| Direct SSH ports | ≥ 1 |
| Доступная длительность | boot timeout + job timeout |

GPU allow-list — единственная модель, `RTX 4080` (`core.constants.DEFAULT_GPUS`), выбранная по прямому бенчмарку именно этого ABR pipeline (один decode, `split=4`, `preset p3`): ≈11.5x realtime при ≈90% NVENC utilization, тогда как `L40S` на том же job показала только ≈7.8x. Другие модели (`L4`, `L40`, `L40S`, `RTX 4090`, `A100`, `H100` и т.д.) больше не используются даже как fallback — если подходящего `RTX 4080` offer не нашлось, orchestrator завершается ошибкой `No suitable RTX 4080 instance available`, а не расширяет поиск на другую модель (см. [Поиск, фильтрация и рейтинг машин](#поиск-фильтрация-и-рейтинг-машин)). `--gpus` остаётся обычным CLI-параметром (`nargs="+"`) — можно осознанно переопределить его на другую модель/список вручную, но по умолчанию расширения нет.

## Установка

Проект — полноценный Python-пакет ([`pyproject.toml`](pyproject.toml)), управляемый через [uv](https://docs.astral.sh/uv/). На Binary Racks:

```bash
apt-get update
apt-get install -y rsync openssh-client ca-certificates curl

# uv, если ещё не установлен
curl -LsSf https://astral.sh/uv/install.sh | sh

install -d -m 755 /opt/vast-hls-orchestrator
# скопируйте репозиторий (pyproject.toml, uv.lock, src/, README.md) в /opt/vast-hls-orchestrator
cd /opt/vast-hls-orchestrator
uv sync --frozen --no-dev
```

`uv sync` создаёт `.venv` рядом с проектом и ставит точные версии из `uv.lock`. Проверить, что пакет собирается и импортируется, перед первым запуском:

```bash
uv run python -c "import vast_hls_orchestrator"
```

## Запуск

Команды ниже выполняются **из директории проекта** (там, где лежит `pyproject.toml`) — если вы следовали разделу [Установка](#установка), это `/opt/vast-hls-orchestrator` на Binary Racks:

```bash
cd /opt/vast-hls-orchestrator   # или ваша рабочая копия репозитория
```

Запуск из другой директории — через `uv run --directory <путь-к-проекту> vast-hls-orchestrator ...`.

Обычный запуск:

```bash
export VAST_API_KEY='...'

uv run vast-hls-orchestrator \
  --video-id test \
  --source-url https://origin.example.com/video/test.mp4 \
  --ssh-key /root/.ssh/vast_encoder \
  --origin-ssh-host origin.example.com
```

Расширенные диагностические сообщения:

```bash
uv run vast-hls-orchestrator \
  --video-id test \
  --source-url https://origin.example.com/video/test.mp4 \
  --ssh-key /root/.ssh/vast_encoder \
  --origin-ssh-host origin.example.com \
  --verbose
```

Только поиск и рейтинг offers, без аренды:

```bash
uv run vast-hls-orchestrator \
  --video-id test \
  --source-url https://origin.example.com/video/test.mp4 \
  --ssh-key /root/.ssh/vast_encoder \
  --origin-ssh-host origin.example.com \
  --dry-run
```

`--ssh-key` и `--origin-ssh-host` — обязательные параметры CLI при любом запуске (в том числе `--dry-run`, argparse проверяет их наличие независимо от режима), но сам файл ключа и доступность origin-адреса для dry-run не проверяются. Dry-run требует `VAST_API_KEY` и ничего не создаёт в Vast.

## Поиск, фильтрация и рейтинг машин

Для каждой разрешённой модели GPU отправляется `POST /api/v0/bundles/` с hard filters. Используется тип `on-demand`; bid/interruptible instances не выбираются.

Результаты всех запросов объединяются и дедуплицируются по offer ID. Для каждого offer рассчитывается оценка полной стоимости:

```text
output_gb = input_gb × 1.7

estimated_cost =
    dph_total × expected_hours
  + input_gb  × inet_down_cost
  + output_gb × inet_up_cost
```

- `input_gb` берётся из `Content-Length` source URL;
- если размер неизвестен, для рейтинга принимается 10 GB;
- `expected_hours` по умолчанию равен 0.5 и влияет только на рейтинг;
- множитель 1.7 — консервативная оценка размера полного набора ABR outputs.

Offers сортируются лексикографически по следующему tuple:

1. **максимальное число NVENC-сессий у GPU** (`core.constants.GPU_NVENC_SESSIONS`, см. [Vast host/offer](#vast-hostoffer)) — исторический первый критерий из тех времён, когда allow-list включал несколько моделей разных NVENC-тиров; сейчас allow-list — это только `RTX 4080` (`core.constants.DEFAULT_GPUS`), так что все найденные offers имеют одно и то же значение здесь, и этот пункт tuple не различает их;
2. минимальная `estimated_cost`;
3. минимальная почасовая `dph_total`;
4. максимальная download bandwidth (`inet_down`) — обязательный минимум 500 Mbps уже задан фильтром поиска, здесь это тай-брейкер в пользу более быстрого канала (1 Gbps+ предпочтительнее);
5. максимальная upload bandwidth (`inet_up`) — тот же принцип: 500 Mbps гарантирован фильтром, из прошедших фильтр предпочитается более быстрый — именно через upload-канал instance пушит готовый ABR HLS напрямую на origin (см. [Upload: direct parallel push](#upload-direct-parallel-push-vast--origin));
6. максимальная disk bandwidth.

В terminal выводится таблица пяти лучших кандидатов с отдельными колонками `NVENC` и `Direct`. Поиск требует `direct_port_count >= 1`. Для аренды последовательно рассматриваются первые десять: если offer уже занят или вернул `no_compatible_tag`, берётся следующий — но только среди `RTX 4080`, без расширения на другую модель GPU.

## Создание Vast instance

Лучший доступный offer принимается запросом `PUT /api/v0/asks/<offer_id>/`. Параметры instance:

```text
image:        progeroffline/vast-transcoder:1.1
disk:         40 GB
runtype:      ssh_direct
target_state: running
cancel_unavail: true
env:          -e NVIDIA_DRIVER_CAPABILITIES=all -e NVIDIA_VISIBLE_DEVICES=all
```

`env` — docker flag-format строка (`-e KEY=value ...`), как того требует сам `/asks/` endpoint, а не JSON-объект `{key: value}` (это была реальная ошибка в более раннем коде: значение уходило в API как объект и не применялось так, как задумано). `all` для `NVIDIA_DRIVER_CAPABILITIES` — то же самое значение, что и в собственном Vast-шаблоне образа ("HLS Transcoder"), провалидированное там на реальном железе: Dockerfile образа сам по себе задаёт лишь `compute,utility`, без `video`, необходимого для NVENC/NVDEC.

### Docker image: `progeroffline/vast-transcoder`

По умолчанию (`--image`, переопределяется через `VAST_IMAGE`) используется собственный образ проекта — [`progeroffline/vast-transcoder`](https://hub.docker.com/r/progeroffline/vast-transcoder), тот же, что зарегистрирован в приватном Vast-шаблоне ["HLS Transcoder"](https://cloud.vast.ai/?template_id=adf45ab182295032e068198e37c4788e) (`hash_id=adf45ab182295032e068198e37c4788e`, получен через `GET /api/v0/template/?select_filters={"hash_id":{"eq":...}}`). База — `nvidia/cuda:12.6.3` Ubuntu 24.04, поверх неё собран свой `ffmpeg` (в `/opt/ffmpeg/bin`, уже в `PATH` образа) с `h264_nvenc`/`hevc_nvenc`/`av1_nvenc`, `cuvid`-декодерами и `scale_cuda`, плюс `aria2c`, `curl`, `ca-certificates`. Поэтому `onstart` (см. [Bootstrap и remote job](#bootstrap-и-remote-job)) больше не ставит `ffmpeg`/`aria2`/`curl`/`ca-certificates` через `apt-get` — только `rsync`, которого в образе нет — а remote job теперь сам запускает `rsync`-клиент, пушащий результат наружу на origin (см. [Upload: direct parallel push](#upload-direct-parallel-push-vast--origin)), так что без него upload-стадия не сможет стартовать вообще.

Образ не заменяет и не отменяет запись SSH-ключа через `onstart` (см. ниже) — свой `authorized_keys` он не пишет и не знает публичного ключа заранее, эта часть архитектуры не изменилась.

**Известное ограничение образа**: `ENTRYPOINT`/`CMD` образа (`tini -- /usr/local/bin/start.sh`) сам по себе делает GPU/NVENC/NVDEC/`scale_cuda` preflight и затем `exec sleep infinity`, чтобы удержать контейнер живым; это отдельный процесс, параллельный orchestrator'овскому `onstart` (Vast выполняет `onstart` через отдельный exec, а не вместо image `CMD`) и на сам `onstart`/job не влияет. Но если preflight внутри `start.sh` сам завершится с ошибкой (`exit 10/11/12` — до `exec sleep infinity`), это может уронить весь контейнер как PID 1 под `tini`, ещё до того, как `onstart` вообще успеет отработать — тогда SSH не поднимется не из-за проблемы с ключом, а потому что контейнера уже нет, и SSH-recovery (reboot + retry, см. ниже) будет упираться в тот же самый preflight по кругу. На практике это тот же набор GPU-проверок, что и в собственном preflight `job_script.py` (см. [Проверка GPU и диска](#проверка-gpu-и-диска)) — то есть на офере, прошедшем поиск/фильтры, до этого дело обычно не доходит, но если это когда-то случится систематически, это повод поправить `start.sh` в самом образе (например, не завершаться с ошибкой, а тоже уходить в `sleep infinity`), а не что-то, что можно обойти со стороны orchestrator.

Также передаются уникальный label и `onstart` script. Label используется не только для удобства: если TCP-соединение оборвалось после PUT и неизвестно, создал ли Vast instance, orchestrator ищет instance через `GET /api/v1/instances` с точным label. До завершения reconciliation новый offer не арендуется — это защищает от «потерянного» платного instance.

API retries:

- transport errors, 429 и 5xx на безопасных read/delete запросах используют exponential backoff;
- 401/403 немедленно завершают pipeline как ошибка credentials/permissions;
- create PUT никогда вслепую не повторяется после неоднозначного transport/5xx результата;
- 429 create можно повторить, поскольку сервер явно отклонил запрос по rate limit;
- исчезновение instance или bad state считается fatal.

### SSH-ключ пишется в контейнер напрямую, а не через аккаунтную инъекцию Vast

Аккаунтные SSH-ключи (Console → Keys) по документации Vast.ai должны подставляться в любой новый instance автоматически. На практике это оказалось ненадёжно: `GET /api/v0/instances/<id>/ssh` может подтверждать, что нужный ключ числится у Vast для конкретного instance, а реальный `authorized_keys` внутри контейнера при этом его не содержит — расхождение между записью Vast и фактическим состоянием контейнера, воспроизведённое и подтверждённое на реальной аренде. Именно так выглядит ситуация "через консоль заходит, через API/оркестратор нет" даже при полностью корректном ключе и аккаунте, и полагаться на аккаунтную инъекцию или на ответ Vast API как на единственный источник истины небезопасно.

Поэтому единственная надёжная гарантия — записать ключ самим. `onstart` (см. ниже) первым делом, ещё до `apt-get` и до всего остального, выполняет с root-правами внутри контейнера:

```bash
mkdir -p /root/.ssh && chmod 700 /root/.ssh
printf '%s\n' "<публичная часть --ssh-key>" > /root/.ssh/authorized_keys.new
chmod 600 /root/.ssh/authorized_keys.new
mv -f /root/.ssh/authorized_keys.new /root/.ssh/authorized_keys
```

Файл каждый раз пересобирается с нуля (truncate + atomic `mv`), а не дополняется по `grep -qxF`: это делает шаг идемпотентным точно так же, как и append-if-missing, но вдобавок автоматически лечит любой сторонний/повреждённый `authorized_keys` при следующем старте контейнера — что и используется SSH-recovery (см. ниже). Публичная часть берётся из `<--ssh-key>.pub`, а если такого файла нет — извлекается из приватного ключа через `ssh-keygen -y -f <--ssh-key>` (ключ должен быть без passphrase, как и для всех остальных non-interactive SSH-операций); без публичного ключа orchestrator не арендует instance вообще. Никаких дополнительных вызовов Vast API для привязки/проверки ключа нет.

### SSH-recovery: reboot instance, если SSH не поднялся

Vast сообщает два независимых SSH-маршрута:

- **direct**: `public_ipaddr` + `ports["22/tcp"][0]["HostPort"]`;
- **proxy/relay**: `ssh_host` + `ssh_port`.

Orchestrator сначала проверяет direct endpoint и только при его недоступности использует proxy. `ssh_port` никогда не соединяется с `public_ipaddr`. Во время monitoring сохраняется уже работающий маршрут; если он ломается, второй проверяется немедленно до начала общего reconnect timeout. SSH compression явно отключён, как и в bulk rsync.

Если оба маршрута не проходят в течение окна ожидания (`orchestration/provisioning.wait_for_ssh_with_recovery`), orchestrator не сдаётся сразу, а:

1. вызывает `PUT /api/v0/instances/reboot/<id>/` — стоп/старт того же контейнера без потери GPU-аренды (в отличие от destroy/re-rent);
2. это заново прогоняет `onstart`, а значит и переписанный с нуля блок записи ключа выше — эффективно "убрать и заново добавить" сам ключ, без необходимости SSH-сессии, чтобы сделать это вручную;
3. заново ждёт `actual_status=running` (`wait_for_running`), затем сбрасывает локальный `known_hosts` для этого job — свежий старт контейнера может пересоздать host key sshd, а `StrictHostKeyChecking=accept-new` доверяет только *новому* хосту и откажет, если для уже записанного хоста ключ поменялся;
4. снова проверяет direct, затем proxy.

Цикл повторяется до нескольких раз подряд; если SSH так и не поднялся — job завершается ошибкой с диагностикой. Поскольку Vast reboot API не принимает параметров, каждый reboot заново гоняет тот же самый `onstart` — двух reboot подряд («убрать / перезапустить / добавить / перезапустить» как раздельные шаги) не требуется: перезапись ключа с нуля уже происходит атомарно при каждом отдельном reboot.

## Bootstrap и remote job

Vast может передать onstart оболочке `/bin/sh`, поэтому внешний onstart содержит только POSIX-совместимое декодирование payload и явно запускает `/bin/bash`.

Bootstrap:

1. пишет SSH-ключ в `/root/.ssh/authorized_keys` (см. выше) — раньше всего остального;
2. создаёт `/workspace` и stage/status-файлы;
3. перенаправляет stdout/stderr через `tee` в `/workspace/bootstrap.log`;
4. запускает self-destroy watchdog;
5. выполняет `apt-get update`;
6. устанавливает `rsync` (единственный отсутствующий в образе бинарь — `ffmpeg`, `aria2c`, `curl`, `ca-certificates` уже в `progeroffline/vast-transcoder`, см. [Docker image](#docker-image-progeroffline-vast-transcoder));
7. декодирует `/workspace/encode-job.sh` из gzip+Base64;
8. запускает job через `nohup`, вывод направляет в `/workspace/job.log`.

И сам `encode-job.sh` (внутренний payload), и весь bootstrap-скрипт целиком (внешняя обёртка, которую Vast получает как значение `onstart`) сжимаются gzip перед Base64 — на remote-стороне соответственно `base64 -d | gunzip`. Base64 здесь не является шифрованием: он нужен только для надёжной передачи многострочного Bash через JSON/API без разрушения quoting; secrets в payload отсутствуют. gzip — не про секретность, а про размер: `PUT /api/v0/asks/<id>/` реально отклоняет запрос с `HTTP 400 "Invalid args: ... len(args) > 16384"`, если результирующий onstart-payload превышает этот лимит (воспроизведено на реальном API), а обычный Base64 без сжатия для этого job-скрипта уже вплотную подходит к границе. `docs.vast.ai` сам называет gzip+Base64 решением для большого `onstart`.

Если bootstrap падает до запуска job, trap всё равно создаёт `JOB_EXIT`, переводит stage в `bootstrap-failed` и создаёт `JOB_DONE`, поэтому origin не ждёт до полного timeout.

Ephemeral deploy-ключ (для upload-стадии, см. [Upload: direct parallel push](#upload-direct-parallel-push-vast--origin)) в этот `onstart`-payload не входит — origin пушит его отдельно, уже после того как admin SSH подтверждён живым (`orchestration/deploy_key.py::install_private_key_on_remote`), напрямую в `/root/.ssh/vast_deploy_key` через stdin того же SSH-канала. Так приватный материал не проходит через Vast API вообще, даже в сжатом виде.

## Загрузка исходного видео

Remote job использует [aria2](https://aria2.github.io/) с несколькими HTTP Range connections:

```text
connections:            16
split streams:          16
piece size:             8 MiB
automatic retries:      8
retry delay:            3 seconds
connect timeout:        20 seconds
transfer timeout:       30 seconds
file allocation:        none
```

Файл сохраняется как `/workspace/input/source.mp4`. Перед загрузкой проверяется ожидаемый размер из origin HEAD; после загрузки обязательно проверяются ненулевой размер и корректная duration через `ffprobe`.

## Проверка GPU и диска

После загрузки `ffprobe` получает точную duration. На её основе оценивается объём HLS:

```text
required ≈ duration × 13.2 Mbit/s ÷ 8 × 1.15 + 2 GiB reserve
```

При недостатке места job останавливается до encoding.

GPU preflight включает:

1. `nvidia-smi`;
2. проверку наличия encoder `h264_nvenc`;
3. проверку CUDA filter `scale_cuda`;
4. тестовое двухсекундное кодирование реального source **той же формы**, что и реальный encode — один decode, `split` на 4 ветки, 4 параллельных `h264_nvenc`:

```text
NVDEC decode → split → 4× scale_cuda → 4× h264_nvenc (параллельно) → null muxer
```

Проверки `h264_nvenc`/`scale_cuda` захватывают вывод `ffmpeg -encoders`/`-filters` в переменную и ищут паттерн через `grep -q ... <<< "$var"`, а не через `producer | grep -q`. Прямой pipe в `grep -q` под `set -o pipefail` ломается даже при УСПЕШНОМ совпадении: `grep -q` завершается сразу по первому найденному совпадению, ffmpeg получает `SIGPIPE` на попытке дописать оставшийся вывод, и pipefail засчитывает это как код `141` — job падает на ровном месте, хотя NVENC/scale_cuda реально присутствуют.

Таким образом проверяется не только наличие названий в `ffmpeg -encoders/-filters`, но и фактическая совместимость драйвера, FFmpeg, decode, CUDA scaling и NVENC.

## FFmpeg и ABR HLS

Один FFmpeg-процесс декодирует source **один раз** через NVDEC, затем делит кадры на GPU (`split`) на четыре ветки, каждая масштабируется отдельным `scale_cuda` и кодируется отдельным `h264_nvenc` — вместо четырёх независимых процессов, каждый из которых заново decode'ил бы весь source с нуля. Это официально документированный NVIDIA/FFmpeg паттерн 1:N transcode:

```text
source.mp4
     │
     ▼
   NVDEC (один раз)
     │
     ├──────────► scale_cuda → 1080p → NVENC → HLS
     ├──────────► scale_cuda → 720p  → NVENC → HLS
     ├──────────► scale_cuda → 480p  → NVENC → HLS
     └──────────► scale_cuda → 360p  → NVENC → HLS
```

| Rendition | Resolution | Video | Maxrate | Bufsize | Audio | CQ |
|---|---:|---:|---:|---:|---:|---:|
| 1080p | 1920×1080 | 6500k | 7150k | 13000k | AAC 160k | 19 |
| 720p | 1280×720 | 3500k | 3850k | 7000k | AAC 128k | 20 |
| 480p | 854×480 | 1800k | 2000k | 3600k | AAC 128k | 21 |
| 360p | 640×360 | 900k | 1000k | 1800k | AAC 96k | 22 |

Общие параметры:

- один `-hwaccel cuda -hwaccel_output_format cuda` decode на весь job;
- `[0:v]split=4[...]`, затем `scale_cuda` на каждую ветку — CUDA-фреймы никогда не возвращаются в system RAM;
- `h264_nvenc`, preset `p3`, tune `hq`, rate control `vbr`, свой `-cq`/битрейт/аудио на каждый output;
- forced keyframe/IDR каждые 6 секунд — на каждый output отдельно, без stream-index суффиксов (`-force_key_frames`/`-forced-idr` позиционно scoped на "текущий" output, что явно проверено — суффикс `:N` для этого не нужен и не всегда предсказуем);
- HLS VOD, `hls_time=6`, `independent_segments`;
- optional первый audio stream, замаплен в каждый output отдельно (`0:a:0?`);
- stdout/stderr всего процесса — один общий `/workspace/out/ffmpeg.log` (не по одному на rendition, как раньше).

Структура remote output:

```text
/workspace/out/
├── master.m3u8
├── ffmpeg.log
├── progress.txt
├── 1080p/
│   ├── index.m3u8
│   └── segment_00000.ts ...
├── 720p/
├── 480p/
└── 360p/
```

Если процесс завершается с ошибкой, job сразу печатает tail единого `ffmpeg.log` и падает — до этого никакие rendition ещё не начинали писаться по отдельности, отменять нечего (всё было одним процессом). Master playlist создаётся только после успеха и проверки `#EXT-X-ENDLIST`/segments для всех четырёх.

### GPU preflight проверяет именно эту схему

Двухсекундный preflight-тест (см. [Проверка GPU и диска](#проверка-gpu-и-диска)) теперь гоняет тот же `split` + 4× `scale_cuda` + 4× `h264_nvenc`, что и реальный encode — а не одну ветку, как раньше. Если GPU физически не тянет несколько NVENC-сессий сразу, это выяснится за 2 секунды, а не спустя часы реального job.

### GPU allow-list: только RTX 4080, по прямому бенчмарку

`--gpus`/`core.constants.DEFAULT_GPUS` — только `RTX 4080`, без fallback на другую модель (см. [Vast host/offer](#vast-hostoffer)). Это не оценка по абстрактному числу NVENC-движков, а результат прямого замера именно на этом ABR pipeline (один decode, `split=4`, `preset p3 tune hq`): `RTX 4080` — ≈11.5x realtime при ≈90% NVENC utilization; `L40S` на том же job — только ≈7.8x. `L4`, `L40`, `L40S`, `RTX 4090`, `A100`, `H100` и другие модели больше не рассматриваются автоматически, даже если бы формально прошли остальные фильтры (`GPU_NVENC_SESSIONS`, откуда исторически шло более широкое ранжирование по числу NVENC-движков — см. [Поиск, фильтрация и рейтинг машин](#поиск-фильтрация-и-рейтинг-машин) — остался в коде как факт про железо, но с allow-list из одной модели он больше ничего не решает). Если подходящего `RTX 4080` offer нет, orchestrator завершается ошибкой `No suitable RTX 4080 instance available`, а не расширяет поиск.

## Прогресс и логи

### FFmpeg progress

Обычный stderr `-stats` не парсится. Единственный ABR-процесс получает один `-progress` endpoint через FIFO — так как все четыре rendition кодируются из одного decode, у FFmpeg нет отдельного `frame=`/`out_time=`/`speed=` на каждый output (multi-output `-progress` репортит их агрегированно на весь процесс; отдельные есть только `stream_N_0_q`, качество на поток), так что единый прогресс — не потеря информации, а точное отражение того, что все rendition физически идут в одном темпе. Relay на remote стороне собирает полный record до строки `progress=...`, записывает временный файл и делает `mv` в `progress.txt`. Origin поэтому не читает наполовину записанные records.

Хвост `bootstrap.log`/`job.log` для панели "Remote log tail" читается через `tail -c 4000 <файл>` для каждого файла, а не через `cat` целиком — во время download aria2 непрерывно дописывает в `job.log` (прогресс каждую секунду), и `cat` растущего файла на **каждом** опросе (`--monitor-interval`) со временем всё дольше идёт по SSH; `tail -c` не сканирует файл целиком независимо от его размера.

Поля `frame`, `fps`, `out_time_us`, `bitrate`, `speed` и `progress` опрашиваются по SSH. Процент рассчитывается как:

```text
percentage = out_time / ffprobe_duration × 100
ETA        = (duration - out_time) / speed
```

### Обычный последовательный вывод, без постоянного полноэкранного режима

Orchestrator — обычный CLI-инструмент: вывод идёт построчно в scrollback терминала, ничего не перерисовывается поверх себя и не занимает терминал целиком. Полноэкранный alternate-screen режим (как у `htop`) в проекте пробовали, но он оказался источником нестабильности (зависания/визуальные глюки при просмотре через SSH-сессию на сам сервер) без сопоставимой пользы — поэтому от него отказались в пользу простого лога.

`console.status(...)` (короткий спиннер) используется только на время одной конкретной операции, где ожидание может затянуться: поиск offers, ожидание `running`, ожидание живого SSH. Каждый спиннер сам исчезает, как только операция завершается, и не оставляет постоянного состояния после себя.

### Прогресс encoding — периодические строки лога

Пока идёт encoding, каждые `--monitor-interval` секунд orchestrator опрашивает удалённую машину по SSH (stage, размер скачанного файла, `-progress` FFmpeg, `nvidia-smi`), но печатает сводку не на каждый опрос, а не чаще раза в 10 секунд — иначе короткий `--monitor-interval` превратился бы в спам логов. Строка выглядит так:

```text
Progress: stage=encoding  download=100.0% (9.4 GiB/9.4 GiB)  media=00:02:14/00:10:00  fps=48.2  speed=1.35x  cost=$0.0142
```

`cost` — это `цена offer ($/h) × время с момента фактической аренды instance / 3600`, то есть накопленные расходы на **этот** instance к текущему моменту (а не оценка на весь job). Отсчёт времени идёт с момента успешного создания instance (`rent_instance`), а не с начала мониторинга encoding — provisioning, bootstrap и download тоже платные и должны входить в сумму. При завершении job (успех, `Ctrl+C` или сбой) тот же расчёт печатается как `Total cost` — оплата идёт независимо от результата, поэтому строка появляется во всех трёх случаях.

На стадии `stage=uploading` строка выглядит иначе:

```text
Progress: stage=uploading  uploaded=57.4% (5.8 GiB/10.1 GiB)  speed=27.1 MiB/s  workers=11/16  eta=00:02:45  cost=$0.0891
```

`uploaded` origin считает **сам**, локально, обходом staging-каталога (`orchestration/publish.py::staging_bytes_on_disk`, реальные disk blocks, а не apparent size — тот же принцип, что и у `download=` при скачивании source) — никакого дополнительного SSH round-trip на каждый байт не нужно, поскольку instance пушит именно в тот каталог, которым origin уже владеет локально. По SSH-polling (тот же relay-эндпоинт, что и раньше) забираются только две дешёвые вещи, которые origin сам узнать не может: общий ожидаемый объём (`total_bytes`, посчитан remote-стороной сразу после batching) и число уже завершившихся workers. `speed`/`eta` считаются по двум последовательным замерам `uploaded` между печатями строки (раз в те же 10 секунд). Проценты, как и у `download=`, заклэмплены сверху в 100% — реальные disk blocks могут по тем же причинам (выравнивание по блокам ФС при параллельной записи) слегка превышать заявленный `total`.

Переходы стадии (`stage=download` → `stage=encoding` → `stage=finalizing` → `stage=uploading` → ...) и статуса логируются сразу, отдельной строкой, а не только в периодической сводке. `SUCCESS Encoder job completed successfully` больше не печатается сразу после того, как remote script завершится (это означало только "encode+upload на стороне Vast прошли", а не "весь pipeline готов") — теперь это `INFO`-строка, а финальный `logger.success("SUCCESS")` печатается только после локального atomic publish на origin, то есть после того, как результат реально и безопасно доступен по итоговому URL.

### Loguru

[Loguru](https://loguru.readthedocs.io/) направляется в собственный sink, печатающий прямо в терминал:

- `INFO` — stages, выбранный offer, endpoint, transfer lifecycle, периодический progress;
- `SUCCESS` — SSH ready, encode complete, publish и destroy;
- `WARNING` — transient API/SSH/rsync failures и recovery;
- `ERROR` — понятная причина окончательного сбоя;
- `DEBUG` — API attempts, remote commands и remote log changes; включается `--verbose`.

API keys и private key contents не логируются.

## Получение и публикация результата

### Upload: direct parallel push (Vast → origin)

Раньше bulk-транспорт (`.ts`-сегменты, playlists, master) шёл через `rsync pull`: origin сам подключался к Vast через SSH endpoint и забирал `/workspace/out/` одним потоком — по факту ≈3.5–4 MiB/s, а через `sshN.vast.ai` relay мог падать до 2–3 MiB/s. Прямой outbound push с самого instance при 16 параллельных потоках даёт на порядок больше (замерено отдельно: ≈28 MiB/s / ≈226 Mbps на 16 потоках против ≈3.5 MiB/s на одном). Поэтому bulk transfer теперь идёт в обратную сторону — remote job сам пушит результат на публичный SSH Binary Racks. Vast direct/proxy endpoints используются только для control-plane; proxy является fallback, а не путём передачи HLS.

После `finalizing` remote job (`remote/job_script.py`) переходит в `set_stage uploading` и делает следующее:

Upload намеренно начинается после успешного завершения единого FFmpeg-процесса. Передача сегментов одновременно с encoding потребовала бы отдельного watcher-протокола, который достоверно отличает закрытый `.ts` от ещё записываемого и согласует четыре изменяющихся playlist; ошибка здесь способна опубликовать усечённый сегмент. Узкое место relay устранено direct push и параллельными workers без вмешательства в проверенный encoding lifecycle.

1. Строит манифест всех `.ts` сегментов (`find "$OUT" -name 'segment_*.ts' -printf '%s %P\n'`) — размер + относительный путь (`1080p/segment_00000.ts` и т.п.).
2. Балансирует их по **суммарному байтовому размеру**, не по количеству файлов и не по одному worker'у на rendition (у renditions сильно разный размер — naive-разбиение оставило бы 1080p как одинокий медленный поток, пока остальные уже закончили). Алгоритм — greedy LPT (largest-first в самый лёгкий на данный момент batch), реализован как отдельный чистый модуль `remote/upload_batching.py::plan_batches()`, embedded в generated bash буквально (единственный источник истины, покрыт юнит-тестами напрямую как Python).
3. Запускает до `--upload-workers` (по умолчанию 16) параллельных `rsync`-воркеров с задержкой `--upload-worker-stagger` (по умолчанию 0.5s) между стартами — 16 одновременных SSH-подключений упираются в `MaxStartups`/`MaxSessions` sshd на origin (`kex_exchange_identification: read: Connection reset by peer`), stagger снимает это без изменения sshd-конфигурации на origin.
4. Каждый worker — `rsync -a --whole-file --partial --partial-dir=.rsync-partial --files-from=<batch>` с `Compression=no` (`.ts` уже сжат) и `-c aes128-gcm@openssh.com`. Неудачный batch повторяется независимо от остальных — до `--rsync-retries` попыток (тот же флаг, что раньше был у pull; семантика "resumable transfer attempts" не изменилась) с тем же backoff `min(2**attempt, 10)`, что и везде в проекте. Успешные batches не передаются повторно.
5. После **всех** сегментов — 4 playlist (`<rendition>/index.m3u8`).
6. Последним — `master.m3u8`, под именем **`master.m3u8.tmp`** в staging-каталоге (не `master.m3u8`).
7. Если хоть один batch не прошёл после всех retries — `exit` с ненулевым кодом; `finish()` (единый EXIT trap, см. [Bootstrap и remote job](#bootstrap-и-remote-job)) корректно помечает `JOB_STAGE=failed`. Поскольку master пушится строго последним, при любом сбое раньше он физически не может оказаться на origin.

Staging-каталог (`abr.staging.<job_token>`, где `job_token` — тот же токен, что и у `create_label`; создан origin'ом заранее, **до** аренды instance — `job_script.py` должен знать путь ещё на этапе сборки, а `instance_id` тогда ещё не существует) не публичен, пока не произойдёт atomic exchange ниже — клиент физически не может увидеть новый `master.m3u8`, у которого ещё нет части renditions.

После того как remote job завершился успешно (`JOB_DONE`/`JOB_EXIT=0`), origin (`orchestration/publish.py::finalize_push_publish`) **не делает никакого сетевого transfer'а** — он уже случился на стороне Vast. Origin только:

- проверяет `master.m3u8.tmp`, все четыре `index.m3u8`, хотя бы один непустой `segment_*.ts` на rendition;
- локально переименовывает `staging/master.m3u8.tmp → staging/master.m3u8`;
- делает существующий atomic publish (см. ниже) — не изменился.

На Linux существующий `abr` и новый staging меняются местами одним `renameat2(RENAME_EXCHANGE)`. Старый `abr`, оказавшийся в staging, переносится в backup и удаляется. Если filesystem не поддерживает exchange, применяется совместимый fallback:

```text
abr → abr.backup.<job_token>
staging → abr
delete backup
```

При старте следующего job orchestrator восстанавливает backup после прерванного publish и очищает stale staging/backup (`abr.staging.*`/`abr.backup.*` — те же glob'ы, что и раньше, просто теперь под `job_token`, а не `instance_id`). Все rename выполняются внутри одного filesystem.

Итоговый URL:

```text
https://<origin>/video/<video_id>/abr/master.m3u8
```

## Cleanup, watchdog и обработка ошибок

### Обязательный local cleanup

`instance_id` сохраняется сразу после подтверждённого create или reconciliation. Основной pipeline обёрнут в `try/finally`; `finally` вызывает:

```http
DELETE /api/v0/instances/<instance_id>/
```

DELETE повторяется при transient 429/5xx. HTTP 404 означает, что instance уже удалён.

### Failsafe watchdog

Watchdog стартует до `apt-get`. После `--failsafe-seconds` он использует автоматически внедрённые `CONTAINER_API_KEY` и `CONTAINER_ID`, затем повторяет DELETE раз в минуту до успеха. Он нужен на случай SIGKILL, kernel panic или полной потери origin.

### Ctrl+C

При первом Ctrl+C:

1. активный rsync (обычный foreground subprocess) получает SIGINT от терминала вместе с самим orchestrator;
2. управление переходит в `except KeyboardInterrupt` / Python `finally`;
3. Vast instance удаляется;
4. процесс возвращает exit code `130`.

### Диагностика ошибок

При remote failure orchestrator запрашивает и показывает в terminal:

- tail `/workspace/bootstrap.log`;
- tail `/workspace/job.log`;
- tail единого `/workspace/out/ffmpeg.log` (весь ABR-процесс пишет в один файл).

Обрабатываются, среди прочего:

- invalid/expired API key и недостаточные permissions;
- Vast rate limiting и server errors;
- unavailable offer и `no_compatible_tag`;
- неоднозначный create response;
- provisioning timeout и исчезновение instance;
- позднее появление/изменение SSH endpoint;
- SSH так и не поднялся после старта instance (reboot + повторная попытка, до нескольких циклов);
- кратковременные SSH disconnects;
- aria2/source failure;
- недостаток remote disk space;
- отсутствие NVENC/NVDEC/`scale_cuda`;
- сбой ABR-процесса (единый decode+split+encode);
- job timeout;
- сбой отдельного upload batch (retry только этого batch, независимо от остальных; persistent failure после всех попыток — весь job failed, master не публикуется);
- неполный HLS result;
- stale staging/backup;
- параллельный job с тем же `video_id`.

## Параметры командной строки

| Параметр | Default | Назначение |
|---|---:|---|
| `--video-id` | required | Безопасный ID и имя origin-каталога |
| `--source-url` | required | Публичный HTTP(S) MP4 URL |
| `--origin-root` | `/var/www/html/video` | Корень публикации |
| `--ssh-key` | **обязателен** | Локальный private key для Vast SSH; публичная часть должна быть заранее добавлена в тот же аккаунт Vast.ai, чей `VAST_API_KEY` используется |
| `--known-hosts` | `/root/.ssh/vast_known_hosts` | Базовое имя ephemeral known-hosts file |
| `--image` | `progeroffline/vast-transcoder:1.1` | Vast Docker image (переопределяется через `VAST_IMAGE`) |
| `--disk-gb` | `150` | Размер instance disk; также минимальный `disk_space` в поиске offers |
| `--max-hourly` | `0.80` | Максимальная цена offer |
| `--min-reliability` | `0.98` | Минимальная reliability |
| `--min-cpu` | `4` | Effective CPU cores |
| `--min-ram-mb` | `16384` | RAM в MB |
| `--min-disk-bw` | `500` | Disk bandwidth MB/s |
| `--min-download-mbps` | `500` | Минимальный `inet_down`, Mbps; 1 Gbps+ предпочтительнее (тай-брейкер ранжирования) |
| `--min-upload-mbps` | `500` | Минимальный `inet_up`, Mbps; 1 Gbps+ предпочтительнее (тай-брейкер ранжирования) |
| `--expected-hours` | `0.5` | Время только для рейтинга cost |
| `--boot-timeout` | `600` | Provisioning timeout, seconds |
| `--job-timeout` | `10800` | Remote job timeout, seconds |
| `--failsafe-seconds` | `14400` | Задержка self-destroy watchdog |
| `--monitor-interval` | `1.5` | Частота SSH telemetry polling |
| `--ssh-reconnect-timeout` | `180` | Допустимая длительность SSH outage |
| `--rsync-retries` | `4` | Число resumable transfer attempts на **каждый** upload batch независимо |
| `--origin-ssh-host` | **обязателен** | SSH-адрес origin, доступный снаружи — куда Vast пушит результат напрямую. Автоопределения нет (NAT/несколько интерфейсов/DNS/нестандартный порт) |
| `--origin-ssh-port` | `22` | Порт для `--origin-ssh-host` |
| `--origin-ssh-user` | текущий OS-пользователь | От чьего имени Vast пушит на origin; можно указать выделенного ограниченного deploy-пользователя |
| `--origin-authorized-keys` | `~/.ssh/authorized_keys` | Куда дописывается/удаляется ephemeral deploy public key |
| `--upload-workers` | `16` | Параллельных rsync push-воркеров (1–64) |
| `--upload-worker-stagger` | `0.5` | Секунд между стартом воркеров — иначе часть соединений может упасть под `MaxStartups` origin sshd |
| `--gpus` | `RTX 4080` (см. [Vast host/offer](#vast-hostoffer)) | Разрешённые GPU names; без явного override — расширения на другую модель нет |
| `--verbose` | off | DEBUG logging |
| `--dry-run` | off | Только search/ranking, без аренды |

Полный актуальный список:

```bash
uv run vast-hls-orchestrator --help
```

## Библиотеки и внешние инструменты

### Python

- [Requests](https://requests.readthedocs.io/) — HTTPS-клиент Vast API и HEAD source URL.
- [Rich](https://rich.readthedocs.io/) — styled log output, spinners (`console.status`) и таблица offers.
- [Loguru](https://loguru.readthedocs.io/) — structured severity logging и exception output.
- [argparse](https://docs.python.org/3/library/argparse.html) — CLI.
- [subprocess](https://docs.python.org/3/library/subprocess.html) — безопасный запуск `ssh` и `rsync` без shell для локальных commands.
- [fcntl](https://docs.python.org/3/library/fcntl.html) — межпроцессный file lock.
- [ctypes](https://docs.python.org/3/library/ctypes.html) — Linux `renameat2(RENAME_EXCHANGE)`.

### Remote/system tools

- [Vast.ai API](https://docs.vast.ai/api-reference/introduction) — lifecycle GPU instances.
- [Search offers](https://docs.vast.ai/api-reference/search/search-offers) — marketplace filters.
- [Create instance](https://docs.vast.ai/api-reference/instances/create-instance) — принятие offer.
- [Show instance](https://docs.vast.ai/api-reference/instances/show-instance) — provisioning/endpoint state.
- [Show instances v1](https://docs.vast.ai/api-reference/instances/show-instances) — reconciliation по label.
- [Reboot instance](https://docs.vast.ai/api-reference/instances/reboot-instance) — stop/start контейнера на месте; используется SSH-recovery для повторного прогона `onstart`.
- [Destroy instance](https://docs.vast.ai/api-reference/instances/destroy-instance) — окончательное удаление.
- [Vast Docker environment](https://docs.vast.ai/guides/instances/docker-environment) — `CONTAINER_API_KEY` и `CONTAINER_ID`.
- [FFmpeg](https://ffmpeg.org/documentation.html) — ffprobe, NVDEC, CUDA filters, NVENC и HLS muxer.
- [FFmpeg progress protocol](https://ffmpeg.org/ffmpeg.html#toc-Advanced-options) — machine-readable `-progress`.
- [aria2](https://aria2.github.io/) — parallel Range download.
- [rsync](https://rsync.samba.org/documentation.html) — параллельный resumable push результата с Vast на origin.
- [OpenSSH](https://www.openssh.com/manual.html) — non-interactive remote monitoring and transport.
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/) — GPU exposure и driver capabilities на host side.

## Эксплуатационные рекомендации

1. Сначала запускайте `--dry-run` и проверяйте реальные цены/availability.
2. Держите `--failsafe-seconds` больше `--job-timeout` с запасом на rsync и диагностику.
3. Не запускайте orchestrator из web request process; используйте systemd service/queue worker.
4. Ограничьте доступ к `VAST_API_KEY`, private key и systemd EnvironmentFile правами `0600`.
5. Настройте alert на ошибку DELETE instance: watchdog является дополнительной защитой, а не заменой мониторинга billing.
6. Контролируйте свободное место origin: remote disk проверяется автоматически, origin disk — ответственность оператора.
7. Тестируйте HLS playback и nginx MIME types (`application/vnd.apple.mpegurl`, `video/mp2t`) после первого deploy.
8. Перед изменением bitrate ladder или `--gpus` (allow-list сейчас — только `RTX 4080`, выбранная по прямому бенчмарку) прогоните тот же бенчмарк на новой карте/конфигурации, а не полагайтесь на теоретическое число NVENC-движков.
9. Убедитесь, что `--origin-ssh-host` реально доступен снаружи (проверьте с любой внешней машины, не только с самого origin) — если Vast instance не сможет достучаться до него, upload-стадия будет проваливаться на каждой попытке, и job будет падать уже после (платного) encode.
10. Для более строгой изоляции deploy-ключа заведите отдельного ограниченного OS-пользователя на origin (только права на конкретный `--origin-root`, без shell-доступа) и укажите его через `--origin-ssh-user`/`--origin-authorized-keys` — по умолчанию используется тот же пользователь, что запускает orchestrator, что проще для старта, но менее строго изолировано. SSH `command=`-форсинг под конкретный `rsync --server` в этой версии не реализован (см. [Ephemeral deploy-ключ](#ephemeral-deploy-ключ-rootsshvast_deploy_key-vast--origin-только-для-push)) — это следующий шаг усиления, если понадобится.
