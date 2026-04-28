# 飞书流式回复方案

## 概述

当前 Bot 的回复流程是：LLM 生成完整回复 → `send_text()` 一次性发送。用户需等待 1-5 秒才看到回复。

目标改为：发送一张 interactive card → LLM 流式生成 → 每 500ms 或每 N 个 token 更新卡片内容 → 实现打字机效果。

## 架构

```
用户发消息
    ↓
TextHandler.handle()
    ├── 发送初始卡片（"正在思考..."）
    ├── LLM stream = chat_stream()  ← 逐 chunk 返回
    ├── 每 500ms update_card()      ← PATCH 消息
    └── 最终更新（完整回复）
```

## 涉及的文件与改动

### 1. `services/llm.py` — 新增 `chat_stream()`

```python
def chat_stream(messages, config) -> Generator[str, None, str]:
    """流式调用 LLM，逐 chunk yield 文本，最后返回完整文本"""
    client = get_client(config)
    stream = client.chat.completions.create(
        model=config.openai_model,
        messages=messages,
        stream=True,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    full_content = ""
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            full_content += delta
            yield delta
    return full_content
```

### 2. `feishu/messages.py` — 新增 `send_card()` 和 `update_card()`

```python
def send_card(conversation_id, card_content, config) -> str:
    """发送 interactive card 消息，返回 message_id"""

def update_card(message_id, card_content, config) -> None:
    """更新已发送的卡片内容（PATCH）"""
    client = _get_client(config)
    request = (PatchMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            PatchMessageRequestBody.builder()
            .content(json.dumps({"content": card_json}))
            .build()
        )
        .build())
    client.im.v1.message.patch(request)
```

### 3. `services/handlers/text_handler.py` — 修改 `handle()`

```python
def handle(self, inbound):
    # ... 同上：system prompt 修复、追加用户消息 ...

    # 发送初始卡片
    msg_id = send_card(inbound.conversation_id, initial_card(), self.config)

    # 流式调用 LLM
    full_reply = ""
    last_update = time.time()
    buffer = ""
    for chunk in chat_stream(context, self.config):
        full_reply += chunk
        buffer += chunk
        now = time.time()
        if now - last_update > 0.5:  # 每 500ms 更新一次
            update_card(msg_id, build_card(full_reply), self.config)
            last_update = now
            buffer = ""

    # 最终更新（完整回复）
    update_card(msg_id, build_card(full_reply), self.config)

    # 保存到 session
    session.messages.append({"role": "assistant", "content": full_reply})
    ...
```

### 4. 卡片 JSON 模板

```python
def build_card(text: str, is_final: bool = False) -> dict:
    """构建飞书 interactive card"""
    header_title = "🤖 AI 回复完成" if is_final else "🤖 AI 回复中..."
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": "blue" if not is_final else "green",
        },
        "elements": [
            {"tag": "markdown", "content": text or "正在思考..."},
        ],
    }
```

## 关键设计决策

### 更新频率
- **每 500ms 或每收到 5 个 chunk**（取先到的条件）
- 太频繁 → Feishu API rate limit（未公开具体限制，建议保守）
- 太低频 → 用户等待感强，失去流式意义

### 错误处理
- 某次 `update_card()` 失败 → 继续 append buffer，下个周期重试
- LLM stream 中断 → 用已累积的 `full_reply` 做最终更新，改为 error card
- 初始 `send_card()` 失败 → fallback 回原来的 `send_text()` 一次性发送

### 线程安全
- streaming 已在后台线程中（`_process_in_background`）
- `update_card()` 使用独立 HTTP client（与现有 `send_text` 一致）

### 可配置开关

```python
# config.py
feishu_streaming: bool = field(
    default_factory=lambda: os.getenv("FEISHU_STREAMING", "true").lower() in ("true", "1")
)
feishu_streaming_interval: float = field(
    default_factory=lambda: float(os.getenv("FEISHU_STREAMING_INTERVAL", "0.5"))
)
```

## 工作量评估

| 文件 | 改动类型 | 预估行数 |
|------|----------|----------|
| `services/llm.py` | 新增 `chat_stream()` | ~15 行 |
| `feishu/messages.py` | 新增 `send_card()` / `update_card()` / `build_card()` | ~55 行 |
| `services/handlers/text_handler.py` | 修改 `handle()` | ~20 行 |
| `config.py` | 新增 2 个字段 | ~5 行 |
| `.env.example` | 新增注释 | ~3 行 |
| **总计** | | **~98 行** |

## 风险

1. **Feishu API rate limit**：未公开具体限制，高频率更新可能触发 429。
   - 缓解：`streaming_interval` 可配置，默认 500ms。
   - 缓解：失败时递增间隔（exponential backoff）。

2. **卡片长度限制**：飞书卡片 `markdown` 内容有长度限制（约 30KB）。
   - 缓解：超长回复在 `build_card()` 中截断 + 追加 "…（内容过长已截断）"。

3. **OneAPI 兼容性**：当前通过 OneAPI 转发 vLLM，需确认 `stream=True` 在 `extra_body` 同时使用时是否正常。
   - 缓解：可先在本地 `uv run` 测试流式调用再部署。

---

要不要开始实现？大约 100 行代码改动。
