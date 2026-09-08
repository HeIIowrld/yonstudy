#!/bin/sh
set -eu

repository="${YONSTUDY_REPOSITORY:-HeIIowrld/yonstudy}"
source_dir=/source
state_dir=/state

run_compose() {
    # Compose runs inside the updater, so its client-side build context is the
    # mounted /source path. Runtime bind mounts still come from the host .env.
    YONSTUDY_BUILD_CONTEXT=/source docker compose \
        --project-directory "$source_dir" \
        --env-file /deployment/.env \
        -f "$source_dir/compose.yaml" "$@"
}

mkdir -p "$state_dir"
git -C "$source_dir" fetch --quiet origin main
candidate="$(git -C "$source_dir" rev-parse origin/main)"
deployed="$(cat "$state_dir/deployed-commit" 2>/dev/null || true)"

if [ "$candidate" = "$deployed" ]; then
    exit 0
fi

# Public repositories expose check runs without a deployment token. Deploy only
# after the GitHub unit-test job for this exact commit has passed.
checks="$(curl -fsSL \
    -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/$repository/commits/$candidate/check-runs")"
test_result="$(printf '%s' "$checks" | jq -r \
    '[.check_runs[] | select(.name == "test") | .conclusion] | last // "pending"')"
if [ "$test_result" != success ]; then
    echo "$(date -Iseconds) $candidate CI test status: $test_result; deferring"
    exit 0
fi

# Do not interrupt a long-running archive or playback job. A later poll retries.
if docker inspect yonstudy >/dev/null 2>&1 \
    && ! docker exec yonstudy flock -n /run/yonstudy/automation.lock true; then
    echo "$(date -Iseconds) yonstudy job is active; deferring $candidate"
    exit 0
fi

git -C "$source_dir" reset --hard --quiet "$candidate"
if docker image inspect yonstudy:local >/dev/null 2>&1; then
    docker image tag yonstudy:local yonstudy:rollback
fi

if ! run_compose build scheduler; then
    echo "$(date -Iseconds) image build failed for $candidate" >&2
    exit 1
fi
if ! run_compose up -d --no-deps scheduler; then
    echo "$(date -Iseconds) container replacement failed for $candidate" >&2
    if docker image inspect yonstudy:rollback >/dev/null 2>&1; then
        docker image tag yonstudy:rollback yonstudy:local
        run_compose up -d --no-deps scheduler
    fi
    exit 1
fi

sleep 3
if ! docker exec yonstudy python /app/cli.py --store /data/store status >/dev/null; then
    echo "$(date -Iseconds) post-deploy check failed for $candidate" >&2
    if docker image inspect yonstudy:rollback >/dev/null 2>&1; then
        docker rm -f yonstudy >/dev/null 2>&1 || true
        docker image tag yonstudy:rollback yonstudy:local
        run_compose up -d --no-deps scheduler
    fi
    exit 1
fi

printf '%s\n' "$candidate" >"$state_dir/deployed-commit"
echo "$(date -Iseconds) deployed $candidate"
