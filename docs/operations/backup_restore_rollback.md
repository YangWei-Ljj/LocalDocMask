# 备份、恢复、升级与回滚手册

## 1. 备份

```powershell
.venv\Scripts\python.exe scripts\db_maintenance.py backup `
  --db data\tasks.db `
  --output backups\tasks-manual.db
```

备份使用 SQLite 官方 backup API，写入临时文件并通过 `PRAGMA integrity_check` 后才原子落位。

## 2. 验证

```powershell
.venv\Scripts\python.exe scripts\db_maintenance.py verify --db backups\tasks-manual.db
```

必须满足 `integrity=ok` 且 schema 不高于当前应用支持版本。

## 3. 恢复

先停止应用，再执行：

```powershell
.venv\Scripts\python.exe scripts\db_maintenance.py restore `
  --backup backups\tasks-manual.db `
  --db data\tasks.db
```

恢复流程会先验证备份和临时副本，再 checkpoint 当前库并原子替换；脚本还会为当前数据库建立 `pre-restore` 备份。无效备份不得删除目标库或 WAL/SHM。

## 4. v3 → v4 升级

应用启动或执行：

```powershell
.venv\Scripts\python.exe scripts\db_maintenance.py migrate `
  --db data\tasks.db --to 4
```

升级前自动生成 `tasks.db.bak-*`。迁移在显式事务中执行，失败回滚并保留原库和备份。

## 5. 回滚

应用拒绝打开高于自身支持版本的 schema。数据库回滚使用升级前备份：

```powershell
.venv\Scripts\python.exe scripts\db_maintenance.py restore `
  --backup <升级前备份> --db <回滚目标数据库>
```

恢复后旧 schema 数据可读取。旧版二进制本身的兼容启动需要单独部署验证，本轮未伪造该项结果。

## 6. 文件目录保护

数据库恢复只操作指定 SQLite 文件，不删除成功目录、隔离目录、原件或配置。恢复前应先备份整个受控数据目录，并保持 `quarantine` 不自动清理。

## 7. 合成数据演练

```powershell
.venv\Scripts\python.exe scripts\release_db_drill.py `
  --output dist\t9-evidence\db-drill.json
```

演练覆盖 v3 备份、v4 升级、迁移前备份、v3 回滚恢复、损坏备份拒绝、当前数据库不变、未来 schema 拒绝以及成功/隔离目录哨兵文件保留。
