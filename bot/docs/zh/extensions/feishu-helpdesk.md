# 飞书服务台渠道扩展

本扩展让配置式飞书渠道自动加入服务台工单绑定群。入群后，用户在群内 `@Bot`，由原有 `FeishuChannel` 解析消息、调用 Agent 并回复。普通单聊、群聊继续使用同一个渠道，不需要另开一条长连接。

## 工作流程

1. 普通飞书 IM 按原路径启动；独立后台任务读取机器人 `open_id`，使用服务台凭据订阅工单创建和更新事件，成功后标记服务台就绪并启动 CLI 保活。
2. `helpdesk.ticket.created_v1` 直接读取 `event.chat_id`；`helpdesk.ticket.updated_v1` 读取 `event.object.chat_id`。创建时没有绑定群就等待后续更新，不扫描群列表。
3. 对配置的服务台、当前应用和未关闭工单，先以机器人身份检查是否已在群内。
4. 未入群时，检查 lark-cli 的本地用户登录态；有可用授权才通过 `im chat.members create --as user` 邀请当前应用，再用机器人身份验证成员状态。未登录时跳过邀请，不记录为已入群。接口返回成功、待审批或部分成功均不能代替成员验证。
5. 后续只处理标准 `im.message.receive_v1` 消息，复用原有回复链路。不会把工单消息 ID 当作 IM 消息 ID，也不重复订阅工单消息事件。

网络超时、连接故障、限流和明确可重试的服务端临时错误会在同一个入群任务内重试，最多尝试 4 次，间隔为 2、5、10 秒，无需等待新的工单事件。同群并发事件共享该任务；每次尝试先检查 Bot 是否已在群内，避免响应超时后重复邀请。权限不足、授权失效、待审批和未登录不自动重试。关单或停止渠道会取消请求及退避等待；重试耗尽后记录失败，后续工单更新事件仍可再次触发入群。

服务台的机器人阶段未必有可加入的群。只有飞书事件提供绑定 `chat_id` 后才能加入，通常发生在转人工时。工单已关闭（状态 50/51/52）不触发邀请，不主动修改工单状态。关闭工单后，尚未完成回写的入群任务不能恢复旧关联；停止渠道会清空工单关联缓存，防止迟到消息携带过期来源。

服务台身份查询或订阅缓慢、超时、权限不足时，不阻塞或停止普通飞书 IM。初始化尚未成功时不处理工单事件；失败会明确记录 `Feishu Helpdesk unavailable; native IM remains active`，不视为服务台可用，也不绕过服务台权限。修复服务台配置、网络或权限后重启渠道重新初始化；不在后台无限重试。停止渠道会取消未完成的初始化、工单和保活任务。

## 配置

在 `~/.openviking/ov.conf` 中给已有飞书渠道增加 `helpdesk` 配置。保留现有模型、OpenViking、工具和沙箱配置：

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
        "thread_require_mention": true,
        "helpdesk": {
          "enabled": true,
          "helpdesk_id": "<目标服务台 ID>",
          "helpdesk_token": "<服务台 Token>",
          "cli": {
            "executable": "lark-cli",
            "profile": "support",
            "timeout_seconds": 30,
            "auth_check_interval_seconds": 60,
            "keepalive_interval_seconds": 1800
          }
        }
      }
    ]
  }
}
```

- `helpdesk.enabled` 默认 `false`，不开启时仍创建官方 `FeishuChannel`。
- `helpdesk_id`、`helpdesk_token` 来自目标服务台；仅用于服务台事件订阅。
- `cli.executable` 默认 `lark-cli`，支持可执行文件的绝对路径；使用 argv 调用，不经过 shell。
- `cli.profile` 默认空字符串，使用 CLI 当前默认配置。建议指定专用的 `support` profile，并先通过 CLI 完成该 profile 的应用配置。扩展会校验其 App ID 与飞书渠道一致，不匹配就跳过用户操作。
- 授权的客服用户必须已在目标工单群内，且群设置允许其添加机器人。服务台 Token 和机器人自己的 `tenant_access_token` 不能替代这项用户授权。
- 用户 OAuth 应授权 `im:chat.members:write_only` 和 `offline_access`。本实现按 lark-cli 1.0.66 的命令和 JSON 契约验证；该版本登录时会自动请求 `offline_access`，实际是否获得权限仍取决于应用配置和用户授权。
- 扩展不再使用早期版本的 `user_access_token`、`user_refresh_token`、`oauth_token_url` 配置或 `oauth.json` 缓存；改由 CLI 登录，不复制旧 refresh token，避免两个程序轮换同一凭据。
- 一组应用凭据启动一个配置式飞书渠道实例。不要同时在 Studio 新建同一应用的另一条长连接；Studio 的应用接入向导目前不配置本扩展。

修改后按现有方式重启 `vikingbot gateway` 或 `openviking-server --with-bot`。扩展仅通过 CLI 命令使用授权，不自行读取或解密 CLI 的凭据文件，也不读取 Hermes 登录缓存。

## CLI 授权和后台保活

在飞书应用后台开通上述用户权限，并在“安全设置”开启“刷新 user_access_token”；没有该开关时按飞书文档默认开启。变更后发布应用配置，再使用运行 Gateway 的系统账号、同一份 CLI profile 扫码授权，例如：

```bash
lark-cli --profile support auth login --scope "offline_access im:chat.members:write_only"
```

后台任务随服务台渠道启动、停止，行为如下：

1. 默认每 60 秒执行 `auth status --json`，**不带 `--verify`**，只检查本地登录。未登录、refresh token 已过期、App ID 不匹配时，不执行用户 API；相同状态只提示一次。
2. 本地必须有用户 `openId`、`available: true`，且状态为 `ready` / `needs_refresh`、`tokenStatus` 为 `valid` / `needs_refresh`，才放行。单有 App ID、App Secret 或 bot 身份不算用户已登录。
3. 首次发现可用授权后立即执行一次下面的只读请求，之后默认每 30 分钟执行；若本地状态已变成 `needs_refresh`，不等待常规间隔。access token 是否刷新、refresh token 的轮换及持久化均由 CLI 处理，扩展不硬编码 token 有效期。

   ```bash
   lark-cli --profile support api GET /open-apis/authen/v1/user_info --as user --format json
   ```

4. 临时调用失败按 30、60、120、240、300 秒退避，实际重试在后续本地检查轮次进行；不输出原始 stdout/stderr、用户资料或 token。CLI 明确返回用户认证失败后暂停该授权，直到本地授权信息发生变化；重新扫码后自动恢复保活，无需重启。
5. 邀请和保活共用扩展内的锁，CLI 自身负责跨进程 token 刷新锁。停止渠道时先取消任务并清理运行中的 CLI 子进程，再关闭 HTTP client；POSIX 系统会清理独立进程组，包含 npm 启动器的子进程。

未登录不会阻止原有飞书 IM 渠道启动，已在群中的 Bot 仍能回复。跳过邀请的工单等待后续工单更新事件再次尝试，不扫描历史工单。授权满 365 天、被撤销，或停机超过 refresh token 有效期时，仍需重新授权。

CLI 命令在 Gateway 所在运行环境执行；模型通过 Skills 执行的命令则遵循原有 sandbox 配置。二者需要使用同一份应用配置和可用的授权存储。服务器或容器部署必须提供 CLI 可执行文件，并持久化其配置、安全凭据存储和必要的解密材料；仅挂载 `config.json` 不够。不要把真实配置或凭据提交到 Git。

需要模型使用飞书业务能力时，再通过共享 Skills 安装流程将所需 `lark-*` Skills 暴露到实际工作区的 `skills/` 目录，并确保模型的执行环境可用 CLI。自动邀请和后台保活由固定扩展代码执行，不依赖模型读取 Skill；本扩展不会自动安装或修改共享 Skills。

## 飞书后台设置

1. 为应用启用机器人能力，配置长连接事件接收，订阅现有 `im.message.receive_v1`。
2. 开通服务台事件订阅权限 `helpdesk:all:readonly`；启动时通过服务台专用接口订阅 `helpdesk.ticket.created_v1` 和 `helpdesk.ticket.updated_v1`。
3. 机器人需要成员查询权限，例如 `im:chat.members:read`；邀请用户需要 `im:chat.members:write_only`。应用权限和用户授权均须生效。
4. 沿用已有飞书 IM 的消息读取、发消息、图片、资源和表情等权限，不因开启扩展扩大工具或发送者权限。

## 复用范围与验证

文本、富文本、图片下载与卡片图片、引用回复、话题隔离、处理表情、命令授权、发送者白名单，以及下游 Agent/工具/Skills 均使用现有实现。文件、音频等按上游已有解析能力处理，扩展不另行实现解析器。

本扩展只新增工单会话的发现和机器人入群；不新增工单关闭、字段填写、匿名客服身份映射或服务台客服身份发消息等业务。服务台权限错误会禁用本次启动的服务台能力并明确记录，普通 IM 独立运行；不会绕过权限继续处理工单。

## 模型可见的消息来源

工单事件确认群关联后，扩展会在匹配的入站消息 metadata 中写入 `helpdesk_id` 与 `helpdesk_ticket_id`。上下文构建器仅对已确认服务台的群消息，将这些渠道字段按白名单转换为 system 角色的 `Current Message Source (trusted channel metadata)` 区块，供 Skills 识别来源。服务台消息仍使用标准飞书 IM 事件，不能仅凭 `chat_type=group`、群名、@ 或正文工单号判断。

区块固定标记 `source=feishu_helpdesk`，包含 `channel`、`channel_id`、`session_chat_id`、`chat_type`、`helpdesk_id`、`helpdesk_ticket_id`，以及可用的 `message_id`、`reply_to`、`root_id`。普通单聊、未确认服务台关联的群聊或字段不完整时不添加区块，完整模型输入保持原有结构；缺少标记时由 Skill 按未知来源处理，不推定为生产。

只接受渠道生成的白名单字段，不复制 Token、群标题或任意 metadata，用户正文中的同名标记不改变来源。任务执行中接收追加消息时刷新当前区块，保持在前置 system 消息中以便上下文压缩保留；当前消息未确认服务台关联则清除旧区块。后台任务结果不自动添加入站来源。其他渠道不添加该区块。

代码只提供来源，不推定业务环境或授予访问权限。例如，业务 Skill 可以规定“服务台默认生产只读、普通消息先确认环境”，并允许用户明确指定 test、uat 覆盖默认；此策略需随 Skill 部署，测试环境另行选择。

当前群与工单关联沿用内存缓存，不扫描历史工单。重启后尚未收到对应工单事件的群没有来源区块，按来源未知处理；这不证明它是普通群，不应据此猜测生产环境。仅更新 Skill 无法让旧版 bot 自动生成该区块。

本地回归命令（需要项目 bot/test 依赖）：

```bash
PYTHONPATH=bot python -m pytest -c bot/pytest.ini \
  bot/tests/test_feishu_helpdesk.py \
  bot/tests/test_feishu_helpdesk_api.py \
  bot/tests/test_feishu_helpdesk_cli.py \
  bot/tests/test_feishu_outbound_images.py \
  bot/tests/test_message_source_context.py \
  bot/tests/test_agent_loop_outcome.py
```

真实环境验收：先在未登录的测试 profile 下确认只有一次跳过提示，没有 `user_info` 请求；完成扫码后确认日志出现 `authorized user keepalive active`。新建工单并转人工，确认日志出现 `Feishu Helpdesk bot admitted` 且群成员列表中有机器人；随后测试 `@Bot` 文本、图片/富文本、话题回复，并检查普通飞书单聊和群聊。邀请出现 `232011` 时，表示授权的用户不在目标群；`232017` 表示该用户没有邀请权限。处理权限或重新授权后，由后续工单更新事件再次尝试，不扫描历史工单。

## 与官方代码的边界

主要服务台实现放在 `bot/vikingbot/extensions/feishu_helpdesk/`。官方代码的接入点包括：

- `config/schema.py`：增加可选配置字段。
- `channels/manager.py`：按开关选择扩展子类。
- `channels/feishu.py`：将原有事件注册提取为可覆盖的方法，未改变消息处理和回复实现。
- `agent/context.py` 与 `agent/loop.py`：将入站来源白名单传入模型，并在追加消息时刷新；不包含业务环境默认策略。

## 接口依据

- [工单创建事件](https://open.feishu.cn/document/server-docs/helpdesk-v1/ticket-management/ticket/events/created.md)
- [工单更新事件](https://open.feishu.cn/document/server-docs/helpdesk-v1/ticket-management/ticket/events/updated.md)
- [订阅服务台事件](https://open.feishu.cn/document/server-docs/helpdesk-v1/event/subscribe.md)
- [将用户或机器人拉入群聊](https://open.feishu.cn/document/server-docs/group/chat-member/create.md)
- [判断机器人是否在群](https://open.feishu.cn/document/server-docs/group/chat-member/is_in_chat.md)
- [刷新用户 OAuth 凭据](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/refresh-user-access-token-v3.md)
