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

开发 Job 使用现有 `oj-audit-db` Secret，但通过 `PGOPTIONS` 和 psycopg connection/transaction 设置强制只读。由于该 Secret 背后的数据库角色实际上具备写权限，它不能用于生产部署。
