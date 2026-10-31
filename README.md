# Diag Hub（集成诊断）

Home Assistant 自定义集成，用来回答一个很具体的问题：**「到底是哪个集成在刷状态、在吃内存、在漏监听器、在刷日志？」**

它不是一个面向终端用户的插件，而是开发 / 排查期的自检工具。所有统计都在进程内做轻量聚合，数据通过一个自建 HTML 面板呈现，面板可以从设备页「访问」以悬浮窗形式打开，不占用侧边栏。

- 版本：`0.6.4`
- 依赖：`frontend`、`http`（HA 内置）
- 单实例：同一个 HA 只允许添加一次

---

## 1. 四块能力

| 能力 | 开关 | 统计内容 | 做法 | 开销 | 重启后 |
|:--|:--|:--|:--|:--|:--|
| 活动统计 | `switch.diag_hub_activity` | 状态写入次数（按集成 / 按实体）、asyncio 任务归属、进程 RSS | 订阅 `state_changed`，只做 dict 自增 | 很低，可常开 | 自动恢复 |
| 内存追踪 | `switch.diag_hub_memory` | tracemalloc 分配按集成汇总、最大分配点 | `tracemalloc.start(4)` + 每 60 秒采样 | 高，仅排查时开 | 不恢复 |
| 监听器统计 | `switch.diag_hub_listeners` | 每个集成的监听器存活 / 峰值 / 累计注册 | 临时包装 `hass.bus.async_listen`，从调用栈认归属 | 低 | 不恢复 |
| 日志计数 | `switch.diag_hub_logs` | 每个集成 warning / error / critical 条数 | 挂 root logger 的 Handler，只计数 | 低 | 不恢复 |

设计上的硬约束：所有时间序列都用 `deque(maxlen=...)` 环形缓冲，关闭时真正卸载订阅并清空缓冲，**工具本身不能变成下一个泄漏源**。

---

## 2. 安装

1. 把 `diag_hub` 整个目录放到 `/config/custom_components/diag_hub/`（必须包含 `www/` 子目录，面板和悬浮窗模块都在里面）。
2. 重启 Home Assistant。
3. 「设置 → 设备与服务 → 添加集成 → 搜索 `Diag Hub` / `集成诊断`」，点一次即可。

目录结构：

```
custom_components/diag_hub/
├── __init__.py        入口：注册视图、服务、平台
├── const.py           常量（窗口长度 / 条数上限 / API_TOKEN）
├── runtime.py         追踪器容器（统一开关与卸载）
├── activity.py        活动统计（状态写入 / 任务 / 进程）
├── memory.py          tracemalloc 归因（usage / snap / diff）
├── listeners.py       事件总线监听器归属
├── logs.py            日志计数
├── sensor.py          6 个诊断传感器
├── switch.py          4 个开关
├── entity.py          设备信息（所有实体挂在同一个设备下）
├── page.py            诊断页面视图 + 悬浮窗模块注入 + 设备链接
├── http_api.py        GET /api/diag_hub
├── services.py        服务 diag_hub.relink_device
├── config_flow.py     单实例配置流
├── manifest.json      集成清单（版本 / 依赖）
├── strings.json       配置流文案（HA 基准翻译文件）
├── services.yaml      服务说明（界面上的中文描述）
├── translations/      翻译
└── www/
    ├── page.html              面板本体（原生 HTML/CSS/JS，无构建）
    └── frontend/diag-embed.js 悬浮窗模块（Shadow DOM，前端注入）
```

---

## 3. 打开面板

三种方式，任选：

| 方式 | 地址 | 说明 |
|:--|:--|:--|
| 设备页「访问」 | 设置 → 设备与服务 → 集成诊断 → 右上角 | 被前端模块接管，在当前页面弹出**可拖动 / 可缩放悬浮窗**（iframe 加载面板） |
| 直链 | `http://<你的HA地址>:8123/diag_hub/page` | 整页打开，不依赖 Lovelace |
| 接口 | `http://<你的HA地址>:8123/api/diag_hub?a=all` | 返回 JSON，脚本 / 自动化可直接取 |

悬浮窗模块通过 `frontend.add_extra_js_url` 注入，只拦 `/diag_hub/page` 这一个链接；中键、`Ctrl+点击`、模块注入失败时都会退回普通新标签页打开。

> 早期版本（0.2 / 0.3）注册过的 Lovelace 仪表盘 `diag-hub` 会在启动时自动清理。

---

## 4. 实体

所有实体挂在同一个设备「集成诊断」下（`entry_type: service`）。

### 4.1 传感器（每 30 秒刷新）

| 实体 ID | 状态 | 主要属性 |
|:--|:--|:--|
| `sensor.diag_hub_events_per_min` | 全局状态写入速率（次/分，按运行时长折算） | `events_total`、`rate_stable`（运行满 60 秒才可信）、`uptime_seconds`、`window_minutes`、`top[]`（按集成的 `domain/per_min/events/entities/tasks`）、`proc`（`vmrss`/`threads` 等）、`allocated_blocks` |
| `sensor.diag_hub_top_integration` | 窗口内最活跃的集成域名 | `ranking[]`（`domain/per_min/last_minute/entities/tasks`） |
| `sensor.diag_hub_tasks` | 当前 asyncio 任务总数 | `by_domain`、`by_name`（任务名已归一化：长 hex / 换行会被替换成 `*`） |
| `sensor.diag_hub_memory` | 内存占比最大的集成（未开启时为 `off`） | `enabled`、`total_mb`、`total_blocks`、`integrations[]`（`domain/mb/blocks/percent`）、`top[]`（`trace/kb/count`） |
| `sensor.diag_hub_listeners` | 存活监听器总数 | `enabled`、`patch_error`（包装失败原因）、`integrations[]`（`domain/live/peak/registered`）、`by_event[]` |
| `sensor.diag_hub_logs` | warning 及以上总条数 | `enabled`、`integrations[]`（`domain/warning/error/critical/total/last`） |

属性条数上限：传感器只放 **前 15 条**（`TOP_N`），HTTP 接口放到 **前 60 条**（`HTTP_TOP_N`）——HA 单个实体属性超过 16KB 会被 Recorder 丢弃，这条线必须守住。

### 4.2 开关

| 实体 ID | 作用 | 关闭后 | 重启后 |
|:--|:--|:--|:--|
| `switch.diag_hub_activity` | 活动统计总开关 | 取消订阅并清空全部缓冲 | **自动恢复**（开销极低，适合常开） |
| `switch.diag_hub_memory` | tracemalloc 归因 | `tracemalloc.stop()` + 丢弃基线 | 不恢复（防止忘了关） |
| `switch.diag_hub_listeners` | 监听器归属 | 还原事件总线，不留实例属性 | 不恢复 |
| `switch.diag_hub_logs` | 日志计数 | 摘掉 root logger Handler | 不恢复 |

开关的 `is_on` 直接读追踪器自身的 `enabled`，启动失败时界面立刻回到 `off` 并弹出真实原因（`HomeAssistantError`），不会出现「点开 2 秒后自己关掉却没提示」。

---

## 5. 面板说明

页面每 5 秒拉一次 `/api/diag_hub?a=all`，右上角「暂停」可断开轮询。内存区点「当前分布 / 看增量」后进入手动视图（数据不再跟随自动快照），点「回到实时」恢复自动跟随。

| 区块 | 内容 | 判读要点 |
|:--|:--|:--|
| 顶部胶囊 | 四个开关的开 / 关 | 灰点表示该项在关闭状态，对应区块会是空数据 |
| KPI | 写入速率、事件总数、任务、监听器存活、warning 及以上、内存总量、进程 RSS、运行时长 | 写入速率是「累计 / 运行分钟」折算，运行不满 1 分钟会虚高，卡片会显示「统计中」 |
| 写入排行 | 按集成（滚动 60 分钟窗口）+ 写入最频繁的实体 | 实体榜是**累计值**（开关打开之后），默认列出全部（最多 200 条），区域内可滚动；表头「实体」过长会省略，鼠标悬停看全名 |
| 任务 | 总数、按集成、同名任务 | 同名任务大量堆积通常意味着某处 `create_task` 没做去重或没被回收 |
| 内存归因 | 按集成分布 + 最大分配点 | 只在 tracemalloc 开启后才有数据；「看增量」需要先「设基线」 |
| 监听器 | 存活 / 峰值 / 累计注册、按事件 | 累计注册远大于存活，说明有注册不注销的地方；峰值高说明曾同时存在大量监听器 |
| 日志计数 | 每个集成 error / critical / warning / 合计 + 最近一条摘要 | 只留摘要，不存正文；摘要已做 `%s` 参数格式化 |

右上角「复制报告」会把当前视图拼成一份 Markdown（含版本、环境、关注点、各表），直接贴给别人排查。带虚线下划线的文字支持鼠标悬停看解释。

---

## 6. 服务

| 服务 | 参数 | 说明 |
|:--|:--|:--|
| `diag_hub.relink_device` | 无 | 重新计算面板绝对地址并写回设备的 `configuration_url`。改过 HA 内网 / 外网地址、反向代理或端口映射后调用一次，不用重启 |

设备页「访问」的地址在 HA 启动完成（`homeassistant_started`）后自动写入；拿不到绝对地址时（既没有 `internal_url` 也没有 `external_url`，且 `network.get_url` 也失败）会跳过，等下次调用本服务补上。

---

## 7. HTTP 接口

```
GET /api/diag_hub?t=<API_TOKEN>&a=<action>
```

| `a` | 返回 |
|:--|:--|
| `status`（默认） | 活动快照（含 `integrations`、`hot_entities`、`tasks`、`proc`） |
| `top` | 同上，去掉 `tasks` |
| `tasks` | 任务总数与按集成 / 按名称的归属 |
| `listeners` | 监听器快照 |
| `logs` | 日志计数快照 |
| `all` | 面板用的全量：版本、环境、条目概况、活动、监听器、日志、内存缓存 |
| `memory` | 立刻算一次 tracemalloc 存活分布（阻塞，走 executor） |
| `snap` | 建立 tracemalloc 基线 |
| `diff` | 与基线对比的增量（按集成，过滤 1KB 以下噪声） |
| `memstart` / `memstop` | 开启 / 关闭 tracing（免去先去开关页点一下） |

示例：

```bash
curl -s "http://192.168.1.10:8123/api/diag_hub?a=top" | head -c 800
```

---

## 8. 安全提醒

- `/api/diag_hub`、`/diag_hub/page`、`/diag_hub/frontend/diag-embed.js` 都是 `requires_auth = False`，**只要能在网络上访问 HA 端口，就能读到这些数据**。
- 仅适合内网 / 测试机。若 HA 对外可达，请把 `const.py` 里的 `API_TOKEN` 填上，并在页面 URL 上带 `?t=<token>`（接口则带 `&t=<token>`）。
- 静态模块走白名单路由：`/diag_hub/frontend/{file_name}` 只放行 `diag-embed.js`，其他文件名一律 404。

---

## 9. 开销与注意事项

1. **tracemalloc 会与 `memspy` 等工具互相干扰**，同一时间只开一个；开启后 CPU / 内存开销明显上升，排查完请立刻关闭。
2. tracemalloc 只统计**开启之后新分配**的对象，所以刚开始 `total_mb` 必然远小于进程 RSS（面板会显示「已观察 N 秒」提示）。两个数字不可直接比较。
3. 统计是**从开关打开那一刻起算**的：监听器只记录之后发生的注册，已经跑起来的集成要重载一次才会重新登记。
4. 集成归因靠路径识别：`custom_components/<domain>/...` → 该集成；HA 核心 → `homeassistant`；第三方包 → `pkg:<包名>`；其他 → `other`。装在 HA 核心里的平台（如 `esphome` 的实体写入）会被算到 `homeassistant` 或对应包名。
5. 实体级热度有上限（4000 条），超过时每分钟只保留最忙的一半，长跑不会无限增长。
6. 面板和悬浮窗模块都按 mtime 缓存读取，改完 `www/` 下的文件刷新浏览器即可生效，不必重启；改 `.py` 必须重启或重载集成。

---

## 10. 常见问题

**Q：为什么监听器只有几条？**
A：只统计开关打开之后发生的注册。先打开 `switch.diag_hub_listeners`，再重载想观察的集成（或重启 HA）。

**Q：内存总量只有几十 MB，明显不对？**
A：tracemalloc 只算开启后新分配的对象。要看真实占用用 KPI 里的「进程 RSS」（读 `/proc/self/status`），它和 tracemalloc 是两套口径。

**Q：设备页没有「访问」按钮？**
A：需要 HA 能推算出绝对地址（配置了 `internal_url` / `external_url`，或 network 能推导）。手动调用一次 `diag_hub.relink_device`，或先确认 `/config` 里的 `configuration.yaml` 有 `homeassistant: internal_url:`。

**Q：日志里只看到每个集成最后一条，别的呢？**
A：这是刻意设计——只计数、只留摘要，避免日志风暴时把正文堆在内存里。需要看全部请用 HA 自带的日志页。

**Q：日志摘要里出现 `%s`？**
A：旧版本用 `record.msg` 取到的是未格式化模板。0.6.4 起改用 `record.getMessage()`，摘要会显示真实实体名与数值。

**Q：页面提示 `取数据失败：HTTP 503`？**
A：`diag_hub` 条目没加载或正在重载，检查「设置 → 设备与服务」里该条目的状态。

---

## 11. 版本

当前 `0.6.4`。

- 面板的「写入最频繁的实体」改为可滚动列表（最多 200 条，`HOT_ENTITY_N`），报告里列出前 30 条。
- 日志摘要改用 `record.getMessage()`，不再出现 `%s` 占位符。
- 修掉事件循环内的阻塞调用：`platform.platform()`（会 `open()` 解释器可执行文件）与 `/proc/self/status` 改为在 executor 里读取并缓存，消除 `homeassistant.util.loop` 的 blocking call 告警。
