"""
LLM Relay 调用示例 —— 7 个模型 × 2 套协议全覆盖

服务地址: https://offer.yxzrkj.cn/llm/api
API Key:  sk-relay-xxx (向管理员申请)

运行:
    pip install openai anthropic
    python examples/quickstart.py

包含的示例:
    1. OpenAI SDK + Claude (走 /v1/chat/completions)
    2. OpenAI SDK + GPT
    3. OpenAI SDK + Kimi
    4. OpenAI SDK + GLM
    5. OpenAI SDK 流式
    6. Anthropic SDK + Claude (走 /v1/messages)
    7. Anthropic SDK 流式
    8. Anthropic SDK Tools / Agent 闭环
    9. 多轮上下文
   10. 列出所有模型
"""
import os
import sys

# ============ 配置（改这里）============

BASE_URL = "https://offer.yxzrkj.cn/llm/api"
API_KEY = os.getenv("RELAY_API_KEY") or "sk-relay-在这里填你的 key"

# ============ 工具函数 ============

def title(s):
    print(f"\n{'─' * 70}")
    print(f"  {s}")
    print('─' * 70)

# ============ Section A: OpenAI SDK ============

def example_1_openai_claude():
    """OpenAI SDK 调 Claude（OpenAI Chat 协议）"""
    title("1. OpenAI SDK + Claude opus-4-7")
    from openai import OpenAI
    client = OpenAI(base_url=f"{BASE_URL}/v1", api_key=API_KEY)
    r = client.chat.completions.create(
        model="claude-opus-4-7",
        messages=[
            {"role": "system", "content": "你是一个简洁的助手"},
            {"role": "user", "content": "用一句话介绍 Anthropic"},
        ],
        max_tokens=80,
    )
    print(f"  → {r.choices[0].message.content}")
    print(f"  tokens: in={r.usage.prompt_tokens}, out={r.usage.completion_tokens}")

def example_2_openai_gpt():
    title("2. OpenAI SDK + GPT-5.5")
    from openai import OpenAI
    client = OpenAI(base_url=f"{BASE_URL}/v1", api_key=API_KEY)
    r = client.chat.completions.create(
        model="gpt-5-5",
        messages=[{"role": "user", "content": "1+2+3 等于多少？只给数字"}],
        max_tokens=20,
    )
    print(f"  → {r.choices[0].message.content}")

def example_3_openai_kimi():
    title("3. OpenAI SDK + Kimi 2.6")
    from openai import OpenAI
    client = OpenAI(base_url=f"{BASE_URL}/v1", api_key=API_KEY)
    r = client.chat.completions.create(
        model="kimi-2.6",
        messages=[{"role": "user", "content": "你是谁开发的？一句话。"}],
        max_tokens=50,
    )
    print(f"  → {r.choices[0].message.content}")

def example_4_openai_glm():
    title("4. OpenAI SDK + GLM 5.1")
    from openai import OpenAI
    client = OpenAI(base_url=f"{BASE_URL}/v1", api_key=API_KEY)
    r = client.chat.completions.create(
        model="glm-5.1",
        messages=[{"role": "user", "content": "你是哪个公司开发的？"}],
        max_tokens=2000,   # GLM-5.1 是 reasoning 模型，需要足够 budget
    )
    print(f"  → {r.choices[0].message.content[:200]}")

def example_5_openai_stream():
    title("5. OpenAI SDK 流式 (kimi-2.6)")
    from openai import OpenAI
    client = OpenAI(base_url=f"{BASE_URL}/v1", api_key=API_KEY)
    stream = client.chat.completions.create(
        model="kimi-2.6",
        messages=[{"role": "user", "content": "用 30 字介绍 Python"}],
        max_tokens=100,
        stream=True,
    )
    print("  → ", end="", flush=True)
    for chunk in stream:
        if chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="", flush=True)
    print()

# ============ Section B: Anthropic SDK ============

def example_6_anthropic_basic():
    title("6. Anthropic SDK + Claude (走 /v1/messages)")
    import anthropic
    client = anthropic.Anthropic(base_url=BASE_URL, api_key=API_KEY)
    msg = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=80,
        system="你是一个简洁的助手",
        messages=[{"role": "user", "content": "用一句话介绍 Claude"}],
    )
    print(f"  → {msg.content[0].text}")
    print(f"  tokens: in={msg.usage.input_tokens}, out={msg.usage.output_tokens}")

def example_7_anthropic_stream():
    title("7. Anthropic SDK 流式")
    import anthropic
    client = anthropic.Anthropic(base_url=BASE_URL, api_key=API_KEY)
    print("  → ", end="", flush=True)
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=[{"role": "user", "content": "用 30 字介绍 Anthropic"}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
    print()

def example_8_anthropic_tools():
    title("8. Anthropic SDK Tools / Agent 闭环")
    import anthropic
    client = anthropic.Anthropic(base_url=BASE_URL, api_key=API_KEY)

    tools = [{
        "name": "calculator",
        "description": "Perform basic arithmetic. Returns the numeric result.",
        "input_schema": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["add", "sub", "mul", "div"]},
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["operation", "a", "b"],
        },
    }]

    # 实现"工具"
    def run_tool(name, args):
        if name == "calculator":
            ops = {"add": lambda a, b: a + b, "sub": lambda a, b: a - b,
                   "mul": lambda a, b: a * b, "div": lambda a, b: a / b}
            return str(ops[args["operation"]](args["a"], args["b"]))
        return "tool not found"

    # Round 1: 让模型决定用哪个工具
    messages = [{"role": "user", "content": "请帮我算 (12 + 5) 然后再乘以 3，必须用 calculator 工具一步步算"}]
    msg = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=400,
        tools=tools,
        messages=messages,
    )
    print(f"  Round 1 stop_reason: {msg.stop_reason}")

    if msg.stop_reason != "tool_use":
        print(f"  ⚠️  没触发 tool_use，content={msg.content[0].text}")
        return

    # 多轮循环直到完成
    for turn in range(5):
        # 收集所有 tool_use 并算出 tool_result
        tool_results = []
        for blk in msg.content:
            if blk.type == "tool_use":
                result = run_tool(blk.name, blk.input)
                print(f"    Tool: {blk.name}({blk.input}) → {result}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": blk.id,
                    "content": result,
                })

        # 把 assistant 消息和 tool_result 都拼回去
        messages.append({"role": "assistant", "content": msg.content})
        messages.append({"role": "user", "content": tool_results})

        msg = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=400,
            tools=tools,
            messages=messages,
        )
        print(f"  Round {turn+2} stop_reason: {msg.stop_reason}")
        if msg.stop_reason == "end_turn":
            for blk in msg.content:
                if blk.type == "text":
                    print(f"  Final: {blk.text}")
            break

def example_9_multi_turn():
    title("9. 多轮上下文 (OpenAI SDK)")
    from openai import OpenAI
    client = OpenAI(base_url=f"{BASE_URL}/v1", api_key=API_KEY)
    r = client.chat.completions.create(
        model="claude-opus-4-7",
        messages=[
            {"role": "user", "content": "我家里有 3 只猫"},
            {"role": "assistant", "content": "好的，记住了 — 你有 3 只猫。"},
            {"role": "user", "content": "我又领养了 2 只。我家现在共有几只猫？"},
        ],
        max_tokens=80,
    )
    print(f"  → {r.choices[0].message.content}")

def example_10_list_models():
    title("10. 列出所有可用模型")
    from openai import OpenAI
    client = OpenAI(base_url=f"{BASE_URL}/v1", api_key=API_KEY)
    models = client.models.list()
    for m in models.data:
        endpoints = getattr(m, 'endpoints', None) or []
        ep_str = ", ".join(endpoints) if endpoints else "?"
        print(f"  - {m.id:25s}  endpoints: {ep_str}")

# ============ 主入口 ============

def main():
    if API_KEY.startswith("sk-relay-在这里"):
        print("⚠️  请先设置 API_KEY，方法：")
        print("   1) 改本文件 API_KEY 变量")
        print("   2) 或者: export RELAY_API_KEY=sk-relay-xxx")
        sys.exit(1)

    print(f"🌐 BASE_URL: {BASE_URL}")
    print(f"🔑 API_KEY:  {API_KEY[:12]}***{API_KEY[-4:]}")

    examples = [
        example_1_openai_claude,
        example_2_openai_gpt,
        example_3_openai_kimi,
        example_4_openai_glm,
        example_5_openai_stream,
        example_6_anthropic_basic,
        example_7_anthropic_stream,
        example_8_anthropic_tools,
        example_9_multi_turn,
        example_10_list_models,
    ]

    # 命令行参数：跑指定的几个例子（默认跑全部）
    if len(sys.argv) > 1:
        nums = [int(x) for x in sys.argv[1:]]
        examples = [examples[n - 1] for n in nums]

    failed = []
    for fn in examples:
        try:
            fn()
        except Exception as e:
            print(f"  ❌ FAIL: {type(e).__name__}: {e}")
            failed.append(fn.__name__)

    print(f"\n{'═' * 70}")
    if failed:
        print(f"❌ {len(failed)}/{len(examples)} 失败: {failed}")
    else:
        print(f"✅ 全部 {len(examples)} 个示例跑通")

if __name__ == "__main__":
    main()
