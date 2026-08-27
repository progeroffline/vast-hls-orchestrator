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
- [Live UI, progress и логи](#live-ui-progress-и-логи)
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
  │  3. NVDEC → scale_cuda → NVENC создаёт 4 HLS rendition
  │
  ▲  4. Origin сам подключается по SSH и делает rsync pull
  │
Binary Racks staging → atomic publish /video/<video_id>/abr/
  │
  └─ 5. DELETE Vast instance в обязательном finally
```

Vast instance никогда не подключается обратно к origin по административному SSH и не получает приватный ключ Binary Racks. Направление передачи результата — **pull с origin**, а не push с Vast.

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
│   ├── logging_setup.py            # Loguru → Rich sink / панель Log активного TUI
│   ├── tui_state.py                 # registry активного TuiApp (без зависимости core → ui)
│   ├── models.py                     # dataclasses: VariantProgress, RemoteSnapshot, DashboardContext
│   └── validation.py                  # validate_inputs() для CLI-аргументов
│
├── vast_api/                  # HTTP-клиент Vast.ai marketplace
│   ├── client.py                 # VastClient: search/create/show/destroy instance
│   └── offers.py                  # оценка размера source, ranking, таблица offers (без вывода)
│
├── remote/                    # всё, что выполняется на/через временный Vast instance
│   ├── job_script.py             # bash-скрипт: download, GPU preflight, параллельный ABR encode
│   ├── onstart.py                  # bootstrap (`onstart`): watchdog, apt-get, запуск job
│   ├── ssh.py                       # non-interactive ssh_run/wait_for_ssh
│   └── snapshot.py                   # парсинг FFmpeg `-progress` и nvidia-smi по SSH
│
├── ui/                         # единое full-screen TUI-приложение (Rich)
│   ├── app.py                    # TuiApp: постоянный alternate-screen Live, header/body/log
│   ├── phases.py                   # spinner/summary панели для фаз без своего дашборда
│   ├── formatting.py                # format_duration/format_bytes/bar
│   └── dashboard.py                   # четырёхпанельный контент body во время encoding
│
└── orchestration/              # жизненный цикл instance и результата
    ├── provisioning.py            # rent_instance, wait_for_running, recover_created_instance
    ├── job_monitor.py               # wait_for_job: SSH-поллинг + app.set_body(dashboard)
    ├── transfer.py                   # stream_process: app.set_body(...) для rsync-строки
    ├── publish.py                     # atomic_exchange_dirs, rsync_results
    ├── local_state.py                  # file lock на video_id, recovery прерванного publish
    └── diagnostics.py                   # tail удалённых логов → app.append_log на сбое
```

Зависимости идут в одну сторону: `core` ни от чего не зависит; `vast_api`, `remote`, `ui` зависят только от `core`; `orchestration` зависит от `core`/`vast_api`/`remote`/`ui`; `pipeline.py` и `cli.py` — самый верхний уровень, зависят от всего перечисленного. Ни один низкоуровневый модуль не открывает собственный Rich `Live`/`console.status` — на весь процесс существует ровно один `Live` (`TuiApp`), и все фазы лишь подменяют его `body`.

## Полный жизненный цикл job

1. Orchestrator проверяет аргументы, URL, `video_id`, API key и локальный SSH key.
2. Для обычного запуска получает эксклюзивный file lock для конкретного `video_id`.
3. Выполняет `HEAD` source URL, если сервер поддерживает его, и получает ожидаемый размер MP4.
4. Запрашивает у Vast.ai подходящие offers для RTX 3060, RTX A2000 и RTX 4060.
5. Объединяет, дедуплицирует и ранжирует offers по ожидаемой полной стоимости job.
6. Принимает лучший доступный offer через Vast API и получает `instance_id`.
7. Ждёт `actual_status=running`, затем отдельно ждёт появления рабочего SSH endpoint.
8. Vast выполняет переданный `onstart`: запускает watchdog, устанавливает пакеты и стартует remote encode job.
9. Origin через SSH опрашивает stage, download size, GPU telemetry, progress-файлы и tails логов.
10. Remote job скачивает source, проверяет диск и GPU pipeline, затем параллельно кодирует четыре rendition.
11. После успеха всех rendition создаётся `master.m3u8` и проверяется структура результата.
12. Origin делает resumable `rsync pull` в staging-каталог.
13. Staging валидируется, служебные progress/log-файлы удаляются, HLS публикуется.
14. Независимо от результата Python `finally` вызывает DELETE instance.

## Доступы и модель безопасности

### `VAST_API_KEY`

- Хранится только в environment Binary Racks.
- Передаётся Vast API как `Authorization: Bearer ...` библиотекой Requests.
- Не вставляется в `onstart`, remote job, SSH-команды или логи.
- Нужен для поиска offers, создания, просмотра и удаления instances.

### SSH key `/root/.ssh/vast_encoder`

- Приватная часть существует только на Binary Racks.
- Публичная часть заранее добавлена в аккаунт Vast.ai.
- Используется локальным `ssh`/`rsync` для входа `root@<vast-host>:<port>`.
- Временный Vast instance видит только соответствующий public key.
- Для каждого job создаётся отдельный ephemeral `known_hosts`; это устраняет конфликты при повторном использовании Vast IP/port.

### `CONTAINER_API_KEY`

Vast автоматически внедряет внутрь instance ограниченный ключ `CONTAINER_API_KEY` и `CONTAINER_ID`. Этот ключ может управлять только данным instance и используется исключительно failsafe-watchdog для self-destroy. Это не `VAST_API_KEY` пользователя и не credential origin.

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
- исходящий SSH к endpoint, выданному Vast;
- `rsync` и OpenSSH client;
- права на `/var/www/html/video`;
- Python packages (ставятся через uv): Requests, Rich и Loguru.

### Vast host/offer

Фильтры по умолчанию:

| Параметр | Значение |
|---|---:|
| GPU | RTX 3060, RTX A2000 или RTX 4060 |
| GPU count | 1 |
| Offer type | on-demand |
| Verified | да |
| Reliability | ≥ 0.98 |
| Effective CPU | ≥ 4 cores |
| RAM | ≥ 8192 MB |
| Disk | ≥ 40 GB |
| Disk bandwidth | ≥ 200 MB/s |
| CUDA compatibility | ≥ 12.6 |
| Total price | ≤ $0.08/hour |
| Direct SSH ports | ≥ 1 |
| Доступная длительность | boot timeout + job timeout |

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

Обычный запуск на Binary Racks:

```bash
export VAST_API_KEY='...'

uv run --directory /opt/vast-hls-orchestrator vast-hls-orchestrator \
  --video-id test \
  --source-url https://origin.example.com/video/test.mp4
```

Расширенные диагностические сообщения:

```bash
uv run --directory /opt/vast-hls-orchestrator vast-hls-orchestrator \
  --video-id test \
  --source-url https://origin.example.com/video/test.mp4 \
  --verbose
```

Только поиск и рейтинг offers, без аренды:

```bash
uv run --directory /opt/vast-hls-orchestrator vast-hls-orchestrator \
  --video-id test \
  --source-url https://origin.example.com/video/test.mp4 \
  --dry-run
```

Dry-run требует `VAST_API_KEY`, но не требует локального SSH private key и ничего не создаёт в Vast.

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

1. минимальная `estimated_cost`;
2. минимальная почасовая `dph_total`;
3. максимальная download bandwidth;
4. максимальная disk bandwidth.

В terminal выводится таблица пяти лучших кандидатов. Для аренды последовательно рассматриваются первые десять: если offer уже занят или вернул `no_compatible_tag`, берётся следующий.

## Создание Vast instance

Лучший доступный offer принимается запросом `PUT /api/v0/asks/<offer_id>/`. Параметры instance:

```text
image:        nvidia/cuda:12.6.3-runtime-ubuntu24.04
disk:         40 GB
runtype:      ssh_direct
target_state: running
cancel_unavail: true
NVIDIA_DRIVER_CAPABILITIES=compute,video,utility
```

Также передаются уникальный label и `onstart` script. Label используется не только для удобства: если TCP-соединение оборвалось после PUT и неизвестно, создал ли Vast instance, orchestrator ищет instance через `GET /api/v1/instances` с точным label. До завершения reconciliation новый offer не арендуется — это защищает от «потерянного» платного instance.

API retries:

- transport errors, 429 и 5xx на безопасных read/delete запросах используют exponential backoff;
- 401/403 немедленно завершают pipeline как ошибка credentials/permissions;
- create PUT никогда вслепую не повторяется после неоднозначного transport/5xx результата;
- 429 create можно повторить, поскольку сервер явно отклонил запрос по rate limit;
- исчезновение instance или bad state считается fatal.

## Bootstrap и remote job

Vast может передать onstart оболочке `/bin/sh`, поэтому внешний onstart содержит только POSIX-совместимое декодирование payload и явно запускает `/bin/bash`.

Bootstrap:

1. создаёт `/workspace` и stage/status-файлы;
2. перенаправляет stdout/stderr через `tee` в `/workspace/bootstrap.log`;
3. запускает self-destroy watchdog;
4. выполняет `apt-get update`;
5. устанавливает `ffmpeg`, `aria2`, `curl`, `ca-certificates`, `rsync`;
6. декодирует `/workspace/encode-job.sh` из Base64;
7. запускает job через `nohup`, вывод направляет в `/workspace/job.log`.

Base64 здесь не является шифрованием. Он нужен только для надёжной передачи многострочного Bash через JSON/API без разрушения quoting. Secrets в payload отсутствуют.

Если bootstrap падает до запуска job, trap всё равно создаёт `JOB_EXIT`, переводит stage в `bootstrap-failed` и создаёт `JOB_DONE`, поэтому origin не ждёт до полного timeout.

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
4. тестовое двухсекундное кодирование реального source:

```text
NVDEC decode → CUDA frames → scale_cuda 640×360 → h264_nvenc → null muxer
```

Таким образом проверяется не только наличие названий в `ffmpeg -encoders/-filters`, но и фактическая совместимость драйвера, FFmpeg, decode, CUDA scaling и NVENC.

## FFmpeg и ABR HLS

Четыре независимых FFmpeg-процесса запускаются параллельно:

| Rendition | Resolution | Video | Maxrate | Bufsize | Audio | CQ |
|---|---:|---:|---:|---:|---:|---:|
| 1080p | 1920×1080 | 6500k | 7150k | 13000k | AAC 160k | 19 |
| 720p | 1280×720 | 3500k | 3850k | 7000k | AAC 128k | 20 |
| 480p | 854×480 | 1800k | 2000k | 3600k | AAC 128k | 21 |
| 360p | 640×360 | 900k | 1000k | 1800k | AAC 96k | 22 |

Общие параметры:

- CUDA/NVDEC hardware decode;
- `scale_cuda` без возврата frames в system RAM;
- `h264_nvenc`, preset `p5`, tune `hq`, rate control `vbr`;
- forced keyframe/IDR каждые 6 секунд;
- HLS VOD, `hls_time=6`, `independent_segments`;
- optional first audio stream: видео без audio также допустимо;
- stdout/stderr каждого процесса сохраняется в отдельный `ffmpeg.log`.

Структура remote output:

```text
/workspace/out/
├── master.m3u8
├── 1080p/
│   ├── index.m3u8
│   ├── segment_00000.ts ...
│   ├── progress.txt
│   └── ffmpeg.log
├── 720p/
├── 480p/
└── 360p/
```

Если один FFmpeg завершается с ошибкой, Bash `wait -n` обнаруживает это сразу, посылает TERM остальным процессам, затем KILL зависшим процессам и печатает tails всех FFmpeg logs. Master playlist создаётся только после успеха всех четырёх процессов и проверки `#EXT-X-ENDLIST`/segments.

### Почему четыре процесса, а не один FFmpeg split

Один decode + CUDA split снизил бы нагрузку NVDEC и чтение source. Текущая схема оставлена ради более простой диагностики, отдельных progress/log-файлов и понятного контроля каждого rendition. На данных GPU четыре NVENC output находятся в допустимом практическом диапазоне. Если NVDEC станет bottleneck на 4K/high-FPS source, переход на единый filter graph стоит рассматривать как отдельную оптимизацию и проверять нагрузочным тестом.

## Live UI, progress и логи

### FFmpeg progress

Обычный stderr `-stats` не парсится. Каждый процесс получает собственный `-progress` endpoint через FIFO. Relay на remote стороне собирает полный record до строки `progress=...`, записывает временный файл и делает `mv` в `progress.txt`. Origin поэтому не читает наполовину записанные records.

Поля `frame`, `fps`, `out_time_us`, `bitrate`, `speed` и `progress` опрашиваются по SSH. Процент рассчитывается как:

```text
percentage = out_time / ffprobe_duration × 100
ETA        = (duration - out_time) / speed
```

### Полноэкранное приложение

Весь запуск, от разбора аргументов до `finally` с destroy instance, рендерится как одно [Rich](https://rich.readthedocs.io/) full-screen приложение (`ui/app.py`, alternate screen buffer — как у `htop`/`vim`), а не серия отдельных Live-виджетов. Экран разбит на три зоны:

1. **Header** — название и текущая фаза (`searching offers`, `provisioning`, `encoding`, `transferring result`, `done`/`failed`);
2. **Body** — контент конкретной фазы: spinner при поиске offers/аренде/provisioning/ожидании SSH, таблица топ-5 offers, четырёхпанельный ABR-дашборд во время encoding (stage/instance/GPU/price/elapsed/SSH, download+GPU/NVENC/NVDEC/VRAM, progress по 1080p/720p/480p/360p, remote log tail) или live-строка `rsync --info=progress2` во время transfer;
3. **Log** — хвост локальных Loguru-сообщений оркестратора в реальном времени.

Тело меняется по ходу job вместо открытия новых Live-контекстов — так весь процесс остаётся одним непрерывным full-screen приложением.

### Loguru и recap после выхода

[Loguru](https://loguru.readthedocs.io/) направляется в собственный sink:

- `INFO` — stages, выбранный offer, endpoint, transfer lifecycle;
- `SUCCESS` — SSH ready, encode complete, publish и destroy;
- `WARNING` — transient API/SSH/rsync failures, fallback и recovery;
- `ERROR` — понятная причина окончательного сбоя;
- `DEBUG` — API attempts, remote commands и дополнительные remote log changes; включается `--verbose`.

Пока полноэкранное приложение активно, все log-записи идут в панель **Log**, а не печатаются напрямую в терминал. Alternate screen buffer терминала стирается в момент выхода из приложения, поэтому сразу после закрытия full-screen режима оркестратор печатает весь накопленный лог обратно в обычный scrollback терминала (`TuiApp.print_recap()`) — это единственная информация, которая у оператора остаётся после завершения job, включая diagnostics при сбое (tail bootstrap/job/ffmpeg логов).

API keys и private key contents не логируются.

## Получение и публикация результата

После remote success Binary Racks выполняет:

```text
rsync -a --partial --partial-dir=.rsync-partial --info=progress2
```

Источник — `root@<vast-endpoint>:/workspace/out/`, назначение:

```text
/var/www/html/video/<video_id>/abr.staging.<instance_id>/
```

При временном SSH/rsync failure выполняется до четырёх попыток. Staging не пересоздаётся между попытками, поэтому partial files используются для resume.

До публикации проверяются:

- `master.m3u8`;
- все четыре `index.m3u8`;
- хотя бы один непустой `segment_*.ts` для каждого rendition.

`ffmpeg.log` и `progress.txt` удаляются из staging и не становятся публичными.

На Linux существующий `abr` и новый staging меняются местами одним `renameat2(RENAME_EXCHANGE)`. Старый `abr`, оказавшийся в staging, переносится в backup и удаляется. Если filesystem не поддерживает exchange, применяется совместимый fallback:

```text
abr → abr.backup.<instance_id>
staging → abr
delete backup
```

При старте следующего job orchestrator восстанавливает backup после прерванного publish и очищает stale staging/backup. Все rename выполняются внутри одного filesystem.

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

1. активный rsync получает terminate, затем kill при необходимости;
2. управление переходит в `except KeyboardInterrupt` / Python `finally` внутри полноэкранного приложения;
3. Vast instance удаляется;
4. full-screen приложение закрывается (alternate screen restore) и лог реплеится в обычный терминал;
5. процесс возвращает exit code `130`.

### Диагностика ошибок

При remote failure orchestrator запрашивает и показывает в terminal:

- tail `/workspace/bootstrap.log`;
- tail `/workspace/job.log`;
- tail `ffmpeg.log` каждого rendition.

Обрабатываются, среди прочего:

- invalid/expired API key и недостаточные permissions;
- Vast rate limiting и server errors;
- unavailable offer и `no_compatible_tag`;
- неоднозначный create response;
- provisioning timeout и исчезновение instance;
- позднее появление/изменение SSH endpoint;
- кратковременные SSH disconnects;
- aria2/source failure;
- недостаток remote disk space;
- отсутствие NVENC/NVDEC/`scale_cuda`;
- падение одного FFmpeg;
- job timeout;
- прерванный/resumable rsync;
- неполный HLS result;
- stale staging/backup;
- параллельный job с тем же `video_id`.

## Параметры командной строки

| Параметр | Default | Назначение |
|---|---:|---|
| `--video-id` | required | Безопасный ID и имя origin-каталога |
| `--source-url` | required | Публичный HTTP(S) MP4 URL |
| `--origin-root` | `/var/www/html/video` | Корень публикации |
| `--ssh-key` | `/root/.ssh/vast_encoder` | Локальный private key для Vast SSH |
| `--known-hosts` | `/root/.ssh/vast_known_hosts` | Базовое имя ephemeral known-hosts file |
| `--image` | CUDA 12.6.3 Ubuntu 24.04 | Vast Docker image |
| `--disk-gb` | `40` | Размер instance disk |
| `--max-hourly` | `0.08` | Максимальная цена offer |
| `--min-reliability` | `0.98` | Минимальная reliability |
| `--min-cpu` | `4` | Effective CPU cores |
| `--min-ram-mb` | `8192` | RAM в MB |
| `--min-disk-bw` | `200` | Disk bandwidth MB/s |
| `--expected-hours` | `0.5` | Время только для рейтинга cost |
| `--boot-timeout` | `600` | Provisioning timeout, seconds |
| `--job-timeout` | `10800` | Remote job timeout, seconds |
| `--failsafe-seconds` | `14400` | Задержка self-destroy watchdog |
| `--monitor-interval` | `2` | Частота SSH telemetry polling |
| `--ssh-reconnect-timeout` | `180` | Допустимая длительность SSH outage |
| `--rsync-retries` | `4` | Число resumable transfer attempts |
| `--gpus` | 3060/A2000/4060 | Разрешённые GPU names |
| `--verbose` | off | DEBUG logging |
| `--dry-run` | off | Только search/ranking, без аренды |

Полный актуальный список:

```bash
uv run vast-hls-orchestrator --help
```

## Библиотеки и внешние инструменты

### Python

- [Requests](https://requests.readthedocs.io/) — HTTPS-клиент Vast API и HEAD source URL.
- [Rich](https://rich.readthedocs.io/) — Live dashboard, panels, tables, progress bars и terminal rendering.
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
- [Destroy instance](https://docs.vast.ai/api-reference/instances/destroy-instance) — окончательное удаление.
- [Vast Docker environment](https://docs.vast.ai/guides/instances/docker-environment) — `CONTAINER_API_KEY` и `CONTAINER_ID`.
- [FFmpeg](https://ffmpeg.org/documentation.html) — ffprobe, NVDEC, CUDA filters, NVENC и HLS muxer.
- [FFmpeg progress protocol](https://ffmpeg.org/ffmpeg.html#toc-Advanced-options) — machine-readable `-progress`.
- [aria2](https://aria2.github.io/) — parallel Range download.
- [rsync](https://rsync.samba.org/documentation.html) — resumable result pull.
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
8. Перед изменением bitrate ladder или переходом на единый FFmpeg filter graph проведите тесты на всех трёх моделях GPU.
