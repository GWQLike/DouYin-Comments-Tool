# 第三方软件说明

## MediaCrawler

- 项目：[NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)
- 固定提交：`5d547f45862ca2d4b404e5a51775879d579b29d4`
- 对应源码：[GitHub 固定版本](https://github.com/NanmiCoder/MediaCrawler/tree/5d547f45862ca2d4b404e5a51775879d579b29d4)
- 本地源码：`vendor/MediaCrawler`
- 版权所有：`Copyright (c) [2024] [relakkes@gmail.com]`
- 主许可证：**NON-COMMERCIAL LEARNING LICENSE 1.1 / 非商业学习使用许可证 1.1**，完整原文保留于 [vendor/MediaCrawler/LICENSE](vendor/MediaCrawler/LICENSE)。

本工具通过正常导入调用上游 `DouYinCrawler` 主引擎、抖音客户端、登录及 CDP 管理器，用于视频评论、回复、公开作者信息与公开喜欢列表的读取。喜欢列表、推荐列表、账号搜索、存储与控制策略由本项目扩展；上游源码有版本化兼容补丁，见 `vendor/patches/README.md`。应用界面为本项目的 React 前端，未使用上游 `webui` 作为运行界面。上游 `webui` 的实现不随本工具发布，其独立许可证保留于 [vendor/MediaCrawler/webui/LICENSE](vendor/MediaCrawler/webui/LICENSE)。源文件内的版权声明和其他许可证继续适用。

上游主许可证允许非商业学习目的下使用、复制、修改和合并，要求保留版权及许可声明；禁止大规模爬虫或干扰平台运营，商业使用须取得版权所有者书面同意。上述介绍只用于说明来源，不替代许可证原文，也不将上游授权改为其他许可证。

## 喜欢列表适配的研究参考

本项目自行编写适配逻辑，参考以下项目维护者公开的端点、参数及字段资料，未将这些项目作为运行时依赖：

- [F2 抖音 API 端点](https://github.com/Johnserf-Seed/f2/blob/main/f2/apps/douyin/api.py)及 [UserLike 参数模型](https://github.com/Johnserf-Seed/f2/blob/main/f2/apps/douyin/model.py)。
- [TikTokDownload 字段资料](https://github.com/Johnserf-Seed/TikTokDownload/wiki/APIv1.0)：可选 `show_favorite_list` 布尔字段。缺失字段不视为未公开。
- [独立用户搜索端点修复记录](https://github.com/Youhai020616/douyin/issues/1)及[网页用户搜索参数示例](https://github.com/xiaoxiaodafeng/Douyin_messages/blob/main/send_dm/__main__.py)。仅参考搜索请求参数，不使用其私信功能。

这些非官方接口资料不构成平台稳定性保证。用户搜索返回 `search_nil_info=verify_check` 时，本工具按人工验证要求处理，不将其视为账号不存在。

## 推荐列表适配的研究参考

推荐列表适配由本项目自行实现，继续使用 MediaCrawler 请求客户端。端点与参数依据 [ikenxuan/amagi 固定版本的 URL 构造](https://github.com/ikenxuan/amagi/blob/fcc55ac2e9129cd141938f65496cfd18eb949da2/packages/core/src/platforms/douyin/api.ts)、[推荐列表分页实现](https://github.com/ikenxuan/amagi/blob/fcc55ac2e9129cd141938f65496cfd18eb949da2/packages/core/src/platforms/douyin/endpoints/userRecommendList.ts)与[返回结构](https://github.com/ikenxuan/amagi/blob/fcc55ac2e9129cd141938f65496cfd18eb949da2/packages/response-types/src/generated/douyin/UserRecommendList/UserRecommendList_V0.ts)，未将该项目作为运行时依赖。

使用 `/aweme/v1/web/familiar/recommend/feed/`，按 `max_cursor` 翻页并兼容布尔 `has_more`。[抖音隐私政策](https://www.douyin.com/draft/douyin_agreement/douyin_agreement_privacy.html?id=6773901168964798477)分别说明推荐与点赞的可见性控制。本工具不使用喜欢权限字段推断推荐权限，也不猜测未验证的数字权限枚举。明确私密文案的离线测试不等同于真实私密账号验证。

推荐行为时间候选字段是 `friend_recommend_info.label_user_list[].recommend_time`。上述 Web 返回类型存在该字段；[Android 推荐弹窗消费代码](https://github.com/cxxsheng/TiktokSource/blob/2c6fb7c918b71713c23c4464e7fbb51b752f54a2/com/ss/android/ugc/aweme/feed/recommend/postscript/FRPostscriptDialogFragmentGridStyle.java)将对应 `recommendTime` 乘以 1000 后格式化，因此采用 Unix 秒。此消费代码来自第三方反编译仓库，Web 与移动端语义一致属于推断，未以真实返回确认。本工具只接受目标用户身份匹配且时间无冲突的标签；不使用语义未确认的 `aweme_date.date_map`，也不把推荐标签当作点赞时间。

## 请求与会话适配参考

请求适配参考 MediaCrawler 固定版本的 [并发与请求间隔配置](https://github.com/NanmiCoder/MediaCrawler/blob/5d547f45862ca2d4b404e5a51775879d579b29d4/config/base_config.py)、[抖音采集实现](https://github.com/NanmiCoder/MediaCrawler/blob/5d547f45862ca2d4b404e5a51775879d579b29d4/media_platform/douyin/core.py)与 [CDP 指南](https://github.com/NanmiCoder/MediaCrawler/blob/5d547f45862ca2d4b404e5a51775879d579b29d4/docs/CDP%E6%A8%A1%E5%BC%8F%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97.md)中的浏览器复用建议。当前使用单并发稳健策略；连接池、请求快照、短期会话缓存、持久冷却、作者资料合并及签名线程由本工具适配实现。签名算法保持固定版本，通过显式兼容补丁接入扩展。

## 运行时依赖

Python 依赖声明与锁定版本分别见 `pyproject.toml`、`uv.lock`；前端依赖声明与锁定版本分别见 `frontend/package.json`、`frontend/package-lock.json`。

项目使用 FastAPI、Uvicorn、HTTPX、Pydantic、Playwright、OpenCC、RapidFuzz、React、TypeScript、Vite 等第三方组件，以及各自的传递依赖。它们仍适用各自分发包中的许可和版权声明；安装后可在 `.venv/Lib/site-packages` 对应包或 `.dist-info` 目录、`frontend/node_modules` 对应包目录中查阅。采集通过 Playwright 的 CDP 连接使用本机 Google Chrome；旧 Chromium 目录可保留，但不再由生产采集入口启动。

本文件不为这些独立组件授予新的许可，也不覆盖上游源码或依赖自身的条款。分发包含第三方软件的副本时，应一并保留对应声明与许可文件。
