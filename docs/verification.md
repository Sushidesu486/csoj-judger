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
