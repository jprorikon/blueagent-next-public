# 数据迁移与修数脚本

这里保留针对既有 `data/` 的迁移和修数脚本，与日常诊断、开发工具分开。生产代码不引用这些脚本；执行前先停掉写入同一数据目录的服务，并先跑默认的 dry-run。`--apply` 才会写数据，部分脚本会在写入前自动备份。

| 脚本 | 用途 | 执行性质 |
|---|---|---|
| `fix_raw_and_facts_20261024.py` | raw 去重、行动事实回填 entity／有效期 | 2026-10-24 一次性修补；确认旧数据已处理后再归档删除 |
| `migrate_entity_partition.py` | 为旧 facts 补角色分仓字段 | 一次性 schema／数据迁移 |
| `purge_seed_facts.py` | 归档并删除 demo 种子事实 | 一次性清理；只有重新导入 seed 后才需要再跑 |
| `archive_noise_facts.py` | 将系统提示类噪音事实标为失效 | 可重复；先 dry-run 核对命中项 |
| `classify_facts.py` | 为缺少 kind／slot 的旧事实补分类 | 可重复回填；会调用模型，注意成本 |
| `rebuild_shingles.py` | 按当前归一化规则重建字面索引 | 可重复维护；归一化算法变更后再跑 |

统一从仓库根目录执行，例如：

```bash
python3 agent/tools/migrations/rebuild_shingles.py
python3 agent/tools/migrations/rebuild_shingles.py --apply
```

这些脚本没有被自动执行，也不代表当前生产数据仍待迁移；是否执行必须按实际数据状态判断。
