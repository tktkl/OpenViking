# 主动跟进的上游合并接入点

分支 `codex/vikingbot-helpdesk-followup` 基于 `fc7e0cbb2121288c8695a5be7a353e23b0e4ef4c`，已含 `codex/vikingbot-session-concurrency` 的固定 worker、公平会话队列及会话控制修复。原 `bot-main` 的 `fe3dc33a5cdfdc3309af61912f02077668bc03e5` 和未提交的 agent_bridge、消息总线、OpenAPI 补丁未移动、未叠加到本分支。

## 本次接入面

| 位置 | 必须保留的契约 |
| --- | --- |
| `extensions/feishu_helpdesk/state.py` | SQLite 单实例所有权、消息别名去重、人工轮次、outbox、独立游标 |
| `extensions/feishu_helpdesk/sync.py` | 创建/更新窗口完整分页，消息游标独立，时间单位和可信群绑定，未知来源重试 |
| `extensions/feishu_helpdesk/decision.py` | 独立预算、无工具、可信角色与最新消息依据，不让模型编造关单期限 |
| `extensions/feishu_helpdesk/followup.py` | 仅通过主会话队列执行；版本检查、成功发送计时、跨重启幂等及关闭等待 |
| `extensions/feishu_helpdesk/delivery.py` | 独立后台只读对账，暂停后继续运行；人工消息立即交付，关闭后仍过滤自动回流；回执结算可重入 |
| `extensions/feishu_helpdesk/channel.py` | 复用飞书长连接；在原生媒体解析/steer 等待前观察人工活动；补漏恢复入群不阻塞扫描 |
| `agent/loop.py` | 只增加物理群忙碌查询，涵盖话题、总线/调度器排队、执行及最终保存；ContextVar 排除回调自身 |
| `agent/session_dispatcher.py` | 只增加只读 `queued_session_keys()`，包括内部回调，不改变 worker/公平调度 |
| `channels/manager.py`、`cli/commands.py` | 仅给启用的服务台子类注入当前 provider、model 及 `has_chat_work` |
| `extensions/feishu_helpdesk/config.py` | `followup_interval_minutes` 严格正整数，默认 5；`messages` 统一标识和文案，时间模板校验 |
| `extensions/feishu_helpdesk/api.py`、`cli.py` | 应用授权只读；用户操作仍显式 CLI `--as user`；请求头 stdin、进程取消和脱敏 |

本次没有修改 `bus/events.py`、`bus/queue.py`、原生 `channels/feishu.py` 或 `channels/openapi.py`。内部事件继续使用并发分支已有的 `before_process` / `internal_handler`。不增加发送 API 并发，不改变共享文件、工具、MCP 或沙箱权限。

上游若替换调度入口，必须让回调在现有 worker 内被完整 await，包括 CLI、持久化和清理。不能将回调 fire-and-forget，否则会绕过 `max_concurrent_sessions`。若替换会话队列，忙碌查询必须覆盖新的排队结构和保存阶段；它只排除当前执行 owner，不能排除同物理群其他话题。

## CLI 补丁独立维护

`bot/compat/lark-cli/0001-api-request-headers.patch` 固定在 CLI 提交 `4e2cbea94e33913378ce515f0fccfb9b1f2f2bb9`，只修改原生 `api` 命令及新增测试。`build.sh` 在临时克隆中构建私有副本，使用 `helpdesk.cli.executable` 指向它，不修改全局 CLI 或原 CLI 源仓库。上游支持等价参数后，可通过契约测试再移除补丁。用户 Token 的存储和刷新不得搬到 VikingBot。

## 持久化与回退

发布和回退时保留 `{storage_workspace}/bot/helpdesk/` 及 CLI 的授权存储，不删除数据库来重发欢迎。启用时间、轮次及回执是防止历史工单误接管和重复发送的依据。自动跟进需 macOS/Linux 的文件锁；部署时沿用现有扩展的主机环境。

仅限制执行并发可设 `max_concurrent_sessions=1` 并重启；它仍会自动跟进。暂停整个服务台扩展用 `helpdesk.enabled=false` 并重启，原生 IM 继续运行。代码回退到此前版本同样应保留状态文件。

## 验证记录（2026-11-03）

测试覆盖事件与 IM 合并、乱序、待绑定与权限恢复、独立游标、分页中断及一万条拆窗、5／10／15 分钟时间线、失败和不确定回执、同用户手动/自动消息区分、直接解决与超时关单、旧判断作废、重启欢迎及关闭说明恢复，以及真实 AgentLoop 共享执行额度和话题最终保存保护。

- Bot 完整回归：**822 passed、4 skipped、1 项既有失败**，包含服务台、并发、后台任务、来源、上下文和 OpenAPI；最后增加的关单锁顺序回归也通过。
- Python 3.10 服务台及并发兼容回归：**241 passed**；基础 Token 预算工具：**4 passed**。新增及修改文件的 Ruff 检查、补丁空白检查通过。
- CLI 请求头与用户身份测试、相关包 race 测试、`go vet ./...`、私有二进制构建和构建脚本验证通过。
- Bot 完整套件存在既有 `test_configured_timeout_reaches_chat_request[default]` 失败：默认超时 600 与断言 60 不一致；并发基线也存在该失败。
- CLI 完整 `make unit-test` 在动态元数据 bot-only 判定、semantic-review 策略/waiver 及 minutes 下载地址校验失败；在同固定提交的无补丁克隆中复现。新增请求头测试通过。

自动化验证未向真实飞书工单发消息、关单或修改真实授权。部署前还需在测试服务台核验真实事件投递、当前 CLI 用户权限和所需 OAuth scopes。

## 审查修复

人工消息不再等待 CLI 自动发送回执。后台只读对账与业务调度分开，暂停后仍可恢复回执，关闭说明在工单关闭后仍经过自动消息过滤。欢迎、跟进和关闭说明使用统一可配置格式，既有 outbox 继续使用原文案对账。正常再次提问按新工单接管；同 ID 重新打开作为兼容分支，使用持久化核对标记和独立关闭轮次，避免漏恢复、重复欢迎或按旧对话立即关单。

本轮新增 19 个回归用例，覆盖暂停且后台查询阻塞时人工消息交付、旧轮回执只读恢复、重启及配置变化、关闭说明早到/晚到回流、分页与来源隔离、CLI 锁等待时回执竞争，以及新工单和异常重新打开的兼容。完整 Bot 回归 **841 passed、4 skipped、1 项上述既有超时失败**；Python 3.10 服务台、并发和来源回归 **270 passed**。Ruff 与差异空白检查通过，未调用真实用户发送或关单接口。
