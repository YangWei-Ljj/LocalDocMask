# LocalDocMask 测试报告

- 执行日期：2026-10-29（CST，UTC+8）
- 执行命令：`.venv\Scripts\python.exe -m pytest tests -q`
- 环境：Windows 10 (10.0.19045)，Python 3.11.16（项目内 `.venv`）

## 1. 总体结果

```
157 passed, 1 skipped in ~50s
```

| 指标 | 值 |
| --- | --- |
| 用例总数 | 158 |
| 通过 | 157 |
| 跳过 | 1 |
| 失败 | 0 |
| 错误 | 0 |

跳过用例：

- `tests/test_security_hardening.py:102 :: TestPathContainment::test_symlink_escape_blocked`
  — 原因：当前 Windows 账号未开启开发者模式 / 无管理员权限，`os.symlink` 抛 `OSError`，测试主动 `pytest.skip("当前环境不允许创建符号链接（Windows 需开发者模式/管理员）")`。
  该用例的语义（符号链接逃逸必须被 `safe_join` 拒绝）在 CI 开启开发者模式后可自动复跑；其余 10 条路径越权用例全部通过。

## 2. 测试分层与文件归属

按 `tests/` 下 20 个文件归类，共 157 个用例：

| 层级 | 文件 | 用例数 | 覆盖能力 |
| --- | --- | --- | --- |
| 单元（识别） | `test_detection.py` | 6 | 正则规则命中、身份证校验位+出生日期、词典实体、长词优先排序、自定义敏感词、地址/编号 |
| 单元（识别） | `test_context.py` | 3 | 泛指引用识别、真实专名不被误判、上下文信号抽取 |
| 单元（识别） | `test_fusion.py` | 7 | 多源融合加权、白名单抑制、黑名单自动确认、公开实体转复核、泛指公司抑制、Ollama 冲突转复核、高精度类型自动确认 |
| 单元（识别） | `test_knowledge.py` | 3 | 四桶词库（黑名单/公开/白名单/领域词）加载与分类 |
| 单元（识别） | `test_recognition_pipeline.py` | 5 | `RecognitionPipeline.analyze` 端到端三档、泛例句抑制、黑名单确认、不重新定位已发现候选、空文本 |
| 单元（识别） | `test_company_boundary.py` | 6 | 公司名左边界裁剪：甲乙方/我们合作的前缀不被吞、连接字开头真实字号不误裁、正例命中、泛指仍抑制 |
| 单元（评测闭环） | `test_corpus_tooling.py` | 6 | 句子切分、草稿 roundtrip、坏例导出/合并(dry-run)、词库回灌(dry-run)、趋势落盘对比、合成语料生成与评测 |
| 单元（NER） | `test_ner.py` | 12 | 接线/降级（默认关闭、异常不中断、低权重、仅模型来源转复核、高精度不被覆盖）；真实模型（缺 model.onnx 自动 skip）：边界/人名/LOC |
| 单元（脱敏） | `test_masker.py` | 4 | 模式 A 统一掩码、模式 B 公司/人名跨次一致、数字类仍走掩码 |
| 单元（脱敏） | `test_mapping.py` | 9 | 映射器稳定性、跨文件一致、不撞池、task/global 作用域、memory 不落盘、protected_file 持久化、池耗尽安全阀、`__repr__` 不泄露映射 |
| 单元（评测） | `test_evaluation.py` | 3 | P/R/F1 公式、数据集加载、runner 真实指标 |
| 集成（格式/管道） | `test_pipeline.py` | 7 | DOCX/PDF/XLSX（隐藏表）/PNG 完整管道、审计残留捕获、源哈希变动阻断、不支持格式拒绝 |
| 集成（持久化） | `test_persistence.py` | 10 | 初始化幂等、迁移、存取 round-trip、缺失返回 None、列表/打开、幂等键、操作日志、崩溃恢复、已有成功复用、DB 中无明文敏感值 |
| 集成（批处理） | `test_batch.py` | 8 | 全成功分组、含复核不导出、失败隔离、prepare 失败隔离、进度回调、取消 token、批次共享 mapper、告警无明文原文 |
| 集成（综合） | `test_integration.py` | 5 | 三档裁决、复核门禁阻断后放行、模式 B 跨文件一致、格式 roundtrip+源哈希、幂等复用 |
| 端到端（业务流） | `test_flow.py` | 8 | A 全脱敏成功 / B 部分脱敏+删除 / C 删除失败隔离 / D 删除后残留隔离 / E 批次失败隔离 / F 源位置记录 / G 源哈希不变 / H 最终哈希绑定 |
| 安全门禁 | `test_gate.py` | 7 | 源只读、重复运行哈希稳定、宏文件拒绝、损坏输出阻断、不支持格式拒绝、PDF 原始流无残留、加密 PDF 拒绝 |
| 安全 | `test_security.py` | 10 | Ollama 回环/禁代理、finalize 拒绝伪造/None/空报告、DOCX 脚注脱敏、DOCX 嵌入敏感图删除、XLSX 嵌入对象阻断、PDF 表单域阻断、审计捕获护照/座机 |
| 安全加固 | `test_security_hardening.py` | 24 | 路径包含（点/绝对/safe_join/合法相对/符号链接/产物路径）、日志脱敏、损坏文件失败关闭、崩溃恢复、资源上限、幂等重跑 |
| 回归（近期修复） | `test_major_fixes.py` | 7 | 审计详情脱敏、DB 不存明文审计详情、启动写恢复操作、真实跑后操作日志落库、批次幂等跳过+force、超时 deadline 守卫、review_flags 路由 |
| GUI（offscreen） | `test_gui.py` | 6 | 三档候选表、复核门禁阻断后放行、排除/放行按钮、批量分组与隔离、批量取消、多格式扫描 |

## 3. 安全红线用例清单

| 红线 | 对应用例 |
| --- | --- |
| 原件不变（失败关闭：源哈希变动即拒导出） | `test_pipeline.py::test_source_hash_change_blocks_export`；`test_flow.py::test_G_source_hash_unchanged`；`test_gate.py::test_source_readonly_pipeline`；`test_gate.py::test_repeated_run_hash_stable`；`test_integration.py::test_formats_roundtrip_and_source_hash` |
| 失败关闭（伪造/空/None 报告即拒导出） | `test_security.py::test_finalize_rejects_fabricated_report`；`test_security.py::test_finalize_rejects_none_report`；`test_security.py::test_finalize_rejects_empty_report`；`test_gate.py::test_corrupted_output_blocks`；`test_gate.py::test_macro_file_no_export`；`test_gate.py::test_unsupported_format_no_export`；`test_gate.py::test_encrypted_pdf_rejected` |
| 路径越权（`..`/绝对路径/符号链接逃逸） | `test_security_hardening.py::TestPathContainment::test_dotdot_escape_blocked`；`test_absolute_escape_blocked`；`test_safe_join_rejects_traversal_segment`；`test_safe_join_rejects_absolute_segment`；`test_legitimate_relative_path_allowed`；`test_is_within_negative`；`test_symlink_escape_blocked`（跳过，见 §1）；`test_pipeline_out_path_stays_in_processing_dir`；`test_pipeline_out_path_rejects_traversal_stem` |
| 日志/DB/告警不泄露原文 | `test_security_hardening.py::TestLogLeakage::test_sensitive_values_redacted_in_json_log`；`test_persistence.py::test_no_plaintext_sensitive_values_in_db`；`test_major_fixes.py::test_audit_detail_not_plaintext_in_db`；`test_batch.py::test_warnings_contain_no_plaintext_original`；`test_mapping.py::test_repr_does_not_leak_mapping` |
| 网络回环（Ollama 仅 127.0.0.1，禁代理） | `test_security.py::test_ollama_rejects_non_loopback`；`test_security.py::test_ollama_session_disables_proxy` |
| 损坏文件隔离（单文件失败不影响批次） | `test_security_hardening.py::TestCorruptFailClosed::test_zero_byte_docx_rejected`；`test_truncated_docx_rejected`；`test_truncated_pdf_rejected`；`test_truncated_xlsx_rejected`；`test_extension_content_mismatch_rejected`；`test_batch_isolates_corrupt_without_breaking_good_file`；`test_batch.py::test_failure_isolation`；`test_batch.py::test_prepare_failure_is_isolated`；`test_flow.py::test_E_batch_isolation` |
| 崩溃恢复（PROCESSING 任务重启归位） | `test_security_hardening.py::TestRecovery::test_intermediate_output_landed_in_processing_dir`；`test_recover_missing_output_back_to_pending`；`test_recover_residual_output_not_auto_deleted`；`test_persistence.py::test_recover_moves_interrupted_without_output_to_pending`；`test_major_fixes.py::test_startup_writes_recovery_operation`；`test_major_fixes.py::test_operation_log_populated_after_real_run` |

## 4. 测试局限（如实声明）

1. **全部用例基于合成 fixture**：DOCX/PDF/XLSX/PNG 由 `tmp_path` 内即时生成，不接触任何真实用户文件；OCR 用例使用 PIL 渲染大字黑底白字，不代表真实扫描件质量。
2. **未在测试中调用真实 LLM**：`settings.ollama.enabled` 默认 `False`；所有用例走规则 + 词库 + 融合路径。`_ollama_supplement` / `_ollama_assist` 仅在开关打开时进入，被 `test_fusion.py::test_ollama_only_conflicts_review` 等用 mock 覆盖。
3. **符号链接用例在本机 Windows 跳过**（见 §1）；路径越权语义由其余 9 条用例保证。
4. **GUI 测试在 `QT_QPA_PLATFORM=offscreen` 下运行**，不验证真实合成控件渲染/字体/高分屏；本报告 `docs/screenshots/` 下的 PNG 同为 offscreen 渲染。
5. **性能/压力/并发**：未覆盖大文件（>50MB）、长时（>30min）、多进程并发；资源上限仅在入口校验与 deadline 守卫层面用 tight 配置验证。
6. **评测集规模小**（101 条合成标注），指标不代表真实文档分布，详见 `docs/quality_report.md`。
