#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REMOTE=${OJ_CHECKER_REMOTE:-m601}
NAMESPACE=${OJ_CHECKER_NAMESPACE:-csoj-judger}
JOB_NAME=oj-checker-smoke
REMOTE_ZIPAPP=/tmp/oj-checker.pyz
REMOTE_JOB=/tmp/oj-checker-smoke-job.yaml

make -C "$ROOT_DIR" zipapp
scp -q "$ROOT_DIR/dist/oj-checker.pyz" "$REMOTE:$REMOTE_ZIPAPP"
scp -q "$ROOT_DIR/deploy/kubernetes/smoke-job.yaml" "$REMOTE:$REMOTE_JOB"

ssh -o BatchMode=yes "$REMOTE" \
  "kubectl -n '$NAMESPACE' create configmap oj-checker-dev-code --from-file=oj-checker.pyz='$REMOTE_ZIPAPP' --dry-run=client -o yaml | kubectl apply -f -"
ssh -o BatchMode=yes "$REMOTE" \
  "kubectl -n '$NAMESPACE' delete job '$JOB_NAME' --ignore-not-found --wait=true"
ssh -o BatchMode=yes "$REMOTE" "kubectl apply -f '$REMOTE_JOB'"

if ! ssh -o BatchMode=yes "$REMOTE" \
  "kubectl -n '$NAMESPACE' wait --for=condition=complete job/'$JOB_NAME' --timeout=900s"; then
  ssh -o BatchMode=yes "$REMOTE" \
    "kubectl -n '$NAMESPACE' describe job '$JOB_NAME'"
  ssh -o BatchMode=yes "$REMOTE" \
    "kubectl -n '$NAMESPACE' logs job/'$JOB_NAME' --all-containers=true"
  exit 1
fi

ssh -o BatchMode=yes "$REMOTE" \
  "kubectl -n '$NAMESPACE' get pods -l job-name='$JOB_NAME' -o wide"
ssh -o BatchMode=yes "$REMOTE" \
  "kubectl -n '$NAMESPACE' logs job/'$JOB_NAME' -c checker"
