# VocuTTS

AstrBot 插件 —— 通过 [Vocu](https://vocu.ai/) TTS API 将机器人的文本回复自动转为语音发送。特别适合 TRPG 跑团等场景，支持括号内容过滤与情绪映射。

## 功能

- 在群聊中开启后，机器人每次发送文本消息后自动追加一条对应的语音消息
- 支持会话级别的开关与配置覆盖，不同群可以使用不同的声音角色
- TRPG 友好：可自动过滤括号中的动作描述（不朗读），或将其中的情绪关键词映射为 Vocu 的情绪控制参数
- 完整暴露 Vocu API 的生成参数（预设、语速、语言、活泼表达、情绪控制等），均可在 WebUI 中配置

## 前置准备

1. 前往 [Vocu API Platform](https://app.vocu.ai/apiKey) 创建 API Key
2. 在 Vocu 控制台创建或选择一个声音角色，记录其 Voice Character ID
3. 在 AstrBot WebUI 的插件配置中填入上述信息

## 指令

所有指令通过 `/vocutts` 命令组调用：

| 指令 | 说明 |
|------|------|
| `/vocutts on` | 开启当前会话的语音合成 |
| `/vocutts off` | 关闭当前会话的语音合成 |
| `/vocutts status` | 查看当前会话的 VocuTTS 状态与配置 |
| `/vocutts voice <id>` | 为当前会话设置声音角色 ID |
| `/vocutts voices` | 列出账号下所有可用的声音角色 |
| `/vocutts style <id>` | 为当前会话设置声音风格（Style/Prompt ID） |
| `/vocutts preset <creative\|balance\|stable>` | 切换生成预设策略 |

## 配置项

在 AstrBot WebUI 的插件配置页面中设置：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `api_key` | string | - | Vocu API Key |
| `api_base_url` | string | `https://v1.vocu.ai` | API 基础 URL |
| `voice_id` | string | - | 默认 Voice Character ID |
| `prompt_id` | string | `default` | 声音风格 ID |
| `preset` | string | `balance` | 生成预设：`creative` / `balance` / `stable` |
| `language` | string | `auto` | 语言：`auto` `zh` `en` `ja` `ko` `fr` `es` `de` `pt` `yue` |
| `speech_rate` | float | `1.0` | 语速（0.5 - 2.0） |
| `vivid` | bool | `false` | 活泼表达模式（仅 V3.0 角色） |
| `break_clone` | bool | `true` | 情绪偏向文本（根据文本语境自动推断情绪） |
| `flash` | bool | `false` | 低延迟模式 |
| `bracket_mode` | string | `strip` | 括号处理方式（见下文） |
| `bracket_pattern` | string | 匹配中英文圆括号和方括号 | 括号匹配正则表达式 |
| `emotion_keywords` | text | 预置 16 个中文关键词 | 情绪关键词映射表（JSON） |

## 括号处理模式

针对 TRPG 等场景中括号内动作描述的处理，提供三种模式：

### `strip`（默认）

移除所有匹配括号及其内容，只朗读对白部分。

> 输入：`"你好啊。（微笑着挥手）今天天气真好。"`
> 朗读：`"你好啊。今天天气真好。"`

### `emotion_hint`

同样移除括号内容不朗读，但会从中提取情绪关键词，映射到 Vocu 的 `emo_switch` 参数影响语音情感。

> 输入：`"你怎么敢！（愤怒地拍桌子）"`
> 朗读：`"你怎么敢！"`（带愤怒情绪）

情绪映射为 5 维数组 `[愤怒, 开心, 中性, 悲伤, 匹配上下文]`，值域 0-10。预置关键词包括：愤怒、生气、开心、高兴、微笑、悲伤、难过、哭、平静、冷漠、温柔、紧张、害怕、惊讶等。可在 `emotion_keywords` 配置中自定义。

### `keep`

保留括号内容原样朗读。

## 工作原理

```
用户发消息 → AstrBot 处理并回复文本 → VocuTTS 拦截已发送的文本
→ 处理括号 → 调用 Vocu API 生成语音 → 发送语音消息
```

插件使用 `after_message_sent` 钩子，文本消息会先正常送达，语音紧随其后。即使语音生成失败，文本消息不受影响。
