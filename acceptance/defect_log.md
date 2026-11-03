# LocalDocMask 缺陷日志

共发现 8 项缺陷（P0×3、P1×4、P2×1），全部修复并经回归测试验证。

| 编号 | 级别 | 缺陷 | 根因 | 修复 | 回归测试 | 状态 |
|---|---|---|---|---|---|---|
| SEC-001 | P0 | Ollama 请求经系统代理可能绕行外部 | requests 默认 trust_env=True | 改用 Session，trust_env=False、proxies 置空；并强制校验目标为主机回环地址 | test_ollama_rejects_non_loopback、test_ollama_session_disables_proxy | 已修复 |
| SEC-002 | P0 | 导出门禁可被伪造/空审计报告绕过 | finalize 直接信任 report.passed | finalize 校验 report 必须是 task.audit 真实产物、findings 非空、输出存在；新增原子 run() | test_finalize_rejects_fabricated/none/empty_report | 已修复 |
| SEC-003 | P1 | DOCX 脚注/尾注未提取未脱敏未审计 | python-docx 不解析这些部件 | 提取与重写改为直接读写压缩包 XML；审计从压缩包读取 | test_docx_footnote_detected_and_redacted | 已修复 |
| SEC-004 | P0 | DOCX 嵌入图像内敏感文字泄漏 | 仅检测未删除，审计不复查图像 | 检测到即拒绝导出；审计新增 DOCX 图像 OCR 复查 | test_docx_embedded_image_sensitive_blocks | 已修复 |
| SEC-005 | P1 | XLSX 嵌入 OLE 对象未检查 | 未检测 xl/embeddings | 检测到嵌入对象即阻止导出；审计复查输出包 | test_xlsx_embeddings_block | 已修复 |
| SEC-006 | P1 | PDF 表单字段值泄漏 | 未提取字段值，清空 API 不可靠 | 提取字段值检测；含值即阻止导出（失败关闭） | test_pdf_form_field_blocks | 已修复 |
| SEC-007 | P1 | 审计高风险规则缺护照/固话 | _HIGH_RISK 清单不全 | 补充护照号、固定电话；DOCX/XLSX 改为扫描全部 XML 部件 | test_audit_catches_passport_and_landline | 已修复 |
| SEC-008 | P2 | DOCX 自定义属性未清、存在死导入 | _clear_metadata 未处理 custom.xml | 清空 custom.xml 属性；移除无用导入；GUI 出错置 UNSAFE | 全套回归 | 已修复 |

## 已知限制（非缺陷，明确记录）

- LIMIT-01（P2）：审计/OCR 无超时，极端情况下可能长时间挂起（不会因此导出，但体验受影响）。
- LIMIT-02（P2）：GUI 无“取消扫描/处理”按钮。
- LIMIT-03（P3）：单字符自定义敏感词会被掩码，但独立审计按长度≥2 过滤（高风险数字类别不受影响）。
- LIMIT-04（P1）：无文字的图形印章/签名、二维码、条码、照片人脸不自动识别，提示人工复核。
- LIMIT-05（P1）：XLSM 宏、外部链接、OLE 嵌入对象、PDF 表单字段 → 一律阻止导出。
- LIMIT-06（P1）：图文混排页嵌入图像、DOCX 嵌入图像内检测到敏感文字 → 阻止导出，需人工复核。

## 附带发现（非产品缺陷）

- TEST-HYG-01（P2）：原测试套件将运行产物写入项目 data/ 目录，已改为测试时隔离到临时目录（tests/conftest.py 设置 LOCALDOCMASK_HOME）。
