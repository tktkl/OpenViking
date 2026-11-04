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
| `agent/loop.py` | 物理群忙碌查询涵盖话题、总线/调度器排队、执行及最终保存，以及 `process_direct()` 的初始化、处理和清理；ContextVar 只排除调度回调自身 |
| `agent/session_dispatcher.py` | 只增加只读 `queued_session_keys()`，包括内部回调，不改变 worker/公平调度 |
| `channels/manager.py`、`cli/commands.py` | 仅给启用的服务台子类注入当前 provider、model 及 `has_chat_work` |
| `extensions/feishu_helpdesk/config.py` | `followup_interval_minutes` 严格正整数，当前默认 3；`reply` 选择工单群发送身份；`escalation` 配置系统映射、30 分钟间隔和 Bot/CLI 发送；`messages` 统一标识和文案，时间模板校验 |
| `extensions/feishu_helpdesk/api.py`、`cli.py` | 应用授权只读；普通用户操作显式 CLI `--as user`；关单启动当前 Python，准备后再做最终回调；共享锁、总预算、stdin、进程组取消和脱敏 |
| `extensions/feishu_helpdesk/close_helper.py` | 固定关单 API，版本 1 的 ready/commit/abort 协议；CLI 策略与身份检查、续期先于刷新锁；一次 PUT、不重放不确定提交 |
| `extensions/feishu_helpdesk/cli_credentials.py` | 集中适配 CLI 目录、工作区、profile、AES-GCM 和刷新锁；只读解密 access token，不创建密钥、不使用 refresh token |

本次没有修改 `bus/events.py`、`bus/queue.py`、原生 `channels/feishu.py` 或 `channels/openapi.py`。内部事件继续使用并发分支已有的 `before_process` / `internal_handler`。不增加发送 API 并发，不改变共享文件、工具、MCP 或沙箱权限。

上游若替换调度入口，必须让回调在现有 worker 内被完整 await，包括 CLI、持久化和清理。不能将回调 fire-and-forget，否则会绕过 `max_concurrent_sessions`。若替换会话队列，忙碌查询必须覆盖新的排队结构和保存阶段；它只排除当前执行 owner，不能排除同物理群其他话题。

## Python 关单与 CLI 兼容边界

关单现已改为独立 Python 子进程，删除旧 CLI 补丁和构建脚本，`helpdesk.cli.executable` 使用官方 `lark-cli`。无需 Go、私有 CLI 或独立工具路径。适配契约以 CLI 提交 `4e2cbea94e33913378ce515f0fccfb9b1f2f2bb9` 为参考，按字段和加密布局判断兼容性；未来格式变化仅需修改凭据适配模块。原有 SQLite 无须迁移。

上游合并须保留准备和提交之间的最终检查：`ready` 之后由 Bot 读取最新状态和消息，在当前轮次、关闭轮次、同群忙碌检查通过后持久化待确认操作，再立即 `commit`。刷新锁内不能调用 CLI；最新消息检查使用应用接口并跳过 CLI 来源核实。超时从共享 CLI 锁等待开始计算，取消回收脚本整个进程组。CLI 继续独占 OAuth 刷新和凭据写入；仅 Python 子进程只读解密 access token，不导出、不另存、不自建续期链路。

`commit` 后结果不确定时保留 `close_pending`；远端仍开放也不重放旧 PUT，通过状态及备注确认后才补发关闭说明。凭据不兼容只影响关单，不暂停人工消息和回执对账。平台、目录和升级说明见 [CLI 凭据兼容](../../../compat/lark-cli/README.md)。

## 持久化与回退

发布和回退时保留 `{storage_workspace}/bot/helpdesk/` 及 CLI 的授权存储，不删除数据库来重发欢迎。启用时间、轮次及回执是防止历史工单误接管和重复发送的依据。自动跟进需 macOS/Linux 的文件锁；部署时沿用现有扩展的主机环境。

仅限制执行并发可设 `max_concurrent_sessions=1` 并重启；它仍会自动跟进。暂停整个服务台扩展用 `helpdesk.enabled=false` 并重启，原生 IM 继续运行。代码回退到此前版本同样应保留状态文件。

## 验证记录（2026-11-03）

测试覆盖事件与 IM 合并、乱序、待绑定与权限恢复、独立游标、分页中断及一万条拆窗、当时默认的 5／10／15 分钟时间线（现行为已改为 3／6／9 分钟并进入升级）、失败和不确定回执、同用户手动/自动消息区分、直接解决与超时关单、旧判断作废、重启欢迎及关闭说明恢复，以及真实 AgentLoop 共享执行额度和话题最终保存保护。

- Bot 完整回归：**822 passed、4 skipped、1 项既有失败**，包含服务台、并发、后台任务、来源、上下文和 OpenAPI；最后增加的关单锁顺序回归也通过。
- Python 3.10 服务台及并发兼容回归：**241 passed**；基础 Token 预算工具：**4 passed**。新增及修改文件的 Ruff 检查、补丁空白检查通过。
- CLI 请求头与用户身份测试、相关包 race 测试、`go vet ./...`、私有二进制构建和构建脚本验证通过。
- Bot 完整套件存在既有 `test_configured_timeout_reaches_chat_request[default]` 失败：默认超时 600 与断言 60 不一致；并发基线也存在该失败。
- CLI 完整 `make unit-test` 在动态元数据 bot-only 判定、semantic-review 策略/waiver 及 minutes 下载地址校验失败；在同固定提交的无补丁克隆中复现。新增请求头测试通过。

自动化验证未向真实飞书工单发消息、关单或修改真实授权。部署前还需在测试服务台核验真实事件投递、当前 CLI 用户权限和所需 OAuth scopes。

## 审查修复

人工消息不再等待 CLI 自动发送回执。后台只读对账与业务调度分开，暂停后仍可恢复回执，关闭说明在工单关闭后仍经过自动消息过滤。欢迎、跟进和关闭说明使用统一可配置格式，既有 outbox 继续使用原文案对账。正常再次提问按新工单接管；同 ID 重新打开作为兼容分支，使用持久化核对标记和独立关闭轮次，避免漏恢复、重复欢迎或按旧对话立即关单。

本轮新增 19 个回归用例，覆盖暂停且后台查询阻塞时人工消息交付、旧轮回执只读恢复、重启及配置变化、关闭说明早到/晚到回流、分页与来源隔离、CLI 锁等待时回执竞争，以及新工单和异常重新打开的兼容。完整 Bot 回归 **841 passed、4 skipped、1 项上述既有超时失败**；Python 3.10 服务台、并发和来源回归 **270 passed**。Ruff 与差异空白检查通过，未调用真实用户发送或关单接口。

## 追加审查修复

修复以下四项问题：

- `process_direct()` 使用独立调用标记登记忙碌状态，覆盖 MCP 初始化、消息处理、最终保存和清理。同会话重叠调用分别回收，取消或异常也会清理；同物理群的直接调用未完成时推迟关单。
- 在等待 CLI 锁及授权后重新检查 UUID 有效期，超期请求只进入后台只读对账。锁等待期间已确认回执的请求优先采用回执，不暂停或重发原消息。
- 自动发送改用 CLI 原生消息 API，保留飞书原始毫秒时间。同 UUID 重试使用原消息时间；无有效时间的回执不推进次数或期限，等待后台对账。
- 部署文档将用户发送权限修正为 `im:message.send_as_user`，并补充 `im:message`。

本轮新增 **15 个回归用例**，服务台发送、回执及并发定向测试 **117 passed**。完整 Bot 回归 **856 passed、4 skipped、1 项上述既有超时失败**；Python 3.10 服务台、并发和来源回归 **285 passed**。额外的图片、模型重试、中断和 Token 预算回归 **44 passed、1 项既有失败**：仓库根目录的 `test_dequeued_message_is_active_before_interrupt_runs` 手工构造旧版 AgentLoop，缺少 `runtime_extensions`，已在修复前的 HEAD 源码快照复现。该旧测试夹具不属于本轮四项修复。

修改文件的 Ruff、格式及差异空白检查通过；CLI `cmd/api`、`internal/client` 的 race 测试通过。未发送真实飞书消息、关单或修改授权，原 `bot-main` 工作区保持原状。

## 发送恢复与跨工单回流修复

本轮补齐三处边界：

- 首次发送明确被拒绝时清除待对账状态，修复权限后可恢复，人工消息及重启不会将其永久暂停。未知失败仍保留回执；此前有不确定发送时，后续一次拒绝不会清除原记录。
- 在锁和授权等待结束后，将剩余幂等时间作为 CLI 请求预算，覆盖进程启动、内部凭据刷新和 HTTP 调用。即使配置 `timeout_seconds=180`，接近过期时也会提前取消并回收子进程，后续仅做只读对账。
- outbox 持久化目标群，自动消息匹配覆盖同一可信聊天内的旧工单，兼容无目标群字段的旧记录。不同群不能互相匹配；多个候选发送都符合时过滤自动回流，但不任意结算一个工单的回执。

新增 **19 个回归用例**，覆盖明确拒绝分类、权限修复及重启、新人工轮次、先不确定后被拒绝、剩余预算取消真实本地子进程、新旧发送记录和同群/跨群隔离，以及多个候选回执。完整 Bot 回归 **875 passed、4 skipped、1 项上述既有超时失败**；Python 3.10 服务台、并发和来源回归 **304 passed**。Ruff、格式及差异空白检查通过，未调用真实飞书发送、关单或授权接口。

## 迟到群关联与关闭恢复修复

- 群关联独立于工单状态版本保存。扫描先取得较新状态后，迟到的可信事件仍可补齐群 ID，并在重启后继续欢迎和跟进；旧状态和角色不覆盖新记录，群冲突仍拒绝。
- 正常关单确认与不确定关单恢复共用状态核对，检查查询期间的版本、关闭轮次和待办归属，再合并详情。已重新打开的工单不接收旧关闭说明，迟到关闭结果不能覆盖新状态。发送关闭说明前仍要求当前处于关闭状态且轮次匹配，完成后只清理对应轮次的待办。

新增 **9 个回归用例**，覆盖创建及更新事件迟到、群绑定持久化、开放和关闭状态下的冲突关联、恢复查询读到重新打开、查询期间并发重新打开、正常确认中的旧响应，以及并发扫描或人工消息到达后只补发一次说明。完整 Bot 回归 **884 passed、4 skipped、1 项上述既有超时失败**；Python 3.10 服务台、并发和来源回归 **313 passed**。Ruff、格式及差异空白检查通过，未调用真实飞书发送、关单或授权接口。

## 重新打开后的历史补漏边界

整体复核补充发现：关闭事件先于消息同步到达时，关闭前的人工消息可能在重新打开后才补齐，旧解决结论因此错误触发再次关单。现在以最后已知关闭状态的时间和已知人工消息时间共同约束新的人工沟通，关闭时刻及之前的补漏消息不解除等待；同毫秒记录也按旧消息处理。

新增 **4 个回归用例**，覆盖普通运行及重启恢复、旧消息与关闭同毫秒及早于关闭的情况，并验证真正的新人工消息仍能恢复判断。本轮修复共新增 **13 个用例**。完整 Bot 回归 **888 passed、4 skipped、1 项上述既有超时失败**；Python 3.10 服务台、并发和来源回归 **317 passed**。Ruff、格式及差异空白检查通过，未调用真实飞书发送、关单或授权接口。

## Python 独立关单（2026-11-03）

以随包安装的 Python 子进程替代补丁 CLI，移除旧补丁和构建脚本。官方 CLI 继续负责消息、查询和 OAuth 续期；凭据适配模块只读支持 Linux/macOS 的现有加密布局、profile/工作区和刷新锁，不创建密钥、不自建续期链路。关单采用准备、最终检查、持久化待确认操作、提交的两阶段协议；一次 PUT，提交后结果不确定只查询确认，不能重放旧请求。准备失败和明确拒绝可安全恢复，凭据不兼容只影响关单。

新增 **97 个回归用例**，涵盖真实 AES-GCM 布局、macOS Keychain 失败、身份与品牌不匹配、未知 provider/格式、锁等待及释放、CLI 与原生 API 信封、两类关闭参数、最终检查与自然时间回拨、拒绝/不确定结果、重启恢复以及真实子进程管道和取消清理。正常退出先确认主进程状态，再清理进程组，避免遗留后代持有输出管道时等待 EOF。原 `bot-main` 的 HEAD、暂存区、未暂存补丁及未跟踪文件哈希均保持不变。

- Python 3.12 完整 Bot 回归：**985 passed、4 skipped、1 项既有超时失败**。唯一失败仍是默认 600 秒与断言 60 秒不一致，本次未修改相关代码。
- Python 3.10 凭据、关单协议、CLI、跟进及共享并发定向测试：**218 passed**；进程清理修复后另行复核协议测试 **12 passed**。Python 3.10 全量尝试在既有后台任务 `slow_probe` 用例等待退出，完整回归改用 Python 3.12，该用例通过。
- 基础 Token 预算工具：**4 passed**；修改 Python 文件的 Ruff、格式及差异空白检查通过。已通过实际模块入口和模拟 CLI/凭据验证准备与取消链路。

全部验证离线进行，没有读取真实 CLI 用户凭据、发送飞书消息、关单或修改真实授权。尚需在测试服务台验证真实用户权限、Keychain 访问和远端关单。部署只需更新 VikingBot、保留官方 CLI 登录态、确认 `cli.executable` 指向官方 CLI 后重启，SQLite 无须迁移。

## 2026-11-04 未提交批次：Web 工单回复、升级与发送恢复

### 基线与范围

本轮以本地 `codex/vikingbot-helpdesk-followup` 的 `1f180bc1a` 为提交基线，整理此前分散在工作区中的服务台、关单兼容和 VLM 修复。代码提交为 `bbdb93a86`（服务台发送、升级、回执和关单恢复）与 `d5fd48bbd`（VLM per-call reasoning effort），本文档随后单独提交。核对时本地缓存的上游引用为 `upstream/main@6e7729126`；本轮远程 fetch 未完成，合并前仍须重新 fetch 并以当时引用为准。

应进入本批次的文件共 34 个：

- 服务台扩展生产代码 14 个：`api.py`、`channel.py`、`cli.py`、`close_helper.py`、`config.py`、`decision.py`、`delivery.py`、`followup.py`、`state.py`、`sync.py`、`manager.py`，新增 `escalation.py`、`feed_resolver.py`、`reply.py`。
- VLM 生产代码 3 个：`providers/vlm_adapter.py`、`openviking/models/vlm/base.py`、`openviking/models/vlm/backends/openai_vlm.py`。
- 测试 11 个：服务台跟进、升级、发送、feed 解析、API、关单 helper/协议、Provider thinking 与 VLM reasoning。
- 文档与示例 6 个：本文件、上游合并影响、主动跟进说明、渠道说明、用户手册及完整 JSON 示例。

本机专属内容不进入提交：`bot/workspace/SOUL.md`、`openviking/system_assets/manifest.json`、`bot/workspace/skills/*`。这些内容依赖当前机器或业务 Skill 安装状态，不属于可复用源码。

### 文件级合并契约

| 文件 | 本批次改动 | 合并时必须保留的契约 | 冲突重点 |
| --- | --- | --- | --- |
| `extensions/feishu_helpdesk/reply.py` | 新增可插拔 `FeishuWebGatewaySender`、Web Gateway protobuf、回执解码、auth-mcp Cookie 获取和 feed-id 接入 | 工单群发送与状态机解耦；发送前调用 `before_call` 写入 actor/attempt；同一 UUID 重试；Web 回执数字 ID 与后续 `om_*` echo 可关联；Cookie 不写日志、不落盘 | 上游若新增统一 sender 接口，应适配该接口而不是把 Web 逻辑塞回 `followup.py` |
| `extensions/feishu_helpdesk/feed_resolver.py` | 新增 `oc_* → feed_id` 静态映射、持久缓存、无头 Chromium/CDP 解析、常驻页面冻结/恢复 | 先查配置和缓存；未命中才启动浏览器；页面在空闲时冻结，避免每工单启动；缓存文件只保存 chat/feed 映射 | 上游若提供 chat 到 feed 的官方接口，优先替换浏览器解析，保留缓存与错误边界 |
| `extensions/feishu_helpdesk/escalation.py` | 从工单首条人工选择识别系统，技能组字段兜底，渲染客户名、工单号、流程链接和 30 分钟说明 | 只从配置的 `groups` 生成系统集合；忽略 Bot 的分类选项列表；配置加载器把 `PDM` 转成 `P_D_M` 时仍须归一化为 `PDM` | 不要硬编码系统到群映射；链接使用 `https://applink.feishu.cn/client/chat/open?openChatId=oc_*` |
| `extensions/feishu_helpdesk/config.py` | 增加 `reply`、`escalation` 配置；工单群间隔默认 3 分钟；升级默认 30 分钟；升级 sender 默认 `bot`；增加升级提醒后缀 | `followup_interval_minutes`、升级间隔均为严格正整数；任意系统键可配置；`feishu_web` 必须显式提供 `actor_open_id` 和 `auth_mcp_server` | 上游若调整 Pydantic 配置结构，保留字段默认值和模型校验，不要丢失大小写系统键归一化 |
| `channels/manager.py` | 仅在启用 Helpdesk 时构建并注入 reply sender | sender 是可插拔依赖，不应该让普通 Feishu 渠道初始化 Web、浏览器或 auth-mcp | 上游若重构渠道工厂，需把注入点迁移到同等生命周期位置 |
| `extensions/feishu_helpdesk/channel.py` | 接收 reply sender；绑定 Helpdesk 和 feed 缓存；后台 warm；停止时关闭；持久化 `actual_agent_group`/`hit_agent_groups`；注册 reaction no-op | `bind_helpdesk()` 先于发送；warm 任务不阻塞原生 IM；停止顺序为取消任务、关闭 sender、关闭 API；reaction 事件不再产生 processor-not-found | 上游若改事件 builder、start/stop 顺序或 ticket 对象字段名，逐项迁移 |
| `extensions/feishu_helpdesk/api.py` | 增加群详情和 `send_bot_text()` | 升级群必须使用 tenant token 官方 IM API；接收 `before_call` 并返回 `message_id/create_time`；不得复用 Helpdesk Token 作为发送 token | 上游若提供统一 Bot sender，可替换实现，但保留幂等 UUID 和 tester 身份 |
| `extensions/feishu_helpdesk/followup.py` | 升级状态机、独立 outbox、工单/升级发送路由、30 分钟重复、升级后停止工单提醒、解析失败延迟、关单通知一次策略、已升级工单关单失败重试 | 升级状态不因新人工消息重置；工单群与升级群使用独立期限和 outbox；`scenario=resolved` 优先关单，不被升级提前返回截断；升级群消息不写回工单 | 这是最高冲突文件；不要整文件选择 ours/theirs，应按“pending/欢迎/升级/判断/提醒/关单”分支合并 |
| `extensions/feishu_helpdesk/delivery.py` | 后台只读回执对账；transport 精确识别；关闭工单死信；升级群消息不写回工单 | `feishu_web` 和 `feishu_bot` 均可按精确文案、目标和尝试窗口确认；升级群讨论不进入 ticket messages；关闭后停止重试 | 上游若改 outbox 或 observe 接口，保持 transport 与目标群过滤 |
| `extensions/feishu_helpdesk/state.py` | 增加升级字段；持久化技能组字段；支持删除/死信 outbox；自动消息可用 `im_message_id` 识别 | 旧 SQLite 自动兼容，新增字段均可缺省；人工消息不重置升级状态；重开后重置升级轮次 | 不要新增破坏性 schema migration；旧库启动必须成功 |
| `extensions/feishu_helpdesk/sync.py` | 对 Helpdesk 数字内部消息按 guest/agent/system 分类 | 数字消息 ID 不能永久保持 `human=None`；未知来源仍然阻塞判断；系统消息不冒充人工 | 上游若改消息字段，可按新字段分类，但不能退化为“所有非 om_ 都未知” |
| `extensions/feishu_helpdesk/decision.py` | JSON 提取/重试、echo 合并、人工来源要求、转人工场景提示词 | 只解析第一个平衡 JSON；模型异常按可重试解析错误处理；判断必须引用真实人工消息；业务仅选分类时判 `awaiting_details` | 上游若更新 prompt，保留 resolved 证据、最新消息和人类/处理方身份约束 |
| `extensions/feishu_helpdesk/cli.py`、`close_helper.py` | 兼容新版 CLI identity envelope；原生关单 2xx + `code=0` 且 `data` 缺失；输出受限诊断 | 同时接受旧 `as/appId/userOpenId` 与新 `identity/data.context`；不得泄露凭据；成功关单不因 `data=null` 误判，未知响应仍保留 `close_pending` | CLI 格式变化只应影响关单诊断/确认，不应暂停人工消息和升级 |
| `providers/vlm_adapter.py`、`openviking/models/vlm/{base.py,backends/openai_vlm.py}` | 把 per-call `reasoning_effort` 传给 OpenAI/Azure/Volcengine 后端；MultiCredential 透传 | 没有显式 override 时不改变已有模型默认；非支持后端不注入该参数；视觉路径不受影响 | 上游若重构模型参数对象，保留显式 override 的逐层透传 |
| 测试与文档 | 新增升级、Web sender、feed resolver、Bot IM 发送、关单兼容和 VLM 测试；更新部署与配置示例 | 测试不得依赖真实 Cookie、真实群或付费模型；部署文档必须区分工单群 Web 身份与升级群 Bot 身份 | 文档合并后重新核对命令、默认值和示例 JSON |

### 运行时状态机

| 时间点 | 工单群 | 升级群 | 状态持久化 |
| --- | --- | --- | --- |
| 新人工消息 | 立即作废旧判断并重新判断 | 不直接发送 | 新 generation，清零本轮提醒次数；已升级标记保留 |
| 未解决且无本轮提醒 | 3 分钟后第一次提醒 | 不发送 | `count=1,last_send=成功时间` |
| 第一次成功后仍未解决 | 再 3 分钟第二次提醒，文案提示 3 分钟后升级 | 不发送 | `count=2,last_send=成功时间` |
| 第二次成功后再静默 3 分钟 | 不再发提醒 | 首条升级通知 | `escalation_started_at/escalation_last_sent/escalation_count` |
| 升级后每 30 分钟 | 只同步和判断，不发提醒 | 重复升级通知 | 更新 `escalation_last_sent` 和序号 |
| 判定 resolved | 立即关单，成功后最多发一次关闭说明 | 停止 | `status=51,solved=2` |
| 关单失败 | 5/10 秒重试或 60 秒核对 `close_pending` | 停止发送，等待解决状态确认 | 保留升级字段，resolve 不因升级分支跳过 |
| 远端已关闭 | 停止提醒和升级 | 停止 | `status` 为 50/51/52，后台 outbox 死信 |

没有配置升级路由时，仅保留旧兼容路径：`awaiting_details`/`awaiting_confirmation` 在两次提醒后再等一个 3 分钟间隔以 `solved=1` 超时关闭；`awaiting_handler` 保持开放。当前示例配置启用升级，因此不会走超时关单。

### 发送身份与安全边界

- 工单群：`reply.mode="feishu_web"`，使用 auth-mcp 返回的完整原始 Cookie；只按 `internal-api-lark-api.feishu.cn` 域名拼 Cookie Header；Cookie 仅用于请求，不写入 outbox/state/log/缓存。持久缓存只有 `oc_* → feed_id`。
- 工单群兼容回退：`reply.mode="lark_cli"`，由 `LarkCLIAdapter` 发送；如果 CLI 无法处理服务台群，会明确失败，不会自动切换 Web。
- 升级群：`escalation.sender="bot"` 默认使用 tenant token 官方 IM API；`lark_cli` 可选；两条路径都不使用 Web Messenger。Bot 必须已经在目标群，系统识别失败时每 5 分钟重试，不崩溃，也不退回工单群提醒。
- 关单：CLI 仅做登录、身份和必要续期；子进程只读解密 access token，固定一次 PUT；服务台 Token 与 CLI 用户 token 都不能用来替代 Web 工单群发送身份。

### 合并顺序与冲突处理

1. 先合入 `config.py`、`state.py`、`api.py` 和新增模块，确保配置和持久化字段可加载；不要先合入 `followup.py`。
2. 再合入 `reply.py`、`feed_resolver.py`，运行 Web sender、feed cache 和 Bot IM 单元测试。
3. 合入 `channel.py`、`channels/manager.py`，验证启停、warm 任务和普通 IM 不受影响。
4. 合入 `sync.py`、`decision.py`、`delivery.py`，保证消息来源、echo 去重和回执对账稳定。
5. 最后按分支语义合入 `followup.py`；重点检查 `_due`、`_next_wait`、`_execute`、`_send`、`_close`、`_notice` 六个函数。
6. VLM 改动可独立合并；它不依赖服务台状态，但若上游改了 `get_completion_async` 或模型参数构造，必须重新透传 `reasoning_effort`。
7. 合并后按下列顺序验证：配置加载 → 旧 SQLite 启动 → 普通 IM → 工单 welcome/3/6/9 时间线 → 升级群 Bot 30 分钟 → resolved 立即关单 → 关单失败重试 → 重启不重复。

不要合并下列本机文件；它们只用于当前部署环境：

```text
bot/workspace/SOUL.md
openviking/system_assets/manifest.json
bot/workspace/skills/*
```

### 部署与回退补充

部署前按 [服务台使用手册](vikingbot-helpdesk-user-manual.md) 的完整 JSON 示例更新配置。工单群 Web 发送与升级群 Bot 发送是独立认证：auth-mcp Web 会话失效时只有工单群发送失败，升级群 Bot 和人工长连接仍可工作；Bot 不在升级群时只有升级发送失败，不应暂停工单群判断。升级和提醒 outbox 都有独立 transport，排查时先看 `state.sqlite3` 的 `transport`、`chat_id`、`message_id` 和升级字段。

回退时保留 state SQLite、feed-id 缓存和 CLI/auth-mcp 登录态。关闭升级只改 `escalation.enabled=false`；关闭 Web 工单发送改回 `reply.mode="lark_cli"`，不要在旧 outbox 未结算时删除数据库。升级状态字段可按缺省值恢复，但不要清空历史 outbox，否则会丢失幂等和回执证据。

### 验证快照

本批次离线回归结果：服务台与 VLM 相关测试 **464 passed**（454+10），服务台套件有 9 条既有弃用/环境警告，VLM 套件有 6 条警告。另外用当前生产配置只读验证了配置解析、两个升级群 Bot 成员关系，以及真实升级消息的 `sender_type=app`；未把真实 Cookie、Token 或消息正文写入本文件。合并上游后仍需重新执行下列命令，不能直接引用本快照。

```bash
cd bot
../.venv/bin/python -m pytest -q \
  tests/test_helpdesk_followup.py \
  tests/test_helpdesk_followup_concurrency.py \
  tests/test_helpdesk_delivery.py \
  tests/test_helpdesk_web_reply.py \
  tests/test_helpdesk_feed_resolver.py \
  tests/test_helpdesk_escalation.py \
  tests/test_feishu_helpdesk.py \
  tests/test_feishu_helpdesk_api.py \
  tests/test_feishu_helpdesk_cli.py \
  tests/test_helpdesk_cli_credentials.py \
  tests/test_helpdesk_close_helper.py \
  tests/test_helpdesk_close_protocol.py \
  tests/test_bot_provider_thinking.py
cd ..
PYTHONPATH=bot:. .venv/bin/python -m pytest -c bot/pytest.ini -q tests/unit/test_vlm_reasoning_models.py
```

重点断言：升级后工单群静默、升级群 30 分钟重复、系统映射配置化、resolved 立即关单、升级后关单失败可重试、Echo 不误判人工、旧 SQLite 不需迁移、`feishu_bot` 与 `feishu_web` 回执互不串群。
