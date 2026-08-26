# Verification log

## 2026-08-25 m601 smoke test

环境：

- Namespace：`csoj-judger`
- Job：`oj-checker-smoke`
- Node：`m601.clusters.zjusct.io`
- LLM：未调用
- NFS：只读挂载 `/tank/hpc101/submissions`
- Report output：Pod `emptyDir`

结果：

- Job completed，restart count 0。
- PostgreSQL transaction read-only：true。
- Submission NFS root readable：true。
- Sample submission input readable：true。
- lab4-cpu single-review corpus：52。
- lab4-cpu plagiarism corpus：204。
- 生成 single-review tasks：52。
- 发现跨学生 exact-digest task：1。

该结果证明第一条垂直切片能从全部历史语料重新发现“共享版本不是双方最高分”的 exact-digest 回归案例，同时不会访问 LLM 或写 OJ DB。

## 2026-08-25 专用数据库只读角色

在 m601 上通过临时诊断 Job 创建并验证 `oj_checker_ro`，随后删除诊断 Job。验证结果：

- `rolsuper=false`
- `rolcreaterole=false`
- `rolcreatedb=false`
- `rolreplication=false`
- `rolbypassrls=false`
- 新连接 `transaction_read_only=on`
- 仅有 `oj_submissions: SELECT` 与 `oj_submission_runs: SELECT`
- 两表 `UPDATE=false`
- `public` schema `CREATE=false`

对应连接只保存在集群 `csoj-judger/oj-audit-db-ro` Secret 中，未写入仓库或日志。

## 2026-08-25 正式链路本地验证

- pytest：40 passed。
- mypy strict：通过。
- ruff：通过。
- canary Job：`kubectl create --dry-run=client` 通过。
- `linux/amd64` 生产镜像构建成功。
- 镜像在只读根文件系统、UID 65532 下运行成功，运行时 Git 可用。
- 容器从只读挂载的 HPC101 仓库解析固定 commit `c12aaa26a346fd3fd8a39eceb3ac355ba14d19b0`，成功读取 lab4-cpu 的 98 个 baseline 文件及实验文档。

## 2026-08-25 lab5 MinHash 并行基准

固定 cutoff `2026-08-25T08:49:31.312975Z`，语料为 96 份有效历史提交：

- baseline delta 共生成 3,632,993 个 shingle，单提交最大 116,077，中位数 25,407。
- 单进程 MinHash 签名阶段：81.926 秒；完整候选检测：93.396 秒。
- 2 进程原型签名阶段：42.873 秒。
- 8 进程正式实现完整候选检测：23.975 秒；含 DB/NFS/delta 的总准备时间：32.364 秒。
- 单进程和 8 进程均产生 19 对候选：18 对 MinHash、1 对 exact submission；exclusion 为 0。
- 8 进程 Pod 峰值内存 1,320,255,488 bytes，低于 4 GiB limit。
- m601 共 64 CPU，测试时节点使用约 7.182 CPU；临时并行 Pod 使用 4 CPU request、8 CPU limit。

## 2026-08-26 单提交报告 API 本地验证

- pytest：51 passed。
- mypy strict：通过。
- ruff：通过。
- `kubectl apply --dry-run=client -f deploy/kubernetes/report-api.yaml`：通过，生成 Deployment、ClusterIP Service 和 NetworkPolicy。
- m601 上尚未创建 `oj-checker-report-api` Deployment 或 Service；报告 PVC `oj-checker-reports-20260825` 仍为 Bound/RWX。
- API 只接受规范 UUID；现场审查请求 body 必须严格为单个 `submission_id`，并由 `AuditRunner` 以 `submission_id` 筛选快照，因此不创建 plagiarism 任务。
