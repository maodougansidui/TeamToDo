# TeamToDo V1.2

内网任务协同管理平台。当前应用位于 `TeamToDo V1.1/`，后端声明版本为 3.4.0。

## V1.2（2026-10-11）

Git 版本标签为 `V1.2`；为兼容现有部署，应用目录名和原 EXE 的后端版本号保持不变。

- 自动同步其他客户端的任务和反馈更新，保留编辑草稿及当前筛选。
- 修复“我的任务 → 工作台”残留筛选；显式点击工作台恢复全部可见任务。
- 修复顶部统计显示 `[object Object]`，并避免逾期、即将到期数量被明细的 20 条上限截断。
- 包含源码与原 EXE 的独立数据库回归测试，以及诊断报告。

已知未修复问题：岗位权限保存失效、任务删除权限缺失、停用或降权后的旧会话仍有效、当前用户权限不自动同步、子任务进度接口报错。尤其权限与会话问题存在安全风险，详见 [权限测试报告](docs/PERMISSIONS_REVIEW.md)。这些问题不属于本次已修复内容。

更新时同步 EXE 同目录的 `static/index.html`，浏览器按 `Ctrl+F5` 强制刷新，无需重新打包 EXE。操作说明与验证见 [工作台修复记录](docs/WORKBENCH_NAVIGATION_FIX.md)。

每位使用者在自己的电脑启动本机 HTTP 服务，通过浏览器访问；各本机服务读取同一个共享目录中的 SQLite 数据库，实现多人协作。

## 项目组成

| 文件 | 用途 |
| --- | --- |
| `TeamToDo V1.1/server.py` | Python 标准库 HTTP 服务、认证会话、岗位权限、任务和反馈、组织人员、台账及软件更新 |
| `TeamToDo V1.1/static/index.html` | 原生 HTML/CSS/JavaScript 管理页面，含任务卡片、列表、看板、筛选和大屏 |
| `TeamToDo V1.1/static/login.html` | 登录与注册页面 |
| `TeamToDo V1.1/db/schema.sql` | 新数据库建表定义 |
| `TeamToDo V1.1/excelutil.py` | 标准库实现的 Excel 导入导出 |
| `TeamToDo V1.1/TeamToDo.exe` | 已有 Windows 打包程序 |
| `TeamToDo V1.1/linux/` | Linux 安装及启停脚本 |
| `tests/` | 独立数据库上的实时刷新回归验证 |

## 运行和配置

Windows 使用应用目录内的 `启动服务.bat`，默认优先启动 EXE；源码运行可使用 `python server.py`。本机默认地址为 `http://127.0.0.1:8765`。

实际部署配置位于应用目录的 `config.json`，数据库由 `database.folder`、`database.file` 或 `database.path` 指定。共享数据库属于运行数据，不进入 Git；实际配置、日志、端口记录也已排除。配置模板是 `config.json.example`。

重要：`server.py` 正常启动会执行数据库迁移和初始化。不要为了测试对生产数据库启动源码服务；回归脚本使用独立临时库，也不加载实际配置。

## 实时刷新修复

原页面只有首次打开、切换栏目或本人操作后才读取数据，其他客户端的修改不会触发刷新。新增同步循环在当前轮完成后等待 3 秒，再读取当前栏目；正常响应时通常数秒内看到变更，网络或共享盘延迟会延长这一时间。

切回可见页面、窗口重新获得焦点和网络恢复时立即同步；隐藏页面、编辑弹窗、筛选面板或任务拖拽期间暂停，避免覆盖输入。任务列表保持筛选并忽略过期请求，数据未变化时不重绘；已打开的详情同步任务与反馈，保留未发送文字。子任务汇总沿用打开详情时的行为。

本次应用修改仅在 `static/index.html`，后端、数据库结构、实际配置及原 EXE 保持不变。后端代码优先读取 EXE 同目录的 `static/`；部署时把更新后的 `static/index.html` 放到每个客户端的同一位置，然后手动刷新一次已打开的浏览器页面即可加载修复。

## 验证

运行 `node tests/live_refresh.cjs`。需要 Node、Python 和 Playwright。此电脑默认使用 Codex 自带的 Python/Playwright 以及已安装 Chrome；其他环境可用 `PYTHON_PATH`、`PLAYWRIGHT_PATH`、`CHROME_PATH` 指定对应路径。

测试启动真实 HTTP 处理器，创建独立临时数据库，以两个登录会话复现与验证跨会话同步。脚本同时核对 `数据存放/tasks.db` 的 SHA-256，生产库不参与任何测试请求。

验证范围和已知问题见 [修复验证记录](docs/LIVE_REFRESH_REVIEW.md)。

## 老版本留存

修复前版本已保存在 Git 提交 `065df3b`，标签 `baseline-before-live-refresh`，包括原源码、页面、脚本和 EXE；不包含业务数据库、真实配置或日志。2026-10-09 已上传到 [GitHub 老版本备份](https://github.com/maodougansidui/TeamToDo/tree/baseline-before-live-refresh)。后续上传 V1.2 不移动该备份标签。
