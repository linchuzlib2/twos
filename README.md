# Twos（Flask 版）

一个 Twos App 的完整克隆：把事情从脑子里清空 ✌

## 功能

- **每日清单**：今天的待办；未完成事项自动顺延到今天（并标记来源日期）
- **日历**：月视图查看每天的事项数量，点击进入任意一天
- **自定义清单**：创建/重命名/删除清单，事项可在日清单与自定义清单之间移动
- **星标 / 搜索 / 提醒**：星标事项、全文搜索、定时提醒（浏览器通知 + 页面提醒）
- **富文本编辑**：加粗/斜体/标题/列表/引用/链接，每个事项支持富文本
- **多附件/图片上传**：编辑器内一次选择多个文件，图片直接插入正文，附件生成卡片，全部直传**阿里云 OSS**；点击 Word/Excel/PPT 附件由本机 Office 通过 WebDAV 编辑，本地缓存由 Office 管理，保存时自动覆盖同一 OSS 附件
- **附件全文搜索**：支持 UTF-8/UTF-16/GB18030 文本、DOCX/XLSX/PPTX 和可提取文字的 PDF；提取器升级后自动重建历史附件索引
- **附件在线编辑**：TXT、Markdown、CSV、JSON 等文本附件可在浏览器中修改，按 Ctrl+S 覆盖保存原 OSS 文件；Word/Excel/PPT 仍使用本机 Office 打开
- **数据库自动备份**：每次成功写操作在返回前生成 SQLite 一致快照并同步上传到阿里云 OSS；启动时先从 OSS 校验并恢复数据库（应对 Render 临时磁盘）
- **中国大陆时间**：所有日期/时间均按北京时间（Asia/Shanghai）处理

## 部署到 Render

1. 推送本仓库到 GitHub
2. Render → New → Web Service → 连接仓库（项目根目录已含 `render.yaml`）
3. 配置环境变量：

| 变量 | 说明 |
|---|---|
| `APP_PASSWORD` | 可选，访问密码（保护线上数据） |
| `OSS_ACCESS_KEY_ID` | 阿里云 AccessKey ID |
| `OSS_ACCESS_KEY_SECRET` | 阿里云 AccessKey Secret |
| `OSS_BUCKET` | OSS Bucket 名称 |
| `OSS_ENDPOINT` | 如 `oss-cn-beijing.aliyuncs.com` |
| `OSS_PREFIX` | 可选，对象前缀，默认 `twos/` |
| `OSS_PUBLIC_BASE_URL` | 可选，自定义域名（CDN），如 `https://cdn.example.com` |
| `OSS_DB_BACKUP_KEY` | 可选，数据库备份对象名，默认 `twos/backup/twos.sqlite` |

> OSS Bucket 建议设置为公共读（public-read），或在 `OSS_PUBLIC_BASE_URL` 指向绑定 CDN 的公共域名。

## 本地开发

```bash
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5000
```

未配置 OSS 环境变量时，附件保存在本地 `data/attachments/`，可通过 `/local-files/...` 访问。

## 技术栈

Flask 3 + SQLite（WAL）+ 阿里云 OSS（oss2）+ 原生 JS 富文本编辑器，无任何前端框架依赖。
