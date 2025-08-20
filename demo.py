import requests
import json

BASE_URL = "http://localhost:8080/v1"

def list_models():
    """调用 /v1/models 接口获取可用模型列表"""
    url = f"{BASE_URL}/models"
    try:
        response = requests.get(url)
        response.raise_for_status()  # 如果请求失败，抛出 HTTPError
        models = response.json()
        print("可用模型列表:")
        for model in models.get("data", []):
            print(f"- {model['id']}")
        return models
    except requests.exceptions.RequestException as e:
        print(f"获取模型列表失败: {e}")
        return None

def chat_completion(messages, model="DeepSeek-V3-Fast", stream=False):
    """调用 /v1/chat/completions 接口进行聊天补全"""
    url = f"{BASE_URL}/chat/completions"
    headers = {"Content-Type": "application/json"}
    payload = {
        "messages": messages,
        "model": model,
        "stream": stream
    }
    try:
        if stream:
            print(f"\n正在进行流式聊天补全 (模型: {model}):")
            with requests.post(url, headers=headers, json=payload, stream=True) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=None):
                    if chunk:
                        try:
                            # 流式响应可能包含多行数据，每行以 data: 开头
                            for line in chunk.decode('utf-8').splitlines():
                                if line.startswith("data: "):
                                    data = line[6:]
                                    if data == "[DONE]":
                                        break
                                    json_data = json.loads(data)
                                    # 提取并打印内容
                                    if "choices" in json_data and len(json_data["choices"]) > 0:
                                        delta = json_data["choices"][0].get("delta", {})
                                        content = delta.get("content", "")
                                        if content:
                                            print(content, end="", flush=True)
                        except json.JSONDecodeError:
                            print(f"JSON 解析错误: {line}")
                        except Exception as e:
                            print(f"处理流式数据时发生错误: {e}")
            return None
        else:
            print(f"\n正在进行非流式聊天补全 (模型: {model}):")
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()
            if "choices" in result and len(result["choices"]) > 0:
                content = result["choices"][0]["message"]["content"]
                print(f"响应内容:\n{content}")
            else:
                print("未从 API 获取到内容。")
            return result
    except requests.exceptions.RequestException as e:
        print(f"聊天补全失败: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"错误响应: {e.response.text}")
        return None

if __name__ == "__main__":
    print("--- Sophnet OpenAI-Compatible API Demo ---")

    # 3. 进行流式聊天补全
    print("\n--- 测试 /v1/chat/completions (流式) ---")
    messages_stream = [
        {"role": "user", "content": "你好"}
    ]
    chat_completion(messages_stream, model="DeepSeek-V3-Fast", stream=True)

    print("\n--- Demo 结束 ---")