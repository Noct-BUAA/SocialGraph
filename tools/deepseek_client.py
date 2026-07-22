#!/usr/bin/env python3
"""DeepSeek API 客户端 — 替代 qwen_runner.py，提供相同的 generate() 接口

前置条件: 设置环境变量 DEEPSEEK_API_KEY="sk-..."
用法:
  from deepseek_client import generate
  result = generate(system_prompt, user_text, max_tokens=40, temperature=0.1)
"""

from __future__ import annotations
import os, json, time

# 清除代理 — 避免 SSL EOF 错误
for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"]:
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"

from openai import OpenAI

# DeepSeek API 配置
BASE_URL = "https://api.deepseek.com/v1"
MODEL = "deepseek-chat"  # 或 deepseek-reasoner 如果需要推理

# 从环境变量读取 API Key
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
if not API_KEY:
    # 尝试从 .env 文件读取
    env_paths = [os.path.join(os.path.dirname(__file__), "..", ".env"),
                 os.path.join(os.path.dirname(__file__), ".env"),
                 ".env"]
    for p in env_paths:
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DEEPSEEK_API_KEY="):
                        API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            if API_KEY:
                break
        except FileNotFoundError:
            continue

_client = None

def get_client():
    """懒加载 OpenAI client"""
    global _client
    if _client is None:
        if not API_KEY:
            raise RuntimeError(
                "DEEPSEEK_API_KEY 未设置。请设置环境变量或创建 .env 文件:\n"
                "  export DEEPSEEK_API_KEY=\"sk-...\"\n"
                "  或在项目根目录创建 .env: DEEPSEEK_API_KEY=sk-..."
            )
        _client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    return _client


def generate(system_prompt: str, user_text: str, max_tokens: int = 100, temperature: float = 0.1) -> str:
    """单条消息推理 — 与 qwen_runner.generate() 完全兼容的接口"""
    client = get_client()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]

    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                max_tokens=max_tokens,
                temperature=max(temperature, 0.05),
            )
            raw = resp.choices[0].message.content.strip()
            # 清理 markdown code block（与 Qwen 输出清理一致）
            if raw.startswith("```"):
                lines = raw.split("\n")
                raw = "\n".join(lines[1:]) if len(lines) > 1 else raw[3:]
                if raw.endswith("```"):
                    raw = raw[:-3]
            if raw.startswith("json"):
                raw = raw[4:].strip()
            return raw
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                raise e
    return ""


def generate_batch(contents: list[str], max_tokens_per_item: int = 40) -> list[dict]:
    """批量推理 — 一次 API 调用处理多条消息

    Args:
        contents: 消息内容列表
        max_tokens_per_item: 每条消息的输出 token 数

    Returns:
        list[dict]: 每条消息的 JSON 解析结果
    """
    system_prompt = (
        '分析谢渣渣的微信消息，逐条输出JSON。\n'
        '格式: {"emotion":"neutral_ack|cold_response|warm_reply|angry|sad|joking",'
        '"intensity":0.5,"persons":["名字"],'
        '"conversation_role":"response|topic_opener|deflecting|ending|escalation|standalone"}\n'
        '只输出JSON，每条一行。'
    )

    items = "\n".join(f"{i+1}. {c[:150]}" for i, c in enumerate(contents))
    user_text = f"{len(contents)}条消息，每条一行JSON:\n{items}"

    raw = generate(system_prompt, user_text, max_tokens=len(contents) * max_tokens_per_item, temperature=0.1)

    results = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if line.startswith("{"):
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                results.append({"emotion": "unknown", "intensity": 0, "persons": [], "conversation_role": "response"})

    # 补齐不足的条目
    while len(results) < len(contents):
        results.append({"emotion": "unknown", "intensity": 0, "persons": [], "conversation_role": "response"})

    return results[:len(contents)]


def enrich_one(content: str) -> dict:
    """单条消息标注 → JSON dict（兼容 finish_enrich.py 的接口）"""
    prompt = (
        '分析谢渣渣的微信消息→JSON:{"emotion":"neutral_ack|cold_response|warm_reply|angry|sad|joking",'
        '"intensity":0.5,"persons":["名字"],"conversation_role":"response|topic_opener|deflecting|ending|escalation|standalone"}。只输出JSON。'
    )
    raw = generate(prompt, content[:200], max_tokens=40, temperature=0.1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"emotion": "unknown", "intensity": 0, "persons": [], "conversation_role": "response"}


# 兼容 qwen_runner 的 get_model 接口（graph_api.py 启动预加载时调用）
def get_model():
    """兼容 qwen_runner.get_model() — 验证 API Key 可用性"""
    if not API_KEY:
        print("  ⚠️ DEEPSEEK_API_KEY 未设置，首次调用将报错")
        return None, None
    try:
        client = get_client()
        # 快速验证：列出可用模型
        client.models.list()
        print("  ✅ DeepSeek API 就绪")
        return client, MODEL
    except Exception as e:
        print(f"  ⚠️ DeepSeek API 连接失败 ({e})")
        return None, None


if __name__ == "__main__":
    # 快速测试
    if not API_KEY:
        print("请先设置 DEEPSEEK_API_KEY 环境变量")
        print("  export DEEPSEEK_API_KEY=\"sk-...\"")
        exit(1)

    test_msgs = ["哦哦 知道了", "哈哈哈哈笑死", "没招了 [捂脸]"]
    print("=== 批量测试 ===")
    results = generate_batch(test_msgs)
    for msg, r in zip(test_msgs, results):
        print(f"  {msg:30s} → {r.get('emotion','?'):15s} {r.get('persons',[])}")

    print("\n=== 单条测试 ===")
    r = enrich_one("今天好累啊 [大哭]")
    print(f"  → {r}")
