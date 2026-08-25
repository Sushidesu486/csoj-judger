# OJ Arbiter

OJ Arbiter 是 HPC101 OJ 的只读合规审查器。它从 plat101 PostgreSQL 和提交 NFS 读取数据，使用本地相似度算法与集群内 LLM 生成审查报告；它不修改提交有效性，也不向 OJ 数据库写入任何内容。

历史调查和集群现状见 [`handover.md`](handover.md)，需求见 [`docs/spec.md`](docs/spec.md)，模块与 Kubernetes 执行设计见 [`docs/architecture.md`](docs/architecture.md)。

## 安全边界

- OJ 数据库仅用于一致性只读快照。
- 学生提交和 baseline 以只读方式挂载。
- 只有审查报告目录允许写入。
- LLM 结论是人工复核线索，不自动修改 `is_valid` 或成绩。
- 凭据只能来自 Kubernetes Secret 或本地未跟踪环境变量。

## 当前状态

- 本地 Git 仓库已初始化。
- 已实现第一条垂直切片：只读 DB 快照、双语料集任务规划、exact-digest 跨学生分组和原子 manifest 写入。
- 已提供不调用 LLM 的 `doctor`、`plan`、`smoke` 命令和固定到 m601 的 smoke Job。
- 现有 `oj-audit-db` Secret 使用的数据库角色实际具备写权限。开发测试必须强制只读会话，生产部署必须迁移到专用只读角色。

## 开发命令

```bash
make check
make zipapp
scripts/deploy-smoke.sh
```

`deploy-smoke.sh` 会更新开发 ConfigMap、重建 `oj-checker-smoke` Job、等待完成并输出日志。该 Job 不读取 LLM Secret，也不调用 glm。
