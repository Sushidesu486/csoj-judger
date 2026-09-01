# Kubernetes development deployment

`smoke-job.yaml` 用于验证当前工作树构建的 zipapp，不是最终生产 CronJob。

它执行以下只读检查：

- Pod 必须调度到 `m601.clusters.zjusct.io`。
- PostgreSQL transaction 必须是 read-only。
- Submission NFS 根目录和一个样例输入目录必须可读。
- 对 lab4-cpu 的全部有效成功且 `score >= 60` 的提交规划任务。
- 不挂载 LLM Secret，不调用 new-api。
- manifest 写入 Pod 的临时 `emptyDir`，不会污染正式报告目录。

运行：

```bash
scripts/deploy-smoke.sh
```

部署脚本拒绝未提交的工作树，并把当前真实 Git commit SHA 注入 Job；清单中的 `REQUIRED_AT_DEPLOY_TIME` 只是防止绕过脚本后误生成不可追踪报告的占位符。

开发 smoke Job 仍使用现有 `oj-audit-db` Secret，但通过 `PGOPTIONS` 和 psycopg connection/transaction 设置强制只读。正式 canary 使用专用 `oj-audit-db-ro`。

## LLM canary

`canary-job.yaml` 是正式审查的验收模板：

- 固定调度到 `m601.clusters.zjusct.io`。
- 固定 HPC101 commit `c12aaa26a346fd3fd8a39eceb3ac355ba14d19b0`，init container 会核对实际 checkout SHA。
- `lab4-cpu` canary 审查该 lab 每个学生的最高提交，并从全部历史提交产生抄袭候选；不做学生数量抽样。
- MinHash 按提交使用 8 个进程并行计算；checker Pod 请求 4 CPU、限制 8 CPU。
- Submission NFS 整体只读，报告仅通过单独的 `/tank/hpc101/submissions/.oj/audit-reports` NFS 挂载写入。
- 使用 `oj-audit-llm` Secret 的 `api-key`、`base-url`，且镜像和 `OJ_CHECKER_GIT_COMMIT` 中的 `REQUIRED_AT_DEPLOY_TIME` 必须在验收后替换为同一个不可变 OJ-Arbiter commit。
- 使用专用 `oj-audit-db-ro` Secret；角色只允许读取 `oj_submissions` 与 `oj_submission_runs`。

该模板仍不应在验收前直接应用：镜像和代码 commit 尚未冻结。`oj-audit-db-ro` 目前是集群内 Secret，凭据不会写入 Git；后续纳入 GitOps 时必须沿用仓库认可的 Secret 管理方式。

## Signed Agent review API

`report-api.yaml` deploys the internal asynchronous API for one-submission
compliance lookups and on-demand Agent reviews. It is intentionally a
ClusterIP-only service and has no public HTTPRoute. Plat101 queries its own DB,
freezes the Submission metadata and signs `review-bundle-v1`; the checker has
only the corresponding Ed25519 public key and no database credential.

The API Pod mounts the report PVC read/write because an on-demand review writes
an immutable result. Submission input and the HPC101 review basis remain
read-only. The image is pinned by digest. The checked-in `claimName`
(`oj-checker-reports-20260825`) is the temporary RWX report PVC used by the
two-month deployment on m601.

The `oj-checker-report-api` Secret provides the required Bearer token. The
`oj-checker-review-bundle` Secret contains only key `public-key`; the matching
private key exists only in the Plat101 namespace. NetworkPolicy additionally
limits ingress to the `plat101-system` namespace.

Endpoints:

```text
GET  /healthz
GET  /v1/compliance/models
GET  /v1/compliance/submissions/{submission_id}
POST /v1/compliance/review-runs  <signed review-bundle-v1 envelope>
GET  /v1/compliance/review-runs/{run_id}
GET  /v1/compliance/submissions/{submission_id}/review-runs/latest
GET  /v1/plagiarism/submissions/{submission_id}
```

## Nightly compliance review

The legacy DB-reading nightly CronJob is suspended and must not be resumed
against `agent-report-api`, which intentionally rejects the old unsigned
synchronous endpoint. Plat101 now selects each owner's authoritative best
Submission at 02:00 Asia/Shanghai, skips a compliant or active run, and signs
each remaining request with the fixed model `glm-5.3`. The trigger has a
dedicated bearer token but no DB or checker API credential.

Transient LLM failures are attempted at most twice by the report API. The
CronJob itself has `backoffLimit: 0`, continues with the next Submission, and
leaves failed items for the following night.
