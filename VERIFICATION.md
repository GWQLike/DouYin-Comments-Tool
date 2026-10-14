# 验证说明

## 离线检查

在完成初始化的 Windows 环境中运行：

```powershell
$env:PYTHONUTF8 = '1'
$env:DOUYIN_DATA_DIR = Join-Path $PWD 'data\test-app'
New-Item -ItemType Directory -Force data | Out-Null
.\.venv\Scripts\python.exe -m pytest -q --basetemp=./data/test-run
.\.venv\Scripts\python.exe -m backend.check_engine
npm.cmd --prefix frontend run build
```

`data/test-run` 专用于测试，pytest 会清理这个临时目录，不要指向真实数据目录。`backend.check_engine` 只检查内核、补丁和本机 Chrome，不启动采集。

| 测试范围 | 主要检查 |
| --- | --- |
| 评论 | 主评论、回复、分页失败、停止和恢复 |
| 身份 | 公开抖音号、资料缺失、缓存冲突与待核实状态 |
| 搜索 | 繁简体、近似匹配、时间边界与排序 |
| 喜欢／推荐 | 独立接口、公开状态、数量去重、行为时间和断点 |
| 导出 | UTF-8 BOM、字段、空行、顺序、筛选和 TXT 字节一致性 |
| 内核集成 | 实际调用 MediaCrawler 主引擎及客户端，禁用旧采集方法后仍可执行模拟任务 |
| 会话与控制 | 浏览器复用、取消、单并发、退避、持久冷却与人工验证恢复 |

测试使用模拟网络和隔离数据库，不需要扫码。历史速度基准用于比较旧策略；当前生产策略以 `stable_policy.py` 为准。

## 最近结果

2026-10-14 发布整理检查：291 项通过（45.19 秒）；MediaCrawler 内核与本机 Chrome 检查、依赖锁校验、前端类型检查和生产构建均通过。测试仍有一条 Starlette/httpx 弃用提示。本轮未请求抖音真实数据，此记录不代表真实平台接口始终可用。

## 真实平台验证

登录、平台接口和验证提示需要在本机单独确认。真实采集前应限制数量、请求数或时间，遇到验证按界面提示人工处理。不要把真实账号、评论、导出文件或浏览器登录目录作为测试夹具提交到仓库。

本仓库不附带个人实测记录、生产数据库或已登录会话。离线测试通过不能证明某个视频的全部评论可获取，也不能保证喜欢／推荐列表公开。
