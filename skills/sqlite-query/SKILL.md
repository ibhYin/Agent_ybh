---
name: sqlite-query
description: 查询本地 SQLite 数据库时使用；覆盖表结构探查、聚合口径与常见字段陷阱
when: 用户问数据、统计、报表、"有多少条"，或需要先弄清表结构
---

# 查询本地 SQLite

数据库是**只读**的：写操作、ATTACH、危险 PRAGMA 一律被拒绝，别浪费时间尝试。

## 推荐步骤

1. **不确定表名** → 先 `sqlite__list_tables`
2. **不确定字段** → 再 `sqlite__describe_table`，重点看列类型与注释
3. **写 SQL** → 只用 `SELECT` / `WITH`
4. **结果为空** → 不等于"没有数据"，先检查 JOIN 条件与过滤值是否匹配

## 常见陷阱

- **日期是 TEXT**：`order_date` 存的是 `'2025-05-16'` 这种字符串，
  比较用字符串或 `date(order_date)`；不要假设它是 DATE 类型
- **状态值是中文**：`status` 里是 `'已完成'`、`'已退款'`，不是 `completed`
- **金额单位**：看 schema 里的注释，可能是分而不是元
- **NULL 与空串**：`WHERE col = ''` 抓不到 NULL，要用 `col IS NULL`

## 输出建议

聚合类问题先把口径说清楚（比如"按什么去重"），再给数字。
数字对不上时，回到第 1 步重新确认表结构，不要盲改 SQL。
