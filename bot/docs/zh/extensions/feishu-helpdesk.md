# 飞书服务台渠道扩展

本扩展让配置式飞书渠道自动加入服务台工单绑定群。入群后，用户在群内 `@Bot`，由原有 `FeishuChannel` 解析消息、调用 Agent 并回复。普通单聊、群聊继续使用同一个渠道，不需要另开一条长连接。

启用服务台后，同时运行事件监测、每 10 秒增量发现、每 60 秒小批量存量补扫和持久化到期调度。新工单优先定向同步，不会被存量工单的全量同步阻塞。工单群的欢迎、一次跟进和关闭说明默认使用 `reply.mode="feishu_web"` 的服务台 Web 会话身份发送；普通 IM 升级群默认使用应用 Bot 的官方 IM API。关单通过独立 Python 子进程使用 CLI 当前用户凭据执行；部署与状态机详见 [服务台主动跟进](feishu-helpdesk-followup.md)。

更新官方代码前，按 [bot-main 上游合并影响](upstream-merge-impact.md) 核对核心接入点、扩展依赖及合并后回归。

## 工作流程

1. 普通飞书 IM 按原路径启动；独立后台任务读取机器人 `open_id`，使用服务台凭据订阅工单创建、更新和消息事件，成功后启动状态恢复、主动补漏及 auth-mcp 用户凭据领取。
2. `helpdesk.ticket.created_v1` 直接读取 `event.chat_id`；`helpdesk.ticket.updated_v1` 读取 `event.object.chat_id`。创建时没有绑定群就等待后续更新，不扫描群列表。
3. 对配置的服务台、当前应用和未关闭工单，先以机器人身份检查是否已在群内。
4. 未入群时，检查 lark-cli 的本地用户登录态；有可用授权才通过 `im chat.members create --as user` 邀请当前应用，再用机器人身份验证成员状态。未登录时跳过邀请，不记录为已入群。接口返回成功、待审批或部分成功均不能代替成员验证。
5. Agent 提问仍只处理标准 `im.message.receive_v1`，复用原有回复链路。工单消息事件仅归并监测状态，不重复触发回答；按工单消息 ID 和 IM ID 去重。

网络超时、连接故障、限流和明确可重试的服务端临时错误会在同一个入群任务内重试。邀请返回 `232011`（执行邀请的用户尚未在群内）也会在此流程内重试，等待客服入群；该规则不改变 auth-mcp/CLI 错误分类。最多尝试 6 次，间隔为 2、5、10、30、60 秒，无需等待新的工单事件。累计退避等待 107 秒，不含接口请求耗时；这不是无限等待，不能保证覆盖客服晚 5 分钟入群的情况。同群并发事件共享该任务；每次尝试先检查 Bot 是否已在群内，避免响应超时后重复邀请。`232017` 等邀请权限不足、授权失效、待审批和未登录不新增自动重试。关单或停止渠道会取消请求及退避等待；重试耗尽后记录失败，后续工单更新事件仍可再次触发入群。

机器人身份查询 `is_in_chat` 时，飞书可能以 HTTP 400 / `232006` 表示 Bot 不在群内。此组合按“未入群”处理并继续邀请，不单独记录 ERROR；其他错误码、HTTP 状态以及其他接口返回的 `232006` 仍按异常处理。入群重试 WARN 和工单最终失败 ERROR 带有 `ticket_id`、`chat_id`、错误类型及可用的错误码和 HTTP 状态；重试日志另带尝试次数和等待时间，不输出凭据或原始响应。

服务台的机器人阶段未必有可加入的群。只有可信事件或工单消息对应的 IM 详情确认 `chat_id` 后才能加入，通常发生在转人工时。工单已关闭（状态 50/51/52）不触发邀请。关闭工单后，尚未完成回写的入群任务不能恢复旧关联；停止渠道会清空内存关联缓存，持久化状态供重启同步恢复。

服务台身份查询或订阅缓慢、超时、权限不足时，不阻塞或停止普通飞书 IM。初始化尚未成功时不处理工单事件；失败会明确记录 `Feishu Helpdesk unavailable; native IM remains active`，不视为服务台可用，也不绕过服务台权限。修复服务台配置、网络或权限后重启渠道重新初始化；不在后台无限重试。停止渠道会取消未完成的初始化、工单任务和 HTTP client。

## 配置

在 `~/.openviking/ov.conf` 中给已有飞书渠道增加 `helpdesk` 配置。保留现有模型、OpenViking、工具和沙箱配置。完整配置见 [配置示例](feishu-helpdesk-config.example.json)：

```json
{
  "bot": {
    "channels": [
      {
        "type": "feishu",
        "enabled": true,
        "app_id": "cli_your_app",
        "app_secret": "<应用 App Secret>",
        "bot_name": "<机器人显示名>",
        "allow_from": [],
        "thread_require_mention": true,
        "media_window": {
          "enabled": true,
          "max_images": 4,
          "max_age_minutes": 10,
          "max_messages": 20,
          "same_sender_only": true,
          "include_reply_parent": true
        },
        "helpdesk": {
          "enabled": true,
          "helpdesk_id": "<目标服务台 ID>",
          "helpdesk_token": "<服务台 Token>",
          "followup_interval_minutes": 5,
          "reply": {
            "mode": "feishu_web",
            "auth_mcp_server": "wangoon-auth-mcp",
            "provider": "lark-qr",
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
            "interval_minutes": 45,
            "sender": "bot",
            "groups": {
              "TMS": "oc_tms_escalation_group",
              "SCM": "oc_business_escalation_group",
              "SRM": "oc_business_escalation_group",
              "QMS": "oc_business_escalation_group",
              "PDM": "oc_business_escalation_group",
              "PLM": "oc_business_escalation_group"
            },
            "followup_skill_groups": {
              "TMS技能组": ["TMS"],
              "PDM技能组": ["PDM"],
              "PDM英文技能组": ["PDM"],
              "QMS/SCM英文版技能组": ["SCM", "QMS"],
              "供应链业务线": ["SCM", "QMS", "SRM", "PDM", "PLM"],
              "紧急故障（大供应链）": ["SCM", "QMS", "SRM", "PDM", "PLM"]
            }
          },
          "cli": {
            "executable": "lark-cli",
            "profile": "support",
            "timeout_seconds": 30,
            "auth_mcp_server": "wangoon-auth-mcp",
            "provider": "lark-cli",
            "environment": "prod",
            "auth_profile": "default",
            "token_cache_seconds": 60
          }
        }
      }
    ]
  }
}
```

- `helpdesk.enabled` 默认 `false`，不开启时仍创建官方 `FeishuChannel`。
- `media_window` 控制群聊图片上下文：非 @ 的图片消息仍会保存图片引用；后续 @Bot 时，会在当前图片、被引用父消息和同发送者最近消息中选取图片附加到当前轮。只限制图片数量、时间和消息条数，不限制单张图片大小。历史仍只保存文字，不会把图片重复注入每一轮请求。
- 服务台收到带图片的实时消息时，会把同一消息内的全部图片一次性交给视觉模型生成摘要并写回工单状态；自动跟进判断使用该摘要，避免每轮重复读图。图片原文件仍保留在本地缓存中，供人工回复时重新读取。
- `allow_from` 是该飞书渠道的发送者白名单，单聊、普通群和工单群均遵守。上例的 `[]` 表示允许所有用户提问；配置非空列表时，仅列表中的 open_id 可以使用 Bot，其他用户即使在工单群内 `@Bot` 也会被拒绝并记录 `Access denied`。如需开放渠道，在实际生效的 `ov.conf` 中将目标渠道的 `allow_from` 设为 `[]`；本扩展不会自动清空已有名单，也不为工单群绕过名单。命令权限仍按原有 `allow_cmd_from` 和运行模式判断，是否需要 `@Bot` 仍由 `thread_require_mention` 控制。
- `helpdesk_id`、`helpdesk_token` 来自目标服务台，用于读取、订阅及关单的服务台请求头。
- `followup_interval_minutes` 默认 `5`，严格正整数，重启生效。提醒次数固定一次；首次提醒后仍未解决时按 `escalation.groups` 升级，并在升级群每 `escalation.interval_minutes` 分钟提醒一次。
- `reply.mode` 默认 `lark_cli`；服务台工单群推荐 `feishu_web`。后者使用 auth-mcp 的完整原始 Web 会话 Cookie、feed-id 缓存和 Web Messenger 网关，Cookie 不写日志、不落盘，持久化文件只包含 `oc_*` 到内部 feed id 的映射。`auto_resolve_feed_id` 需要可用的无头 Chromium/Playwright；可先用 `feed_ids` 手工固定映射。
- `escalation.sender` 默认 `bot`，升级群通过官方 IM API 以应用 Bot 身份发送；`lark_cli` 使用 CLI 用户身份。两条升级发送路径都不会使用 Web Messenger。
- 启用升级后，工单群首次提醒后再静默 5 分钟即升级，原工单群停止提醒；升级群每 45 分钟重复，直到已解决关单或远端关闭。只有未配置升级路由时才保留旧版业务等待超时关单。
- `followup_skill_groups` 把当前工单技能组映射到一个或多个系统，再复用 `escalation.groups` 解析升级群。只有当前技能组命中该映射才继续欢迎、提醒和判断；未命中时暂停自动跟进并尝试一次 Bot 退群，失败只记录不重试。技能组从范围外切回范围内时自动恢复入群和跟进。转接消息本身不会停止跟进，“原客服已退出会话”也不作为退出条件。
- `cli.executable` 默认官方 `lark-cli`，支持可执行文件的绝对路径；使用 argv 调用，不经过 shell。无需 CLI 补丁或 Go 工具。
- `cli.profile` 默认空字符串，使用 CLI 当前默认配置。建议指定专用的 `support` profile；该 profile 仍需配置与飞书渠道一致的 App ID 和品牌。
- `cli.auth_mcp_server` 默认 `wangoon-auth-mcp`，从 bot 的 `tools.mcp_servers` 中查找认证服务。`cli.provider` 默认 `lark-cli`，`cli.environment` 默认 `prod`，`cli.auth_profile` 默认 `default`。
- 扩展不再执行 `lark-cli auth status`、本地 OAuth 登录或后台保活。每次用户操作先向 auth-mcp 领取 `lark-cli` token，并通过 `LARKSUITE_CLI_APP_ID` + `LARKSUITE_CLI_USER_ACCESS_TOKEN` 注入 CLI；token 在内存中缓存 `cli.token_cache_seconds` 秒。
- auth-mcp 自身优先使用 `lark-cli` refresh token；refresh 失败后才复用唯一扫码源 `lark-qr`。用户只需要在 `lark-qr` 源会话失效时扫一张码，其他 provider 自动授权。
- 关单通过 `lark-cli api PUT /open-apis/helpdesk/v1/tickets/<id>` 执行；最终 `before_call` 业务检查在 PUT 前完成。不再启动 `close_helper`，也不读取 lark-cli 本地加密凭据、Keychain 或 refresh token。
- 授权的客服用户必须已在目标工单群内，且群设置允许其添加机器人。服务台 Token 和机器人自己的 `tenant_access_token` 不能替代这项用户授权。
- 用户 OAuth 继续使用冻结权限。完整跟进/关单至少需要 `helpdesk:all`、`im:message`、消息读取、`im:chat.members:write_only` 和邀请 Bot 所需权限；Web Messenger 发送只需要 `lark-qr` Cookie，不使用 lark-cli token。
- 扩展不再读取早期版本的 `user_access_token`、`user_refresh_token`、`oauth_token_url` 或 `oauth.json`；这些配置已无运行作用。
- 一组应用凭据启动一个配置式飞书渠道实例。不要同时在 Studio 新建同一应用的另一条长连接；Studio 的应用接入向导目前不配置本扩展。

统一认证链路是：

```text
lark-qr 扫码一次
  → auth-mcp 保存飞书 Web Cookie
  → lark-cli 自动授权并保存独立 token
  → VikingBot 通过环境变量把 token 交给官方 CLI
  → token 过期先 refresh，refresh 失败才回到 lark-qr
```

`lark-qr` 二维码必须包含真实 `qrlogin.token`。服务会解码校验；`token:null` 占位码会被丢弃并继续等待，不会投递到飞书。若看到 `token null`，说明是旧码或占位码，应使用最新一张。

未登录不会阻止原有飞书 IM 渠道启动，已在群中的 Bot 仍能回复。已接管工单通过后续事件和补漏同步再次尝试入群；首次启用前的历史工单不自动接管。

CLI 命令在 Gateway 所在运行环境执行；模型通过 Skills 执行的命令遵循原有 sandbox 配置。服务器或容器部署必须提供 CLI 可执行文件、正确的 profile/app 配置，并持久化 auth-mcp 状态目录；仅挂载 `config.json` 不够。不要把真实配置或凭据提交到 Git。

需要模型使用飞书业务能力时，再通过共享 Skills 安装流程将所需 `lark-*` Skills 暴露到实际工作区的 `skills/` 目录。自动邀请和 auth-mcp 验活由固定扩展代码执行，不依赖模型读取 Skill。

## 飞书后台设置

1. 为应用启用机器人能力，配置长连接事件接收，订阅现有 `im.message.receive_v1`。
2. 开通服务台读取/事件订阅权限 `helpdesk:all:readonly`；启动时通过服务台专用接口订阅工单创建、更新和 `helpdesk.ticket_message.created_v1`。
3. 机器人需要成员查询权限，例如 `im:chat.members:read`；邀请用户需要 `im:chat.members:write_only`。应用权限和用户授权均须生效。
4. 沿用已有飞书 IM 的消息读取、发消息、图片、资源和表情等权限，不因开启扩展扩大工具或发送者权限。
5. 当前用户的关单和 CLI 回执查询需要 `helpdesk:all`、`im:message`、消息读取及工单访问权限；Web Messenger 发送还需要 auth-mcp 的 `lark-qr` prod 会话处于 ready。恢复群消息仍受用户读取权限和群成员关系约束。详细权限见主动跟进部署说明。

## 复用范围与验证

文本、富文本、图片下载与卡片图片、引用回复、话题隔离、处理表情、命令授权、发送者白名单，以及下游 Agent/工具/Skills 均使用现有实现。文件、音频等按上游已有解析能力处理，扩展不另行实现解析器。

扩展新增工单发现、Bot 入群，以及固定欢迎、主动跟进和条件关单；不实现匿名客服身份映射或字段填写。服务台初始化失败会明确记录，普通 IM 独立运行；不会绕过权限继续处理工单。

## 模型可见的消息来源

工单事件确认群关联后，扩展会在匹配的入站消息 metadata 中写入 `helpdesk_id` 与 `helpdesk_ticket_id`。上下文构建器仅对已确认服务台的群消息，将这些渠道字段按白名单转换为 system 角色的 `Current Message Source (trusted channel metadata)` 区块，供 Skills 识别来源。服务台消息仍使用标准飞书 IM 事件，不能仅凭 `chat_type=group`、群名、@ 或正文工单号判断。

区块固定标记 `source=feishu_helpdesk`，包含 `channel`、`channel_id`、`session_chat_id`、`chat_type`、`helpdesk_id`、`helpdesk_ticket_id`，以及可用的 `message_id`、`reply_to`、`root_id`。普通单聊、未确认服务台关联的群聊或字段不完整时不添加区块，完整模型输入保持原有结构；缺少标记时由 Skill 按未知来源处理，不推定为生产。

只接受渠道生成的白名单字段，不复制 Token、群标题或任意 metadata，用户正文中的同名标记不改变来源。任务执行中接收追加消息时刷新当前区块，保持在前置 system 消息中以便上下文压缩保留；当前消息未确认服务台关联则清除旧区块。后台任务结果不自动添加入站来源。其他渠道不添加该区块。

代码只提供来源，不推定业务环境或授予访问权限。例如，业务 Skill 可以规定“服务台默认生产只读、普通消息先确认环境”，并允许用户明确指定 test、uat 覆盖默认；此策略需随 Skill 部署，测试环境另行选择。

已接管工单的群关联持久化，重启经工单和消息同步核对后恢复；首次启用前未接管的历史群仍按来源未知处理。缺少来源区块不证明它是普通群，不应据此猜测生产环境。仅更新 Skill 无法让旧版 bot 自动生成该区块。

本地回归命令（需要项目 bot/test 依赖）：

```bash
PYTHONPATH=bot python -m pytest -c bot/pytest.ini \
  bot/tests/test_feishu_helpdesk.py \
  bot/tests/test_feishu_helpdesk_api.py \
  bot/tests/test_feishu_helpdesk_cli.py \
  bot/tests/test_feishu_outbound_images.py \
  bot/tests/test_message_source_context.py \
  bot/tests/test_agent_loop_outcome.py \
  bot/tests/test_openviking_rule_search.py \
  bot/tests/test_compile_preview_hook_signature.py
```

真实环境验收使用测试服务台和测试 profile：核验用户授权、Bot 入群、普通 IM、白名单、话题、媒体、Web Messenger feed 解析及升级群 Bot 发送，然后按主动跟进文档验证 5／10 分钟时间线、45 分钟升级重复、重启补漏和关单条件。自动测试不发送真实飞书消息。

## 与官方代码的边界

主要服务台实现放在 `bot/vikingbot/extensions/feishu_helpdesk/`。官方代码的接入点包括：

- `config/schema.py`：增加可选配置字段。
- `channels/manager.py`：按开关选择扩展子类，并注入可插拔 reply sender。
- `channels/feishu.py`：将原有事件注册提取为可覆盖的方法，未改变消息处理和回复实现。
- `agent/context.py` 与 `agent/loop.py`：将入站来源白名单传入模型，并在追加消息时刷新；不包含业务环境默认策略。

## 接口依据

- [工单创建事件](https://open.feishu.cn/document/server-docs/helpdesk-v1/ticket-management/ticket/events/created.md)
- [工单更新事件](https://open.feishu.cn/document/server-docs/helpdesk-v1/ticket-management/ticket/events/updated.md)
- [订阅服务台事件](https://open.feishu.cn/document/server-docs/helpdesk-v1/event/subscribe.md)
- [将用户或机器人拉入群聊](https://open.feishu.cn/document/server-docs/group/chat-member/create.md)
- [判断机器人是否在群](https://open.feishu.cn/document/server-docs/group/chat-member/is_in_chat.md)
- [刷新用户 OAuth 凭据](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/refresh-user-access-token-v3.md)
