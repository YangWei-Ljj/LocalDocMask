# 逐项处理 / 失败删除 / 分目录输出 —— 功能交付报告

日期：2026-10-25（Asia/Shanghai）

## 1. 修改文件清单

- src/localdocmask/models.py：新增条目级状态（ItemStatus）、Candidate 增加 status/failure_reason/deletion_verified/delete/image_part，Task 增加 task_id/file_type/final_hash/report_path/records 与计数。
- src/localdocmask/config.py：新增 SUCCESS_DIR=output/success、QUARANTINE_DIR=output/quarantine。
- src/localdocmask/masker.py：新增 mask_display()（界面/报告安全脱敏显示）。
- src/localdocmask/auditor.py：新增 residual_terms()/_all_text()（逐项独立残留检查）。
- src/localdocmask/task_manager.py：重写 process() 为“脱敏→逐项审计→失败删除→删除验证→最终审计”，finalize() 增加最终哈希绑定与未解决条目门禁，新增 run() 原子入口。
- src/localdocmask/formats/docx.py：嵌入图像敏感文字改为像素级删除（替换部件），脚注/尾注压缩包级脱敏，逐段/表格/页眉页脚来源定位。
- src/localdocmask/formats/xlsx.py：来源定位精确到单元格坐标。
- src/localdocmask/formats/image.py：来源定位精确到 OCR 坐标，删除用更大像素覆盖。
- src/localdocmask/formats/pdf.py：支持空字符串删除（无替换文本）。
- src/localdocmask/gui.py：新增“成功结果 / 失败信息与来源 / 隔离文件”三个结果页，点击失败记录查看详情，敏感值脱敏显示。
- tests/test_flow.py：新增场景 A~H 共 8 项。
- tests/test_security.py：更新 SEC-004 为“嵌入图像删除后导出”。
- README.md、docs/未完成安全功能清单.md：同步更新。

## 2. 核心流程

识别 → 逐项脱敏 → 逐项审计 →（审计失败项）删除 → 删除验证 → 最终整体审计 → 成功/隔离分目录。

## 3. 运行与测试命令

    .venv/Scripts/python.exe -m pytest tests -v

## 4. 测试结果

- 用例总数 42，通过 42，失败 0，跳过 0，退出码 0。
- 场景 A 全部成功、B 部分成功+部分删除、C 删除失败隔离、D 删除后残留隔离、E 批量隔离、F 来源定位、G 原件哈希不变、H 最终哈希绑定，全部通过。

## 5. 成功输出文件示例（合成数据）

- data/output/success/演示_员工信息_脱敏.docx
- data/output/success/演示_员工信息_脱敏_审计记录.json

示例：5 条敏感信息 → 3 条脱敏成功 + 2 条（嵌入图像内）删除并通过验证，最终审计通过、导出到 success。

## 6. 失败信息界面（可验证）

GUI 右下方新增三个标签页：
- “成功结果”：文件 / 输出路径 / 类型 / 识别 / 成功脱敏 / 删除 / 审计 / 哈希。
- “失败信息与来源”：记录ID / 文件 / 类型 / 类别 / 来源位置 / 原始内容(脱敏) / 处理方式 / 审计 / 失败原因 / 删除验证 / 最终状态；点击任一行弹出详情。
- “隔离文件”：文件 / 状态 / 原因。

## 7. 合成数据测试报告要点

- 嵌入图像内的“李四”“13912345678”被识别为敏感项，常规文本脱敏无法触及 → 审计失败 → 转入删除（图像像素黑块）→ 删除验证通过 → 最终审计通过。
- 删除定位失败（部件不存在）→ 条目标记 unresolved → 整文件隔离。
- 删除后仍残留（错误 OCR 区域）→ 条目 unresolved → 整文件隔离。
- 所有审计记录敏感值均已脱敏显示（如 138****5678、1101**********002X、张*）。

## 8. 尚未解决的问题与限制

- PDF 表单字段、XLSM 宏、外部链接、OLE 嵌入对象：检测到即阻止导出（失败关闭，未逐对象删除）。
- DOCX 嵌入图像删除仅支持可被 PIL 像素处理的图像（PNG/JPEG 等）；EMF/WMF 等矢量图无法像素处理时，条目会标记 unresolved 并隔离。
- 无文字的图形印章/签名/二维码/照片人脸不自动识别，提示人工复核。
- 审计/OCR 无超时；GUI 无取消按钮。
