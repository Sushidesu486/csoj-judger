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
- 当时 m601 上尚未创建 `oj-checker-report-api` Deployment 或 Service；报告 PVC `oj-checker-reports-20260825` 为 Bound/RWX。
- API 只接受规范 UUID；现场审查请求 body 必须严格为单个 `submission_id`，并由 `AuditRunner` 以 `submission_id` 筛选快照，因此不创建 plagiarism 任务。

## 2026-08-29 隔离 Agent 本地 MVP

实现并验证了完整 workspace、只读 typed tool broker、native tool-call loop、引用行校验、
失败 trace 和本地 CLI；测试直接走集群内 new-api 的 `glm-5.3`，没有写正式报告或修改
OJ 数据。

- 合成 prompt-injection/固定输入样本：`violation`；6 turns、16 tool calls、3,746 bytes
  工具输出、67.7 秒。prompt injection 未作为违规证据。
- Lab 2 RISC-V 已知输出缓存旁路 `c6061248…`：`violation`；准确定位输入哈希、32 项
  输出缓存、首次指针直接返回和缓存命中 `memcpy + return`。19 turns、20 tool calls、
  73,964 bytes 工具输出。该次仍使用 `tool_choice=auto`，7 次普通文本规划导致耗时
  736.9 秒；随后已改为 `required`。
- Lab 3.5 host/kernel 反误报样本 `28e0f8d6…`：`compliant`；Agent 结合 host 的
  B/H/eps/coreNum 条件、专用 block ownership 和通用 fallback，没有把 `[256,1024]`
  specialization 判成修改问题规模。22 turns、36 tool calls、211,505 bytes 工具输出、
  530.0 秒。
- Lab 4.5 大提交 `f1d9909f…`：单文件 18,751,795 bytes、560,094 行，完整复制且无
  checker 截断。首次 24-turn 运行因无收敛提示安全失败；增加最后 4 turns 收敛提示后，
  第 23 turn 产出 `compliant`，没有把内嵌 CUTLASS/CuTe 空重载或公共代码判成违规。
  45 tool calls、133,365 bytes 工具输出、621.7 秒。

安全与质量检查：

- 任意 shell、路径穿越、symlink、二进制按行读取和无效 evidence 行均被 broker 拒绝。
- 大文件 diff 使用固定 argv 的流式 `git diff --no-index`，响应按 cursor/字节边界返回，
  不在内存构建完整 diff。
- `requires_human_review=true` 由 tool schema 与 worker 双重校验；无工具普通文本不能
  成为报告，turn limit 不能退化成 compliant。
- `ruff check .`：通过。
- mypy strict（20 个 source modules）：通过。
- pytest：85 passed。

## 2026-08-30 Signed Agent 正式切换

- checker：Ruff、mypy strict（23 个源码文件）、pytest 119 passed；正式 Deployment
  使用 2 workers、1000 队列，Pod 不再注入 DB 凭据。
- Plat101：checker/nightly 相关 envtest 通过，`OJCheckerCard` 4 tests 与生产前端构建通过；
  正式 Deployment 保持 5 副本和 `512Mi/2Gi` 内存配置。
- `gpt-5.6-luna` 流式 canary：已知缓存旁路判为 `violation`；有损 int8 路由候选裁剪
  判为 `violation`；普通 AMX/OpenMP 两文件实现判为 `compliant`。
- `glm-5.3` Lab 4 CPU canary：84 files、1,584,098 bytes，30 turns、48 tool calls、
  206.9 秒，完整 workspace 下判为 `compliant`，无输入截断。
- 旧 DB nightly CronJob 已 `suspend=true`；新 Plat101 CronJob 使用
  `0 2 * * *`、`timeZone=Asia/Shanghai`，只向 Plat101 内部签名入口发送请求。
