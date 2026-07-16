#!/bin/sh
set -eu

container_id="$(
  docker run --detach --rm \
    --tmpfs /cache \
    --tmpfs /library \
    --tmpfs /state \
    --env MEDIA_CACHE_TOKEN=smoke-test-token \
    media-cache:test
)"

cleanup() {
  docker rm --force "${container_id}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

attempt=0
while [ "${attempt}" -lt 30 ]; do
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "${container_id}")"
  case "${status}" in
    healthy)
      docker exec "${container_id}" curl -fsS http://localhost:8686/health
      printf '\n'
      exit 0
      ;;
    unhealthy)
      docker logs "${container_id}"
      exit 1
      ;;
  esac
  attempt=$((attempt + 1))
  sleep 2
done

docker logs "${container_id}"
echo "media-cache did not become healthy within 60 seconds" >&2
exit 1
