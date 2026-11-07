---
name: filesystem
description: 读写沙箱目录内的文件时使用；说明路径规则、搜索技巧与覆盖前的检查
when: 用户要求查看、搜索、保存文件，或需要把结果写到文件
---

# 沙箱内的文件操作

所有路径都**相对沙箱根目录**（默认是项目 `workspace/`）。
绝对路径与 `../` 越界会被直接拒绝——这是设计如此，不是 bug。

## 步骤

1. **不知道有什么** → `filesystem__list_files`（可传 `recursive=true`）
2. **找内容** → `filesystem__search_text`，给关键词而不是整段话
3. **读取** → `filesystem__read_text`，大文件用 `max_chars` 限制
4. **写入** → `filesystem__write_text`，父目录会自动创建

## 注意

- 搜索会跳过二进制文件（`.pyc` / 图片 / `.db`），找不到先确认是不是这类
- **写入是覆盖**，不是追加。要保留原内容就先读出来拼接
- 文件名用相对路径，如 `notes/summary.txt`，不要以 `/` 开头

## 写文件前的检查

覆盖前先确认目标是否存在：若用户没说"覆盖"，先 `read_text` 看一眼，
发现已有内容就提醒用户，而不是直接覆盖。
