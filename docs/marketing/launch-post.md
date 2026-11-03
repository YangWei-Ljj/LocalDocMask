# LocalDocMask 开源发布文案

## 中文标题

LocalDocMask：不把合同和客户数据上传云端的本地文档脱敏工具

## 一句话介绍

LocalDocMask 是一款面向 Windows 的本地优先文档脱敏与独立审计工具，支持 PDF、DOCX、XLSX、图片等格式，在文件不上传、数据不出本机的前提下完成敏感信息识别、脱敏、复核和失败关闭审计。

## 适合发布的正文

合同、客户台账、员工信息、扫描件里往往包含姓名、手机号、身份证号、公司名和地址。真正麻烦的不是“替换几个词”，而是页眉页脚、批注、隐藏工作表、图片 OCR、文档元数据和嵌入对象都可能留下敏感信息。

很多在线工具要求先把文件上传到云端，这对法务、财务、人事和个人开发者都很难接受。

LocalDocMask 采用本地优先设计：

- 原件只读，处理前后校验 SHA-256；
- 规则、上下文、词库、OCR 与本地 NER 共同产生候选；
- 自动确认、人工复核、抑制放行三档处置；
- 独立审计会重新扫描正文、OCR、元数据和隐藏部件；
- 无法检查、仍有残留或产物哈希异常时失败关闭，不进入成功目录；
- 审计 JSON 绑定策略版本、配置哈希和工具版本，不写入原始敏感值；
- PDF、DOCX、XLSX/XLSM、PNG、JPEG、TIFF、BMP 按格式执行不同的安全处理。

项目采用 MIT License，源码已公开：

https://github.com/YangWei-Ljj/LocalDocMask

欢迎提交 Issue、参与测试或贡献新的文件格式与识别规则。

## 英文短版（Reddit / Hacker News / DEV）

Show HN: LocalDocMask – Local-first document redaction with fail-closed auditing

LocalDocMask is a Windows desktop tool for redacting sensitive information from PDF, DOCX, XLSX and images without uploading files to a cloud service.

It combines deterministic rules, OCR and optional local NER, then runs an independent audit over text, OCR, metadata and hidden document parts. If any content cannot be checked, the task fails closed and never enters the success directory.

Source (MIT): https://github.com/YangWei-Ljj/LocalDocMask

Feedback on detection quality, enterprise deployment requirements and additional file formats is welcome.

## 发布前注意

- 不要上传真实客户文件或包含敏感信息的截图。
- 使用仓库中的合成演示数据。
- 明确说明当前仍未完成企业代码签名和真实外部环境验证。
- 不要将合成评测指标宣传为真实文档精度。
