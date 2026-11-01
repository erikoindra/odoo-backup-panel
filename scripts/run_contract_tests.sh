#!/usr/bin/env bash
# Run the contract tests (backup_panel/panel/tests/test_contract.py) against a real Docker daemon.
#
# Read this before running it. The tests build a lab (PostgreSQL, a backup container, an Odoo data volume)
# on the Docker host they run on, and the test runner needs the Docker socket to do that. Whoever can run
# code in that runner can control the host, which is root-equivalent. So:
#   - run it only on a disposable host (a throwaway VM), never on a server that runs live stacks;
#   - the runner has no network (--network none) and its dependencies are baked into the image at build time;
#   - the runner is named and removed when the run ends, and the tests remove their lab resources.
#
# The script refuses to run until you confirm the host is disposable:
#   OBP_CONTRACT_ON_DISPOSABLE_HOST=yes scripts/run_contract_tests.sh [--proxy]
#
# Settings (same meaning as in test_contract.py):
#   CONTRACT_BACKUP_IMAGE    default ghcr.io/erikoindra/odoo-backup-service:pg17-0.1
#   CONTRACT_POSTGRES_IMAGE  default postgres:17-alpine
#   CONTRACT_ODOO_IMAGE      default unset: the neutralize tests are skipped (for example odoo:19.0)
#   DOCKER_SOCKET            default /var/run/docker.sock
#
# --proxy also routes the panel's own calls through docker-socket-proxy with CONTAINERS, EXEC and POST only,
# to check the permissions the panel needs. It does not make the run safer: the runner keeps the socket to
# set up the lab, and the proxy cannot restrict what a created container may mount.
set -euo pipefail

if [ "${OBP_CONTRACT_ON_DISPOSABLE_HOST:-}" != "yes" ]; then
  echo "Refusing to run: this creates containers and volumes on the local Docker host." >&2
  echo "Run it on a disposable host, then set OBP_CONTRACT_ON_DISPOSABLE_HOST=yes to continue." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCKET="${DOCKER_SOCKET:-/var/run/docker.sock}"
IMAGE="obp-contract-runner:local"
RUNNER="obp-contract-runner"
PROXY="obp-contract-proxy"
PROXY_NET="obp-contract-proxynet"
PROXY_MODE=0
[ "${1:-}" = "--proxy" ] && PROXY_MODE=1

cleanup() {
  docker rm -f "$RUNNER" "$PROXY" >/dev/null 2>&1 || true
  docker network rm "$PROXY_NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

docker build -q -t "$IMAGE" -f "$ROOT/scripts/contract/Dockerfile" "$ROOT/backup_panel" >/dev/null

env_args=(
  -e PANEL_CONTRACT_TESTS=1
  -e DJANGO_SECRET_KEY=contract-only-not-secret
  -e "CONTRACT_BACKUP_IMAGE=${CONTRACT_BACKUP_IMAGE:-ghcr.io/erikoindra/odoo-backup-service:pg17-0.1}"
  -e "CONTRACT_POSTGRES_IMAGE=${CONTRACT_POSTGRES_IMAGE:-postgres:17-alpine}"
  -e "CONTRACT_ODOO_IMAGE=${CONTRACT_ODOO_IMAGE:-}"
)

if [ "$PROXY_MODE" = 1 ]; then
  # An internal network: the proxy and the runner can reach each other, and nothing outside.
  docker network create --internal "$PROXY_NET" >/dev/null
  docker run -d --name "$PROXY" --network "$PROXY_NET" --network-alias proxy \
    -e CONTAINERS=1 -e EXEC=1 -e POST=1 \
    -v "$SOCKET:/var/run/docker.sock:ro" \
    tecnativa/docker-socket-proxy >/dev/null
  env_args+=(-e "CONTRACT_PANEL_URL=tcp://proxy:2375")
  network=(--network "$PROXY_NET")
else
  network=(--network none)
fi

# The runner: no network (or only the internal proxy network), the app read-only, a tmpfs for scratch files,
# the Docker socket for the lab, and --rm so nothing is left behind.
docker run --rm --name "$RUNNER" "${network[@]}" \
  -v "$ROOT/backup_panel:/app:ro" \
  -v "$SOCKET:/var/run/docker.sock" \
  --tmpfs /tmp \
  "${env_args[@]}" \
  "$IMAGE" \
  python manage.py test panel.tests.test_contract -v 2
