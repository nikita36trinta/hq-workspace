#!/usr/bin/env bash
# Локальная площадка для правок. Исходники ПРИМОНТИРОВАНЫ, uvicorn с --reload,
# поэтому правка файла видна по F5 — без docker cp и без перезапуска прода.
#
# --no-proxy-headers — как в Dockerfile: площадка должна отличаться от боевой
# чем угодно, только не тем местом, где проверка уже один раз провалилась.
#
# WATCHFILES_FORCE_POLLING обязателен: через bind-mount на macOS inotify внутрь
# контейнера не доходит, и --reload молча не срабатывает — правишь файл, а в
# браузере старая страница.
#
# Монтируем КАТАЛОГ, а не отдельные файлы. Пофайловый bind привязан к иноде, а
# редакторы сохраняют через «временный файл + rename» — инода меняется, и
# контейнер до конца жизни показывает исходную версию файла. Я на это уже
# попался: правки были в файле, но не на экране.
#
# Раньше я гонял каждую итерацию через scp на боевой сервер: 12 секунд на
# перезапуск и, что важнее, каждое промежуточное состояние висело на живом
# сайте. Здесь ни того, ни другого.
#
#   ./dev.sh          — поднять на http://localhost:8790
#   ./dev.sh stop     — погасить
set -euo pipefail
cd "$(dirname "$0")"
NAME=nutriplan-dev
if [ "${1:-}" = "stop" ]; then docker rm -f "$NAME" >/dev/null 2>&1 || true; echo "погашено"; exit 0; fi
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker build -q -t nutriplan-dev-img . >/dev/null
mkdir -p .devdata
docker run -d --name "$NAME" -p 8790:8790 \
  -v "$PWD:/src:ro" \
  -v "$PWD/.devdata:/app/data" \
  -w /src \
  -e DATA_DIR=/app/data \
  -e WATCHFILES_FORCE_POLLING=1 \
  nutriplan-dev-img \
  uvicorn app:app --host 0.0.0.0 --port 8790 --reload \
    --no-proxy-headers \
    --reload-dir /src --reload-include '*.py' >/dev/null
for i in $(seq 1 40); do
  if curl -fsS -o /dev/null http://localhost:8790/api/health 2>/dev/null; then
    echo "http://localhost:8790 готов"; exit 0
  fi
  sleep 0.5
done
echo "не поднялся — docker logs $NAME" >&2; docker logs --tail 30 "$NAME" >&2; exit 1
