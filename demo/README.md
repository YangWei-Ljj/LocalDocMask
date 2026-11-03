# demo/ 合成演示数据与运行器

本目录包含一套**完全合成**的演示资产，用于在不接触任何真实用户文件的前提下，
跑通 LocalDocMask 的三档识别、复核门禁、失败关闭、模式 B 跨文件一致性与 GUI 截图。

> 所有个人信息（手机/身份证/邮箱/银行卡）均为虚构；企业名（杭州云启科技有限公司、
> 星轨数据服务（示例）有限公司 等）与 `resources/blacklist.txt` 一致，均为合成。
> 不对应任何真实个人或机构。

## 文件清单

| 文件 | 设计意图 | 预期三档结果 |
| --- | --- | --- |
| `data/业务说明.docx` | 正文（泛指 + PII）+ 表格（黑名单公司 + 备用手机）+ 页脚（白名单）+ 脚注（领域词） | 泛指引用 → **已抑制**；手机/身份证/邮箱/银行卡/黑名单公司/项目代号-苍梧 → **自动确认**；整文件无待复核项 → **PASSED** |
| `data/合同材料.pdf` | 第 1 页含公开企业「中国石油股份有限公司」+ 手机；第 2 页图文混排（嵌入图 + 正文 >40 字触发 `review_flag`） | 手机 → **自动确认**；中国石油 → **待复核**；图文混排 → **review_flag**；整文件 → **NEEDS_REVIEW，不自动导出** |
| `data/客户台账.xlsx` | 可见表 + 隐藏工作表「内部备忘」+ 隐藏行 + 隐藏列，藏 PII/黑名单公司/领域词 | 全部 PII/黑名单/领域词 → **自动确认**；无待复核项 → **PASSED**；隐藏行列/隐藏表内容同样被识别 |
| `data/扫描件-身份证.png` | 白底黑字大字「扫描件 身份证…」+「手机…」，走 RapidOCR 路径 | 身份证/手机 → **自动确认**；像素级黑框覆盖 → **PASSED** |
| `data/损坏样本.docx` | 57 字节截断假 DOCX（不是合法 zip） | `prepare/scan` 抛 `ValueError`，被 BatchService 失败隔离 → **AUDIT_FAILED**，不影响同批次其他文件 |

## 复跑

```powershell
# 1. （重新）生成合成文件
.venv\Scripts\python.exe demo\make_demo_data.py

# 2. headless 跑完整闭环，输出同时写入 demo/demo_output.txt
.venv\Scripts\python.exe demo\run_demo.py

# 3. offscreen 截取 GUI 界面到 docs/screenshots/
.venv\Scripts\python.exe demo\make_screenshots.py
```

## 隔离说明

- `run_demo.py` 与 `make_screenshots.py` 都在启动最早期把 `LOCALDOCMASK_HOME`
  指向 `demo/_home/`（或 `demo/_shot_home/`），**不会**写入项目根 `data/`。
- `_home/` 与 `_shot_home/` 在每次运行前被清空，可重复运行。
- 不修改 `resources/`、`config/`、`data/`。
