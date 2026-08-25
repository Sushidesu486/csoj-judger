# OJ Arbiter

OJ Arbiter 是 HPC101 OJ 的只读合规审查器。它从 plat101 PostgreSQL 和提交 NFS 读取数据，使用本地相似度算法与集群内 LLM 生成审查报告；它不修改提交有效性，也不向 OJ 数据库写入任何内容。

当前阶段是架构基线。历史调查和集群现状见 [`handover.md`](handover.md)，待确认需求见 [`docs/spec.md`](docs/spec.md)，模块与 Kubernetes 执行设计见 [`docs/architecture.md`](docs/architecture.md)。

## 安全边界

- OJ 数据库仅用于一致性只读快照。
- 学生提交和 baseline 以只读方式挂载。
- 只有审查报告目录允许写入。
- LLM 结论是人工复核线索，不自动修改 `is_valid` 或成绩。
- 凭据只能来自 Kubernetes Secret 或本地未跟踪环境变量。

## 当前状态

- 本地 Git 仓库已初始化。
- 正式 Python 实现和 Kubernetes 清单尚未开始；开始 TDD 前需确认 [`docs/architecture.md`](docs/architecture.md) 中列出的测试 seam。
- 现有 `oj-audit-db` Secret 使用的数据库角色实际具备写权限。开发测试必须强制只读会话，生产部署必须迁移到专用只读角色。
