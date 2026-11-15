# 站内搜索连续改写分析

把站内搜索的**连续改写查询串成路径**，在网页上分析零结果之后的改写是否找到结果、
是否被点击、是否最终转化。纯本地运行：Python 标准库 + 原生 HTML/JS/SVG，
**不接搜索引擎、不接语言模型、零第三方运行时依赖**。

## 快速开始

```bash
# 1. 生成确定性合成数据（data/events.jsonl）
python3 -I scripts/generate_data.py

# 2. 初始化数据库、灌入数据、构建派生表
python3 -I scripts/seed.py

# 3. 启动网页（默认 http://127.0.0.1:8000）
python3 -I run.py
```

测试：

```bash
python3 -I -m unittest discover -s tests -v     # 后端 13 项行为测试
```

## 需求与实现对照

| 需求 | 实现位置 / 规则 |
|---|---|
| 导入查询、结果数、点击、加购、会话 | `app/ingestion.py` 导入 JSONL；`search/click/add_to_cart/order` 四类事件 |
| 连续改写串成路径 | `app/pipeline.py`：同 `session_id + tab_id`、按事件时间排序、间隔超空闲阈值（默认 30 分钟）即切分新路径 |
| 大小写/空格规范化，原始词保留 | `app/normalization.py`：NFKC + casefold + 空白折叠存 `query_norm`；`query_raw` 原样保留，页面同时展示 |
| 同义词只按页面维护的明确映射合并 | `canonicalize()` 只查 `mapping_entries`，无词干提取/模糊匹配/模型；未命中即自身 |
| 零结果后改写：是否找到结果/点击/转化 | 每个零结果节点在**固定观察窗**（默认 30 分钟）内统计恢复、点击、加购、转化 |
| 观察窗固定，结束太早标未完整观察 | `window_observed=0` 且记录原因：`session_ended_early` / `truncated_window`；率指标分母只用完整观察节点 |
| 重复埋点识别 | 相同 `event_id`，或相同业务指纹（session+类型+时间+查询/商品+标签页）即判重，含换 id 重发的信标 |
| 后台自动请求识别 | `is_background=true` 或来源 `api_health` / `suggest_prefetch`，排除并保留载荷审计 |
| 跨标签页行为识别 | 路径按 tab 区分；点击/加购/转化按 **session 作用域**归因，第二个标签页的转化仍归功原搜索 |
| 查看查询路径 | 「查询路径」视图：节点流图 + 零结果/回退/未完整观察/点击/转化标记，可打开完整会话时间线 |
| 比较词类与版本 | 「词类 / 版本对比」视图：显式关键词规则分词类（未命中归 other），A/B 实验版本同口径对比 |
| 修改映射并对照指标变化 | 「同义词与词类映射」视图：克隆版本→编辑→激活，派生表整体重建，页面展示激活前后指标 diff |
| 点击每个指标查到对应会话 | 总览 KPI 卡片可点击下钻到会话列表，再点会话查看路径与原始事件时间线 |
| 本地合成数据 | `scripts/generate_data.py`，固定随机种子，含连续零结果、回退查询、迟到（乱序）转化等场景 |

## 合成数据内置场景

`scripts/generate_data.py` 手工构造了 9 个边界会话（s1–s9）加约 92 个随机会话：

- **s1** 连续零结果链 → 改写恢复 → 点击/加购/转化
- **s2** 零结果后**回退查询**（放宽词、结果数达阈值，节点打 `is_fallback`）
- **s3** 文件顺序乱序的**迟到转化**，验证按 `event_time` 而非文件顺序归因
- **s4** 会话在观察窗内结束 → 标记未完整观察，不计入率分母
- **s5** **跨标签页**：t1 搜索、t2 转化，路径分离但归因到会话
- **s6** 大小写/空格规范化，原词保留
- **s7** 三种**重复埋点**（指纹重复、id 重复）
- **s8** 后台/预取/健康检查自动请求被排除
- **s9** 明确同义词映射合并（跑鞋/运动鞋 → 同一规范词）

## 数据模型分层

```
raw_events / excluded_events     原始事实（接受 / 带规则拒绝，可审计）
        │  ingestion（判重、后台过滤、校验）
        ▼
query_paths / query_nodes        派生事实（路径、节点、窗口归因）——可随时整体重建
derived_events                   派生的非搜索事件，供会话详情
mapping_versions/mapping_entries 版本化同义词映射（激活版本驱动重建）
word_class_rules                 显式词类关键词规则
config                           观察窗、空闲阈值、回退结果数阈值
```

映射编辑采用**克隆快照**而非原地改：旧版本保留，可随时切换并对照指标。

## 目录结构

```
app/
  db.py             SQLite schema 与配置
  normalization.py  机械规范化 + 显式映射/词类规则
  ingestion.py      JSONL 导入与三类排除规则
  pipeline.py       路径串接、固定窗口归因、回退检测、重建
  metrics.py        聚合指标、下钻查询
  mapping.py        映射版本与词类规则管理
  server.py         stdlib HTTP 服务（/api/* + 静态页）
  web/              index.html / styles.css / app.js（原生 SVG 图表）
scripts/
  generate_data.py  合成数据生成器
  seed.py           一键初始化与灌库
tests/              unittest 行为测试
data/               events.jsonl 与 SQLite 库（库文件被 gitignore）
```
