"""
LLM 连通性验证脚本。

运行方式：
  python tests/test_llm.py

依赖：
  export NVIDIA_API_KEY="nvapi-xxxx"
"""
import os
import sys

# 确保能导入 app 包（从项目根目录运行时生效）
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def check_env():
    key = os.environ.get("NVIDIA_API_KEY", "")
    if not key:
        print("❌ 未设置 NVIDIA_API_KEY，请先执行：export NVIDIA_API_KEY='nvapi-xxxx'")
        sys.exit(1)
    print(f"✅ NVIDIA_API_KEY 已设置（前 8 位：{key[:8]}...）")


def check_analyze_stream():
    print("\n─── 测试：流式面试分析（analyze_interview_stream）───")

    from app.llm import LLMAnalyzer
    analyzer = LLMAnalyzer()

    segments = [
        {"speaker": "说话人1", "speaker_id": 0, "text": "请介绍一下你的工作经历。"},
        {"speaker": "说话人2", "speaker_id": 1, "text": "我做了三年后端开发，主要负责微服务架构设计。"},
        {"speaker": "说话人1", "speaker_id": 0, "text": "你熟悉 Python 异步编程吗？"},
        {"speaker": "说话人2", "speaker_id": 1, "text": "熟悉，用过 asyncio 和 FastAPI，了解事件循环原理。"},
    ]
    full_text = "\n".join(f"{s['speaker']}：{s['text']}" for s in segments)

    token_count = 0
    final_result = None

    print("  LLM 输出（流式）：")
    print("  " + "─" * 50)

    for chunk in analyzer.analyze_interview_stream(full_text, segments):
        if isinstance(chunk, str):
            print(chunk, end="", flush=True)
            token_count += 1
        else:
            final_result = chunk

    print("\n  " + "─" * 50)

    if final_result and "error" in final_result:
        print(f"  ❌ 出错：{final_result['error']}")
        sys.exit(1)
    elif final_result and "markdown" in final_result:
        print(f"  ✅ 流式完成，共收到 {token_count} 个 token，"
              f"总字数 {len(final_result['markdown'])} 字")
    else:
        print("  ❌ 未收到有效最终结果")
        sys.exit(1)


if __name__ == "__main__":
    check_env()
    check_analyze_stream()
    print("\n✅ 验证完成")
