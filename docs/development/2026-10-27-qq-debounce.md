# QQ 消息防抖与真人化发送节奏

日期：2026-10-27。本文记录实现前的开源调研、采用/放弃的方案、默认参数与已知边界。

## 1. 调研结论

| 项目 | 许可证 | 可借鉴的做法 | 已知问题/边界 |
| --- | --- | --- | --- |
| [astrbot_plugin_message_merger](https://github.com/ahuai114514/astrbot_plugin_message_merger) | MIT | “零等待”合并：第一条消息立即进入流程；同一用户后续消息到来时，如果旧 pipeline 尚未真正开始/仍在等待，取消旧请求并合并后重新生成；最多 8 条/4000 字；命令、非纯文本、引用/图片/语音不合并 | 只能取消尚未发送的旧请求；已发消息不能撤回，已消耗 token 不退；工具已开始或已进入流式输出时不能安全取消；依赖 AstrBot 4.25 的 pipeline 取消行为 |
| [qq-maid-bot](https://github.com/kuliantnt/qq-maid-bot) | MIT | 私聊短窗口聚合：quiet **1.2s**、hard max **3s**、最多 10 条/12000 字；保留全部 source message/event id；同批只产生一个逻辑用户回合；命令/Pending/媒体为边界消息；聚合等待不占 worker；群聊明确不在首期聚合 | 内存态，进程异常退出会丢未提交批次；群聊按发送者聚合会破坏群 scope 顺序，因此官方首期只做私聊；不使用 LLM 判断是否说完；批次交付失败需要显式回滚/提示 |
| [astrbot_plugin_proactive_chat](https://github.com/DBJD-CR/astrbot_plugin_proactive_chat) | AGPL-3.0 | 分段回复的“像真人”节奏：保留标点分段；`interval_method=log` 用对数曲线随文本长度增长，随机区间默认 1.5–3.5s；不一次性甩长文 | AGPL，不能把代码复制进本仓库；插件面向 AstrBot 生态；主动消息依赖 prompt，非确定性效果；该插件的分段/限流是“生成后发送”层，不解决入站抢答 |

调研来源还包括 [qq-maid-bot 的消息聚合任务归档](https://github.com/kuliantnt/qq-maid-bot/blob/6160de035278ad981d0e2faeb820f2d8a2a6784a/docs/tasks/archive/message-aggregation.md) 与 [聚合 actor 源码](https://github.com/kuliantnt/qq-maid-bot/blob/8066d6a3c5f4bcc26c3f2c46786354fa94f0cb8a/qq-maid-gateway-rs/src/gateway/aggregator/actor.rs)。这些页面用于比较实现思路；本项目没有复制上述项目的代码。

## 2. 本项目采用的方案

### 2.1 入站防抖（仅 QQ 主人私聊）

链路：

```text
OneBot 私聊消息
→ 停止词/命令/媒体检查
→ QQDebouncer（内存短窗口）
→ 一个 private_batch
→ 原 QQBridge.queue → GenerationService
```

- 只有纯文本私聊进入聚合；图片、语音、命令、`/stop` 等立即处理。
- 同一批次内按到达顺序用换行合并文本；保留每个平台 `request_id`，以第一条作为本轮 canonical request id，避免重复提交。
- 每条新消息重置“静默窗口”，但硬上限不重置；达到硬上限一定提交。
- 静默窗口到期后：
  - 如果话没说完（结尾是逗号/顿号/冒号、连接词、未闭合括号等），继续等，直到新消息或硬上限；
  - 如果看起来说完了，再等一个“最终确认窗口”（final grace），防止标点后马上补一句；这期间有新消息就继续吸收；
  - 最终确认窗口结束才提交。
- 默认参数（比 qq-maid 的 1.2/3s 更慢，贴近连续打字的体感）：
  - 静默 `2.2s`；
  - 硬上限 `8s`；
  - 最终确认 `0.9s`；
  - 单批最多 `8` 条 / `4000` 字；
  - 极短但明确的应答（“嗯嗯”“好”“在吗”等）视为说完。
- 完成度判断是确定性规则，不额外调用 LLM：额外模型调用会增加 latency 和费用，且对短聊天片段分类不稳定。规则覆盖终止标点、续写连接词、未闭合括号和最短字数。
- 群聊不聚合：不同成员各自聚合会打乱群 scope 的到达顺序与注意力判断；这与 qq-maid-bot 的首期限定一致。群聊仍按原路径逐条观察/判断。
- 防抖等待不占用生成 worker 或 provider 并发；只持有 asyncio task 和内存批次。

### 2.2 出站发送节奏

- 在每段消息发送前等待，而不再只等“第二段之后”。
- 文本等待采用对数曲线：基础等待 + `log1p(字数) × 1.05` + 0.15–0.55s 抖动，并受“两条最少间隔”和“单段最长等待”约束。
- 默认：基础 `1.0s`、最少间隔 `1.8s`、单段最长 `8s`；第一段额外限制不超过 `2.5s`，避免生成结束后还让用户等太久。
- 每轮最多发 `8` 段（可在 QQ 设置中调 1–32）。
- 图片/语音等媒体也走同一最小间隔，避免文字和媒体连续刷屏。
- 该节奏同时用于私聊、群聊和主动消息投递；`reply_delay_seconds=0` 完全关闭等待（测试和调试用）。

## 3. 已知边界

- OneBot v11 没有可靠的“正在输入”事件；这里的等待是本地节奏模拟，不是平台 typing indicator。
- 未提交批次是内存态：进程崩溃/重启会丢失；本项目接受该限制，不引入磁盘队列。
- 防抖只覆盖私聊；群聊保持原逐条路径，避免破坏群消息顺序和注意力状态。
- 用户连续发图/语音会作为边界打断文本批次，先提交已收集文字，再处理媒体；不会把媒体重新塞回文本批次。
- 最大等待到期时即使句子看起来没说完也会提交，避免无限等待。
- `reply_delay_seconds=0` 是为现有确定性测试保留的关闭开关；生产默认不要设为 0。

## 4. 测试

- `app/tests/unit/test_qq_debounce.py`：完成度规则、文本合并、配置映射、静默/硬上限/最终确认、未完成续写、条数上限、重复消息、停止取消。
- `app/tests/unit/test_qq_debounce_integration.py`：QQBridge 快速私聊合并、停止清理、非文本边界先刷新、批次只产生一个 ChatRequest；发送节奏对数曲线/最小间隔/关闭开关。
- 既有 QQ 协议、媒体、群聊、注意力、idle 测试回归。
