# 服务台主动跟进

VikingBot 使用三条协作链路：同一飞书长连接接收事件，启动及每 60 秒主动增量查询服务台，本地持久化期限到期后将工作送入现有会话调度器。**3 分钟为工单群提醒间隔，30 分钟为升级群重复提醒间隔，60 秒为补漏周期。**

```mermaid
flowchart TD
    E[创建、更新、消息事件] --> S[工单和消息归并]
    P[启动及每60秒查询] --> S
    S --> DB[SQLite：消息、轮次、期限、回执]
    DB --> T[本地到期调度]
    T --> Q[MessageBus内部事件]
    Q --> D[现有会话调度器]
    D --> V[最新状态、消息、轮次校验]
    V --> A[模型判断、分层发送、关单]
    A --> DB
```

## 部署配置与身份

完整的可复制示例见 [feishu-helpdesk-config.example.json](feishu-helpdesk-config.example.json)。在现有 `ov.conf` 中使用 OpenViking 的配置结构：

```json
{
  "bot": {
    "agents": {"max_concurrent_sessions": 4},
    "channels": [{
      "type": "feishu",
      "enabled": true,
      "app_id": "cli_your_app",
      "app_secret": "<应用 Secret>",
      "helpdesk": {
        "enabled": true,
        "helpdesk_id": "<服务台 ID>",
        "helpdesk_token": "<服务台 Token>",
        "followup_interval_minutes": 3,
        "messages": {
          "prefix": "【AI分身自动回复】",
          "welcome": "你好，请描述遇到的问题，并补充相关截图或其他信息。如已经提供，可忽略此消息。",
          "second_reminder_suffix": "如仍未收到你的补充或确认，{minutes}分钟后将自动关闭本工单。",
          "escalation_reminder_suffix": "如仍未收到你的补充或确认，{minutes}分钟后将升级处理。",
          "closed_resolved": "问题已解决，工单已关闭。",
          "closed_timeout": "因两次提醒后仍未收到业务补充或确认，工单已关闭。"
        },
        "reply": {
          "mode": "feishu_web",
          "auth_mcp_server": "wangoon-auth-mcp",
          "provider": "feishu_helpdesk",
          "environment": "prod",
          "profile": "default",
          "actor_open_id": "ou_actor_open_id",
          "gateway_url": "https://internal-api-lark-api.feishu.cn/im/gateway/",
          "web_url": "https://tenant.feishu.cn/next/messenger/",
          "feed_ids": {},
          "auto_resolve_feed_id": true,
          "browser_executable": "",
          "feed_resolution_timeout_seconds": 60,
          "feed_cache_path": "",
          "cookie_cache_seconds": 60
        },
        "escalation": {
          "enabled": true,
          "interval_minutes": 30,
          "sender": "bot",
          "groups": {
            "TMS": "oc_tms_escalation_group",
            "SCM": "oc_business_escalation_group",
            "SRM": "oc_business_escalation_group",
            "QMS": "oc_business_escalation_group",
            "PDM": "oc_business_escalation_group",
            "PLM": "oc_business_escalation_group"
          }
        },
        "cli": {
          "executable": "lark-cli",
          "profile": "support"
        }
      }
    }]
  }
}
```

`followup_interval_minutes` 默认 `3`，为严格正整数，不接受字符串、布尔值、小数和零。连续成功提醒次数固定两次；第二次提醒后等待同一间隔，仍未解决则进入升级流程。

`escalation.groups` 是系统到升级群的配置映射。系统名来自工单首条业务选择，缺失时使用工单技能组字段兜底。TMS、SCM、SRM、QMS、PDM、PLM 当前分别路由到配置中的 TMS 群或业务系统公共群；新增系统只需增加映射，不改代码。升级通知默认以应用 Bot 身份通过官方 IM API 发送，不依赖 Web Cookie 或 feed_id 解析；可通过 `escalation.sender` 切换为 `lark_cli`。通知包含客户名、工单号、系统及原工单群可点击链接，首次发送后每 `escalation.interval_minutes` 分钟重复，直到工单解决或关闭。升级后原工单群继续同步和判断，但不再发送提醒。`escalation.enabled=false` 或 `groups` 为空时保留旧的无升级路由行为。

`messages` 可全部省略，默认值如上。欢迎、提醒及关闭说明统一为 `prefix`、换行和正文；欢迎及两种关闭说明由配置生成，不调用模型。跟进正文仍结合对话生成。配置升级路由时第二次提醒追加 `escalation_reminder_suffix`，否则追加旧版 `second_reminder_suffix`；两者都必须且只能出现一次 `{minutes}`。统一标识和固定文案不能为空，修改后重启生效。已尝试发送的消息保留当时的完整文案和 UUID，对账不受配置改动影响。

发送身份分成两条链：

- 服务台工单群：推荐 `reply.mode="feishu_web"`。服务台工单群不接受普通 CLI 用户 token 稳定模拟人工客服，因此欢迎、提醒、关闭说明和回执 echo 使用 auth-mcp 提供的完整原始 Web 会话 Cookie，经 Web Messenger `/im/gateway/` 发送。Cookie 只在内存中用于请求，不写日志、不落盘；持久缓存只保存 `oc_*` 到内部 feed id 的映射。
- 普通 IM 升级群：默认 `escalation.sender="bot"`，使用应用 tenant token 调用官方 `POST /open-apis/im/v1/messages`，由当前应用 Bot 发送，不依赖 Web Cookie、feed_id 或浏览器。可切换为 `lark_cli`，但不会回退到 Web。

`reply.mode="lark_cli"` 仍是兼容默认值，适合普通群或支持用户 API 发送的环境。`feishu_web` 使用 `auth_mcp_server`、`provider`、`environment`、`profile` 从 wangoon-auth-mcp 获取 ready 会话，`actor_open_id` 必须是目标工单的处理人。`feed_ids` 可手工配置映射；`auto_resolve_feed_id=true` 时，缓存未命中会通过无头 Chromium、CDP 和 Web Messenger 页面解析群名得到内部 feed id，并写入派生缓存。浏览器页面在后台常驻并在空闲时冻结，避免每个新工单重复启动约 20–30 秒。部署需要 Chromium/Playwright 可执行文件、`websocket-client`、可读取的 auth-mcp 配置和租户 Web Messenger 地址。

官方 `lark-cli` 继续负责工单消息读取、发送回执对账、Bot 入群、授权状态检查、保活以及关单前检查。关单由随包安装的独立 Python 子进程执行：先由 CLI 完成身份、命令策略检查和必要续期，再只读解密该用户的本地凭据，在内存中使用 access token 调用服务台。脚本不导出或另存凭据，不自行使用 refresh token。服务台读取和订阅继续使用应用授权加服务台 Token。

除了既有机器人和成员查询权限，应用需开通 `helpdesk:all:readonly` 读取/订阅权限。当前 CLI 用户需要有目标工单访问权限，以及 `helpdesk:all`、`im:message.send_as_user`、`im:message`、`im:chat.members:write_only`、所需的消息读取权限和 `offline_access`。用户读取群消息还受 `im:message.group_msg:get_as_user` 和群成员关系约束。应用权限与 OAuth 授权都必须生效。例如在运行 Gateway 的系统账号下：

```bash
lark-cli --profile support auth login \
  --scope "offline_access helpdesk:all im:message.send_as_user im:message im:chat.members:write_only im:message:readonly im:message.group_msg:get_as_user"
```

已授权且权限齐全的部署只需更新 VikingBot，保留 CLI 登录态及上面的配置，然后重启 `vikingbot gateway` 或 `openviking-server --with-bot`。从旧补丁版迁移时，将 `cli.executable` 改回官方 `lark-cli`；SQLite 无须迁移。

容器/服务器需保留 CLI 配置、安全凭据存储及必要解密材料，使用与 Gateway 相同的系统账号和目录设置；只挂载 `config.json` 不够。Linux 读取 `LARKSUITE_CLI_DATA_DIR` 或 `~/.local/share` 下的 `lark-cli` 加密文件和既有 `master.key`。macOS 读取 `~/Library/Application Support/lark-cli`，优先使用既有 `master.key.file`，否则只读访问系统 Keychain；访问失败仅暂缓关单，不创建密钥、不自动降级。配置目录沿用 `LARKSUITE_CLI_CONFIG_DIR`、CLI profile 和工作区，不扫描其他账号或工作区。初版仅支持本地 profile；环境 Token、自定义凭据 provider、未知格式明确拒绝关单。兼容性按必要字段和加密布局检查，不按 CLI 版本号一律拦截，维护点见 [凭据兼容说明](../../../compat/lark-cli/README.md)。

保持原有 `allow_from`、命令授权、`@Bot`、媒体和沙箱设置；监测人工活动不要求 `@Bot`。关单凭据读取失败不停止人工消息、欢迎、提醒或后台回执对账。

## 发现、补漏与恢复

- 长连接订阅工单创建、更新及 `helpdesk.ticket_message.created_v1`。Agent 回答仍只由标准 IM 触发，工单消息事件仅监测。服务台消息 ID 和 IM ID 合并去重。
- 首次启用时间持久化，只接管其后新建且未关闭的工单；停机期间此后的新工单会补接管。首次启用前的历史工单保持原有 IM 使用方式。
- 分别按创建时间和更新时间分页查工单，窗口重叠 60 秒；完整分页成功才推进相应游标。超过一万条时拆分时间窗口，无法完整枚举则保留原游标。工单查询时间单位为毫秒。
- 每个已接管的未关闭工单独立轮询消息，不依赖工单 `updated_at`。消息查询时间为秒，返回记录时间为毫秒。计时用消息产生时间；页数/总数异常和失败不推进消息游标。
- 群关联取可信事件，或通过工单中有效 `om_` 消息 ID 查询 IM 详情。群关联独立于工单状态版本：迟到事件仍可补齐尚未绑定的群，但不能覆盖较新的状态或角色，冲突关联拒绝合并。缺少权限时保留待绑定，不猜群名、不猜目标。恢复绑定后沿用现有 Bot 入群流程。
- 重启先同步状态和漏掉的消息，再放行到期动作；不依据不完整或来源未核实的对话自动关单。停机期间错过多轮期限，只执行当前一次提醒，下一次从本次成功时间计算。
- 正常流程中，关闭后再次提问创建新工单，按新 ID 独立接管和欢迎。兼容外部系统将同一 ID 重新设为开放：增量列表先持久化待核对标记，再读取最新详情确认；详情失败或重启不会丢失标记。确认后清零本轮次数、保留欢迎记录，等待新的人工沟通后再判断，避免旧的解决结论立即关单。补漏消息只有发生在最后已知关闭状态之后，才可解除这项等待；关闭时刻及之前的消息保留在历史中，不冒充新的沟通。迟到的创建事件不能直接重新打开工单。

## 欢迎、计时和关单

新工单欢迎成功一次，不调用模型，默认发送：

> 【服务台自动消息】
>
> 你好，请描述遇到的问题，并补充相关截图或报错信息。

欢迎语不包含时间。尚无人工沟通时从欢迎成功发送时间计时；有人工消息后按最后人工沟通时间。新人工消息到接收层立即清零本轮提醒、更新版本并作废旧期限，不等待 Agent 消费。所有话题的文本、图片、文件均计入人工活动。自动欢迎、跟进、关闭说明及系统消息不重置；同一 CLI 用户的手动沟通仍计入。

| 场景 | 跟进内容 | 两次成功跟进后且配置升级路由 |
| --- | --- | --- |
| 等待处理方响应、调查或解决 | 请产品／研发／测试响应并推进实际问题 | 再静默一个间隔后升级 |
| 等待业务描述或补充 | 请业务描述问题或补充信息 | 再静默一个间隔后升级 |
| 等待业务验证或确认 | 请业务验证或确认结果 | 再静默一个间隔后升级 |

默认时间线：静默满 3 分钟第一次提醒，成功后再等 3 分钟第二次提醒，第二次成功后再等 3 分钟仍未解决则升级。升级群首次通知成功后，每 30 分钟重复一次，直到工单判断为已解决或远端关闭。升级后原工单群继续同步消息和判断，但不再发送提醒。发送失败不增加提醒次数，也不推进下一期限。

人工沟通触发及时判断。业务确认，或模型引用可信人工消息、包含最新消息且有充分事实证明已解决时，立即关单，`solved=2`，不等待提醒或升级间隔。关单成功后发送一次关闭说明；发送失败只把该通知标记为死信，不回滚关闭状态、不重新打开工单。

仅当 `escalation.enabled=false` 或 `escalation.groups` 为空时保留旧兼容行为：`awaiting_details` / `awaiting_confirmation` 在两次提醒后再静默一个间隔，以 `solved=1` 超时关单；`awaiting_handler` 仍保持开放。当前推荐配置启用升级路由，因此不会因为业务静默自动关单。

判断使用独立无工具输入，受 `session_context_token_budget` 限制，不加载 Agent 的完整历史和工具结果。身份不明、仅附件无法判断、信息不足或模型错误时暂缓关单。文案结合对话，不虚构进展、负责人或完成时间。关单修改服务台工单状态，不关闭聊天页面；远端确认关闭后再发说明。

## 并发、过期保护和退出

欢迎、判断、提醒及关单通过 `internal_handler` / `before_process` 进入现有会话队列，共用 `max_concurrent_sessions`。值为 `1` 时全局串行，示例值 `4` 时跨会话并发；同完整 SessionKey 串行。同工单重复待办合并，内部事件不进入用户中断计数、不 steer 到活动回答。扫描与计时任务只生产状态和待办，不增加模型执行池。

出队、模型返回、发送和关单前检查版本。人工消息改变版本后，旧判断作废。关单使用两阶段协议：Python 子进程确认 CLI profile、应用、用户、品牌和工作区，对固定关单命令执行 `--dry-run`，以只读用户请求完成续期，然后取得 CLI 兼容的跨进程刷新锁并读取凭据，返回 `ready`。持有刷新锁时不调用 CLI。Bot 再通过应用接口核对最新状态、消息、轮次和同群活动，持久化 `close_pending` 后立即发送 `commit`；检查失败则 `abort`。同群任意话题有执行、排队或最终保存任务时推迟，cron、heartbeat 等 `process_direct()` 调用也计入忙碌状态，直至保存和清理完成。

Python 最多发送一次 `PUT /open-apis/helpdesk/v1/tickets/{ticket_id}`，同时带用户 Authorization 和服务台专用请求头；固定 `status=51`、对应的 `solved` 及备注。仅访问渠道品牌对应的官方域名，不跟随重定向。`cli.timeout_seconds` 覆盖共享锁等待、脚本启动、CLI 准备、最终检查及 HTTP 请求；取消时回收整个脚本和 CLI 子进程组。服务台没有条件更新版本参数，最终读取与远端 PUT 之间仍有网络边界；版本检查覆盖已经收到的消息。

本地到期任务按最近期限等待，最多 10 秒校准一次自然时间。执行额度不足时允许排队，到执行时重新验证。另有独立的后台异步任务只查询发送回执、恢复本地记录，不调用模型、不重发旧轮提醒、不关单，不受工单自动化暂停影响。它与普通人工消息交付分开运行，仍与其他 CLI 操作共用锁。停止渠道先停接收和入队、使旧待办失效，取消并等待监测、回执查询及执行回调，再关闭数据库、CLI 和 HTTP。保持单个 AgentLoop、一套 MCP 和扩展实例，普通 Bot 发送 API 并发及外部文件规则不变。

## 持久化和不确定回执

状态位于 `{storage_workspace}/bot/helpdesk/<应用与服务台的SHA256>/state.sqlite3`，SQLite WAL 和单实例文件锁保护，目录及数据库限制本机权限。记录启用时间、独立查询游标、绑定、消息别名、轮次、欢迎时间、人工活动、次数、期限依据及 outbox，不保存 OAuth 或服务台凭据。

发送前持久化 UUID、目标群、transport、完整文案和发送 actor。工单群的 `feishu_web` 回执可能是内部数字消息 ID，Helpdesk 稍后还会返回 `om_*` echo；两者通过精确文案、目标群、actor 和尝试时间窗口关联，不能仅凭发送人相同就视为自动消息。升级群的 `feishu_bot` 使用应用身份，回执对账同样不把升级群讨论写回工单消息。同轮不确定回执在幂等窗口内沿用原 UUID；超出 3500 秒时工单发送暂停等待对账，升级发送标记死信并在下一周期使用新 UUID，互不阻塞。全部不确定发送确认后自动解除回执原因导致的暂停；旧轮回执不增加新轮次数。查询失败或未确认时保持自动化暂停，人工消息仍正常处理；必要时人工核对远端消息和本地 outbox，不能删除数据库冒充首次启用。

首次尝试若被明确拒绝，例如缺少发送权限、参数错误或限流，不留下待对账状态，修复权限后可在后续调度或重启后恢复。若此前已有超时或响应丢失，后续一次明确拒绝不能证明此前未发送，仍保留原发送时间和 UUID 对账。未知服务端错误、无效响应及可能发生在发送后的 CLI 输出策略错误均按结果不确定处理。

关单准备失败不登记已提交记录；最终检查后遇到明确拒绝，只清理本次待确认操作。`commit` 后超时、响应丢失或不可解析时保留 `close_pending` 并查询远端，重启后同样恢复，不能因一次详情仍为开放就重放旧 PUT。远端确认关闭且备注属于本次操作后才补发关闭说明；长期仍不确定时需人工核对远端状态，不删除本地记录。关单参数和服务台授权通过 stdin 传递，协议输出及日志不包含凭据。

CLI 自身消息可能先产生回调、后返回发送回执。接收层依据已有发送记录识别并保存早到的自动消息回执；不会因为发送人和 CLI 用户相同而暂存其所有消息。客服手动沟通立即交给原生 IM，回执查询或自动化暂停均不阻塞它，也不再产生等待回执的内存消息队列。仅有统一标识的消息不能冒充本机自动发送，仍须匹配发送记录。工单关闭后继续识别自动回流；同一聊天关联新工单时，仍会核对该聊天内旧工单的待确认发送，旧关闭说明不会重置新工单或进入普通回答。旧版发送记录没有目标群字段时，沿用已核实的工单群关联；不同群的记录不能互相匹配。多个待确认发送同时匹配时，先过滤自动回流，保持回执不确定，不任意累计其中一个工单的次数。欢迎和关闭说明使用持久化幂等键；兼容同 ID 再次关闭时使用新的关闭轮次键。正常关单确认与响应丢失恢复均检查查询期间的状态版本、关闭轮次和待办归属，并合并最新详情；查询发现已重新打开时作废旧通知，旧的关闭结果不能覆盖新的开放状态。恢复流程还会核对关闭备注是否匹配本次操作，然后补发关闭说明。

## 验证

自动测试使用本地模拟，不发送真实飞书消息或关单：

```bash
PYTHONPATH=bot:. python -m pytest -c bot/pytest.ini \
  bot/tests/test_helpdesk_delivery.py \
  bot/tests/test_helpdesk_followup.py \
  bot/tests/test_helpdesk_escalation.py \
  bot/tests/test_helpdesk_feed_resolver.py \
  bot/tests/test_helpdesk_web_reply.py \
  bot/tests/test_helpdesk_followup_concurrency.py \
  bot/tests/test_feishu_helpdesk.py \
  bot/tests/test_feishu_helpdesk_api.py \
  bot/tests/test_feishu_helpdesk_cli.py \
  bot/tests/test_helpdesk_cli_credentials.py \
  bot/tests/test_helpdesk_close_helper.py \
  bot/tests/test_helpdesk_close_protocol.py \
  bot/tests/test_agent_session_concurrency.py \
  bot/tests/test_session_dispatcher.py \
  bot/tests/test_message_source_context.py \
  bot/tests/test_background_tasks.py \
  bot/tests/test_background_task_segments.py -q
```

部署前在测试服务台核验真实长连接、Token、当前用户访问权限、3／6／9 分钟提醒与升级时间线、升级群 30 分钟重复提醒、已解决立即关单和停机补漏。上游接入点和验证记录见 [合并说明](helpdesk-followup-merge.md)。

## API 依据

- [工单查询](https://open.feishu.cn/document/server-docs/helpdesk-v1/ticket-management/ticket/list.md)、[详情](https://open.feishu.cn/document/server-docs/helpdesk-v1/ticket-management/ticket/get.md)
- [消息查询](https://open.feishu.cn/document/server-docs/helpdesk-v1/ticket-management/ticket-message/list.md)、[消息事件](https://open.feishu.cn/document/server-docs/helpdesk-v1/ticket-management/ticket-message/events/created.md)
- [订阅](https://open.feishu.cn/document/server-docs/helpdesk-v1/event/subscribe.md)、[用户关单](https://open.feishu.cn/document/server-docs/helpdesk-v1/ticket-management/ticket/update.md)、[IM 详情](https://open.feishu.cn/document/server-docs/im-v1/message/get.md)
- [Bot 发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create.md)、[群信息](https://open.feishu.cn/document/server-docs/group/chat/get.md)
- [IM 群历史与回执对账](https://open.feishu.cn/document/server-docs/im-v1/message/list.md)
