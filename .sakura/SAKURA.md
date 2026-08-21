# 项目概述：astrbot_plugin_dailylimit

## 1. 项目简介
这是一个面向 AstrBot 的“每日调用限制”插件，用于按用户、群组和时间段管理 AI 接口调用次数，防止资源滥用并提升服务稳定性与公平性。

## 2. 技术栈
- **Python 3.10+**：插件核心实现语言
- **HTML**：Web 管理界面前端模板
- **AstrBot 插件体系**：作为 AstrBot 的扩展组件运行
- **Redis**：用于存储限流、统计与状态数据
- **Web 管理端**：提供可视化配置与管理能力
- **Ruff**：代码风格与静态检查工具（由 `run_ruff.py` 可见）
- **Jinja/模板渲染风格页面**：`templates/` 下包含登录页与首页

## 3. 项目结构
- `.github/workflows/`：CI/CD 或自动化流程配置
- `.sakura/docs/`：项目文档资源目录
- `core/`：核心业务模块，承担插件主要逻辑
  - `config_loader.py`：配置加载入口
  - `config_manager.py`：配置读写与管理
  - `help_manager.py`：帮助信息生成与组织
  - `limiter.py`：限流/调用次数控制核心逻辑
  - `logger.py`：日志封装
  - `message_builder.py`：消息内容构建
  - `messages_handler.py`：消息处理流程
  - `redis_client.py`：Redis 连接与操作封装
  - `redis_keys.py`：Redis 键名统一管理
- `templates/`：Web 界面模板
  - `index.html`：管理后台主页
  - `login.html`：登录页
- `main.py`：插件主入口
- `web_server.py`：Web 管理服务入口
- `metadata.yaml`：插件元数据与描述信息
- `requirements.txt`：Python 依赖列表
- `README.md` / `CHANGELOG.md` / `CONTRIBUTING.md`：说明、更新日志与贡献规范

## 4. 开发约定
从仓库结构可推断出以下开发规范：

- **模块分层清晰**：业务逻辑集中在 `core/`，入口文件只负责调度与组装。
- **功能解耦**：配置、限流、消息构建、Redis、日志等职责分别拆分为独立模块，便于维护与测试。
- **统一数据访问**：通过 `redis_client.py` 和 `redis_keys.py` 规范化 Redis 读写，避免键名散乱。
- **支持 Web 与命令双通道**：既有命令交互，也有 `web_server.py` 提供的图形化管理界面。
- **重构导向明显**：版本说明中多次提到模块拆分与架构重构，说明项目强调可维护性与渐进式演进。
- **配置驱动**：存在 `_conf_schema.json` 与 `config_manager.py`，推测插件配置采用结构化 schema 管理。
- **代码质量控制**：存在 `run_ruff.py`，说明项目重视格式规范与静态检查。
- **文档同步更新**：有 `CHANGELOG.md`、`CONTRIBUTING.md` 和较完整 README，体现较强的文档化习惯。