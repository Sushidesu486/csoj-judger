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

## Single-submission report API

`report-api.yaml` deploys the internal API for one-submission compliance
lookups and on-demand reviews. It is intentionally a ClusterIP-only service;
it has no public HTTPRoute and accepts exactly one `submission_id` plus one
allowlisted `model` per review. All models use the same configured upstream URL
and credential; callers cannot supply either value.

The API Pod mounts the report PVC read/write because an on-demand review writes
an immutable result. Submission input and the HPC101 review basis remain
read-only. Replace `REQUIRED_AT_DEPLOY_TIME` with the same immutable
OJ-Arbiter image tag and `OJ_CHECKER_GIT_COMMIT` before applying the manifest.
The checked-in `claimName` (`oj-checker-reports-20260825`) reflects the PVC
verified on m601; production should replace it with the stable report PVC name
chosen by the cluster storage/GitOps setup before deployment.

The optional `oj-checker-report-api` Secret can provide `token`; when present,
callers must send `Authorization: Bearer <token>`. NetworkPolicy still limits
ingress to the `plat101-system` namespace.

Endpoints:

```text
GET  /healthz
GET  /v1/compliance/models
GET  /v1/compliance/submissions/{submission_id}
POST /v1/compliance/reviews  {"submission_id":"<uuid>","model":"gpt-5.6-luna"}
```

## Nightly compliance review

`nightly-cronjob.yaml` starts at 02:00 in `Asia/Shanghai`. It reads each
authoritative best Submission from `oj_user_lab_best_scores`, calls the report
API sequentially with the fixed model `glm-5.3`, and skips only a current
allowlisted compliant report. There is no LLM call budget. The Job has no LLM
credential, Submission mount, or long-running scheduler; all LLM work remains
serialized by the report API.

The read-only database role additionally needs:

```sql
GRANT SELECT ON oj_user_lab_best_scores TO <oj_audit_read_only_role>;
```

Transient LLM failures are attempted at most twice by the report API. The
CronJob itself has `backoffLimit: 0`, continues with the next Submission, and
leaves failed items for the following night.
