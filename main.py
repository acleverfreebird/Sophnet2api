"""
Sophnet OpenAI-Compatible API Server
优化版：验证信息池管理、自动刷新、随机Headers
"""

import json
import time
import uuid
import random
import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional, Any, Generator
from dataclasses import dataclass, field
from flask import Flask, request, Response, jsonify, stream_with_context
from flask_cors import CORS
import requests
import json  
import time  
import asyncio 
import queue  
import random  
from typing import Optional  
from camoufox import AsyncCamoufox, DefaultAddons  
import queue
from collections import deque
from typing import Optional, Dict, Callable 


# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 支持的模型列表
SUPPORTED_MODELS = [
    "DeepSeek-V3-Fast",
    "DeepSeek-R1",
    "DeepSeek-R1-0528",
    "DeepSeek-R1-Distill-Llama-70B",
    "DeepSeek-v3",
    "Qwen3-235B-A22B-Instruct-2507",
    "Qwen3-Coder",
    "Qwen3-14B",
    "Qwen3-235B-A22B",
    "Qwen3-32B",
    "GLM-4.5V",
    "GLM-4.5",
    "Kimi-K2",
    "Qwen2.5-72B-Instruct",
    "Qwen2.5-VL-32B-Instruct",
    "DeepSeek-Prover-V2",
    "Qwen2.5-32B-Instruct",
    "QwQ-32B",
    "Qwen2.5-7B-Instruct",
    "DeepSeek-R1-Distill-Qwen-32B",
    "DeepSeek-R1-Distill-Qwen-7B",
    "Qwen2.5-VL-72B-Instruct",
    "Qwen2.5-VL-7B-Instruct",
    "Qwen2-VL-72B-Instruct",
    "Qwen2-VL-7B-Instruct"
]

# User-Agent 池
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
]

@dataclass



class AuthInfo:  
    """认证信息类"""  
    def __init__(self, project_id: str, auth_headers: dict, captcha_data: dict, timestamp: float, max_uses: int = 10):  
        self.project_id = project_id  
        self.auth_headers = auth_headers  
        self.captcha_data = captcha_data  
        self.timestamp = timestamp  
        self.max_uses = max_uses  
        self.use_count = 0  
        self.auth_id = auth_headers.get('authorization', '').replace('Bearer ', '')[:10]  
      
    def is_valid(self) -> bool:  
        """检查认证是否有效（未过期且未达到使用上限）"""  
        # 检查是否过期（假设24小时过期）  
        if time.time() - self.timestamp > 24 * 3600:  
            return False  
        # 检查使用次数  
        if self.use_count >= self.max_uses:  
            return False  
        return True  
      
    def use(self):  
        """使用认证，增加使用计数"""  
        self.use_count += 1  
  
class AuthPool:
    """认证信息池管理器"""
      
    def __init__(self, min_pool_size=5, max_pool_size=10):
        self.min_pool_size = min_pool_size
        self.max_pool_size = max_pool_size
        self.pool = deque(maxlen=max_pool_size)
        self.lock = threading.Lock()
        self.refresh_thread = None
        self.stop_refresh_flag = False  # 统一使用这个变量名
        self.stats = {
            'total_created': 0,
            'total_used': 0,
            'total_expired': 0,
            'total_removed_401': 0 # 新增统计 401 移除的认证
        }
          
    def add_auth(self, auth: AuthInfo):
        """添加认证到池中"""
        with self.lock:
            # 移除无效的认证
            self.pool = deque([a for a in self.pool if a.is_valid()], maxlen=self.max_pool_size)
            self.pool.append(auth)
            self.stats['total_created'] += 1
            logger.info(f"✅ 添加认证 {auth.auth_id} 到池中，当前池大小: {len(self.pool)}")
      
    def get_auth(self) -> Optional[AuthInfo]:
        """从池中获取一个可用的认证"""
        with self.lock:
            # 清理无效认证
            valid_auths = [a for a in self.pool if a.is_valid()]
            self.pool = deque(valid_auths, maxlen=self.max_pool_size)
              
            if not self.pool:
                logger.warning("⚠️ 认证池为空")
                return None
              
            # 获取使用次数最少的认证
            auth = min(self.pool, key=lambda x: x.use_count)
              
            # 如果该认证即将达到使用上限，从池中移除
            if auth.use_count >= auth.max_uses - 1:
                self.pool.remove(auth)
                self.stats['total_expired'] += 1
                logger.info(f"🗑️ 认证 {auth.auth_id} 达到使用上限，从池中移除")
              
            auth.use()
            self.stats['total_used'] += 1
            return auth

    def remove_auth(self, auth_id: str):
        """从池中移除指定 auth_id 的认证"""
        with self.lock:
            original_size = len(self.pool)
            self.pool = deque([a for a in self.pool if a.auth_id != auth_id], maxlen=self.max_pool_size)
            if len(self.pool) < original_size:
                self.stats['total_removed_401'] += 1
                logger.info(f"🗑️ 认证 {auth_id} 因 401 错误从池中移除。当前池大小: {len(self.pool)}")
            else:
                logger.warning(f"尝试移除认证 {auth_id}，但未在池中找到。")
      
    def get_pool_status(self) -> Dict:
        """获取池状态"""
        with self.lock:
            valid_auths = [a for a in self.pool if a.is_valid()]
            return {
                'pool_size': len(valid_auths),
                'auths': [
                    {
                        'id': a.auth_id,
                        'use_count': a.use_count,
                        'remaining': a.max_uses - a.use_count,
                        'age': int(time.time() - a.timestamp)
                    }
                    for a in valid_auths
                ],
                'stats': self.stats
            }
      
    def start_refresh_thread(self, auth_fetcher: Callable[[], Optional[AuthInfo]]):
        """启动自动刷新线程"""
        self.stop_refresh_flag = False
          
        def refresh_worker():
            logger.info("🔄 启动认证池自动刷新线程")
              
            while not self.stop_refresh_flag:
                try:
                    # 检查池大小
                    with self.lock:
                        current_size = len([a for a in self.pool if a.is_valid()])
                      
                    # 如果池小于最小值，获取新认证
                    if current_size < self.min_pool_size:
                        logger.info(f"池大小 ({current_size}) 低于最小值 ({self.min_pool_size})，获取新认证...")
                          
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        auth = loop.run_until_complete(auth_fetcher())
                        if auth:
                            self.add_auth(auth)
                        else:
                            logger.error("获取新认证失败")
                      
                    # 每30秒检查一次
                    for _ in range(30):
                        if self.stop_refresh_flag:
                            break
                        time.sleep(1)
                          
                except Exception as e:
                    logger.error(f"刷新线程错误: {e}")
                    time.sleep(5)
              
            logger.info("🛑 认证池刷新线程已停止")
          
        self.refresh_thread = threading.Thread(target=refresh_worker, daemon=True)
        self.refresh_thread.start()
      
    def stop_refresh(self):
        """停止刷新线程"""
        self.stop_refresh_flag = True
        if self.refresh_thread:
            self.refresh_thread.join(timeout=5)


class SophnetAuthFetcher:  
    """认证获取器 - 使用 Camoufox 异步浏览器"""  
      
    def __init__(self, headless: bool = True):  
        self.base_url = "https://www.sophnet.com"  
        self.chat_url = "https://www.sophnet.com/#/playground/chat"  
        self.headless = headless  
        self.project_id = None  
        self.auth_headers = {}  
        self.captcha_data = {}  
        self.request_queue = queue.Queue()  
  
    async def fetch_auth(self) -> Optional[AuthInfo]:  
        """获取认证信息 - 使用 Camoufox 异步浏览器"""  
        logger.info("正在通过 Camoufox 异步浏览器获取认证信息...")  
          
        # 重置实例变量  
        self.project_id = None  
        self.auth_headers = {}  
        self.captcha_data = {}  
          
        try:  
            # 启动 Camoufox 异步浏览器 - 修复：排除所有默认插件避免插件路径错误  
            async with AsyncCamoufox(  
                headless=self.headless,  
                os="windows",  
                locale="zh-CN",  
                geoip=True,  
                humanize=True,  
                block_webrtc=True,  
                exclude_addons=list(DefaultAddons)  # 排除所有默认插件  
            ) as browser:  
                  
                page = await browser.new_page()  
                  
                # 设置请求拦截器  
                async def handle_route(route):  
                    """处理拦截的请求"""  
                    request = route.request  
                    url = request.url  
                      
                    # 拦截 completion API 请求  
                    if '/chat/completions' in url:  
                        # 提取 project ID  
                        if '/projects/' in url:  
                            parts = url.split('/projects/')  
                            if len(parts) > 1:  
                                project_part = parts[1].split('/')[0]  
                                self.project_id = project_part  
                          
                        # 获取请求头  
                        headers = request.headers  
                        self.auth_headers = {  
                            'authorization': headers.get('authorization', ''),  
                            'cookie': headers.get('cookie', ''),  
                            'user-agent': headers.get('user-agent', ''),  
                            'accept': headers.get('accept', ''),  
                            'content-type': headers.get('content-type', 'application/json')  
                        }  
                          
                        # 获取请求体  
                        post_data = request.post_data  
                        if post_data:  
                            try:  
                                body_data = json.loads(post_data)  
                                # 提取验证码数据  
                                if 'verifyIntelligentCaptchaRequest' in body_data:  
                                    self.captcha_data = body_data['verifyIntelligentCaptchaRequest']  
                                logger.info(f"✅ 成功拦截 API 请求")  
                                logger.info(f"   Project ID: {self.project_id}")  
                            except Exception as e:  
                                logger.error(f"解析请求体失败: {e}")  
                      
                    # 继续原始请求  
                    await route.continue_()  
                  
                # 拦截所有网络请求  
                await page.route("**/*", handle_route)  
                  
                # 额外注入 JavaScript 拦截器（备用方案）  
                await page.add_init_script("""  
                    // 拦截 fetch 请求  
                    const originalFetch = window.fetch;  
                    window.__interceptedRequests = [];  
                    window.fetch = async function(...args) {  
                        const [url, options] = args;  
                        if (url.includes('/chat/completions')) {  
                            // 保存请求信息  
                            const requestInfo = {  
                                url: url,  
                                method: options?.method || 'GET',  
                                headers: options?.headers || {},  
                                body: options?.body || null,  
                                timestamp: Date.now()  
                            };  
                            window.__interceptedRequests.push(requestInfo);  
                            console.log('Intercepted request:', requestInfo);  
                            // 将信息存储到 localStorage  
                            localStorage.setItem('lastInterceptedRequest', JSON.stringify(requestInfo));  
                        }  
                        return originalFetch.apply(this, args);  
                    };  
                """)  
                  
                # 访问聊天页面  
                logger.info("正在加载聊天页面...")  
                await page.goto(self.chat_url, wait_until='networkidle')  
                  
                # 增加等待时间  
                await asyncio.sleep(3)  
                  
                # 等待聊天界面加载  
                logger.info("等待聊天界面加载...")  
                try:  
                    # 等待输入框出现  
                    await page.wait_for_selector('textarea[placeholder="请输入内容"]', timeout=15000)  
                    logger.info("✅ 聊天界面加载完成")  
                except:  
                    logger.warning("⚠️ 聊天界面加载超时，尝试其他选择器...")  
                    # 尝试其他可能的选择器  
                    selectors = [  
                        'textarea.el-textarea__inner',  
                        'textarea[autofocus]',  
                        '.el-textarea textarea'  
                    ]  
                    found = False  
                    for selector in selectors:  
                        try:  
                            await page.wait_for_selector(selector, timeout=5000)  
                            logger.info(f"✅ 使用备用选择器找到输入框: {selector}")  
                            found = True  
                            break  
                        except:  
                            continue  
                      
                    if not found:  
                        logger.error("❌ 无法找到输入框")  
                        return None  
                  
                # 增加等待时间让页面完全稳定  
                await asyncio.sleep(2)  
                  
                # 发送测试消息  
                logger.info("发送测试消息...")  
                input_selectors = [  
                    'textarea[placeholder="请输入内容"]',  
                    'textarea.el-textarea__inner',  
                    'textarea[autofocus]',  
                    '.el-textarea textarea'  
                ]  
                  
                input_box = None  
                for selector in input_selectors:  
                    try:  
                        if await page.locator(selector).count() > 0:  
                            input_box = page.locator(selector).first  
                            break  
                    except:  
                        continue  
                  
                if input_box:  
                    # 清空并输入消息  
                    await input_box.click()  
                    await asyncio.sleep(0.5)  # 增加点击后的等待  
                    await input_box.fill("test")  
                    await asyncio.sleep(0.5)  # 增加输入后的等待  
                      
                    # 按回车发送  
                    await input_box.press('Enter')  
                    logger.info("📤 已发送测试消息")  
                      
                    # 等待请求被拦截，增加等待时间  
                    logger.info("等待拦截请求...")  
                    for i in range(10):  
                        if self.project_id and self.auth_headers:  
                            logger.info(f"✅ 成功获取认证信息 (等待 {i+1}秒)")  
                            break  
                        await asyncio.sleep(1)  
                else:  
                    logger.error("无法找到输入框")  
                    return None  
                  
                # 检查是否成功获取了必要信息  
                if not self.project_id or not self.auth_headers:  
                    # 尝试从 localStorage 获取  
                    logger.info("尝试从 localStorage 获取...")  
                    try:  
                        stored_data = await page.evaluate("() => localStorage.getItem('lastInterceptedRequest')")  
                        if stored_data:  
                            data = json.loads(stored_data)  
                            logger.info(f"从 localStorage 获取到数据: {data}")  
                    except Exception as e:  
                        logger.error(f"从 localStorage 获取失败: {e}")  
                  
                # 如果还是没有，使用默认值  
                if not self.project_id:  
                    logger.warning("⚠️ 未能获取 project ID，使用默认值")  
                    self.project_id = "Ar79PWUQUAhjJOja2orHs"  # 从你提供的 URL 中的默认值  
                  
                # 构建认证信息  
                if self.project_id and self.auth_headers:  
                    # 为了池管理，每次使用不同的 User-Agent  
                    random_ua = random.choice(USER_AGENTS)  
                    self.auth_headers['user-agent'] = random_ua  
                      
                    auth_info = AuthInfo(  
                        project_id=self.project_id,  
                        auth_headers=self.auth_headers,  
                        captcha_data=self.captcha_data,  
                        timestamp=time.time()  
                    )  
                      
                    logger.info(f"✅ 成功获取认证信息!")  
                    logger.info(f"   Project ID: {auth_info.project_id}")  
                    logger.info(f"   Auth ID: {auth_info.auth_id}")  
                    logger.info(f"   已获取认证头: {bool(auth_info.auth_headers)}")  
                    logger.info(f"   已获取验证码: {bool(auth_info.captcha_data)}")  
                      
                    return auth_info  
                else:  
                    logger.error("未能获取完整的认证信息")  
                    return None  
                      
        except Exception as e:  
            logger.error(f"获取认证失败: {e}")  
            import traceback  
            traceback.print_exc()  
            return None


RETRY_LIMIT = 3 # 定义重试次数

class SophnetOpenAIAPI:
    """Sophnet OpenAI 兼容 API"""
    
    def __init__(self, auth_pool: AuthPool):
        self.auth_pool = auth_pool
        self.base_url = "https://www.sophnet.com"
        
    def call_sophnet_api(self, messages: List[Dict], model: str, stream: bool = False,
                         **kwargs) -> Optional[requests.Response]:
        """调用 Sophnet API，支持重试和认证踢出"""
        
        for attempt in range(RETRY_LIMIT):
            auth = self.auth_pool.get_auth()
            if not auth:
                logger.error(f"尝试 {attempt + 1}/{RETRY_LIMIT}: 无法从池中获取认证")
                if attempt == RETRY_LIMIT - 1: # 最后一次尝试仍未获取到认证
                    return None
                time.sleep(1) # 等待1秒后重试
                continue
            
            logger.info(f"尝试 {attempt + 1}/{RETRY_LIMIT}: 使用认证 {auth.auth_id} (已用 {auth.use_count}/{auth.max_uses})")
            
            # 构建 URL
            url = f"{self.base_url}/api/open-apis/projects/{auth.project_id}/chat/completions"
            
            # 构建请求头 - 保持原有格式
            headers = {
                'accept': 'text/event-stream' if stream else 'application/json',
                'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'content-type': 'application/json',
                'origin': self.base_url,
                'referer': f"{self.base_url}/#/playground/chat",
                'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120"',
                'sec-ch-ua-mobile': '?0',
                'sec-ch-ua-platform': '"Windows"',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-origin'
            }
            
            # 更新认证头
            headers.update(auth.auth_headers)
            
            # 构建请求体
            payload = {
                "model_id": model,
                "messages": messages,
                "stream": str(stream).lower(),
                "temperature": kwargs.get('temperature', 1.0),
                "top_p": kwargs.get('top_p', 1.0),
                "max_tokens": kwargs.get('max_tokens', 2048),
                "frequency_penalty": kwargs.get('frequency_penalty', 0),
                "presence_penalty": kwargs.get('presence_penalty', 0),
                "webSearchEnable": False,
                "stop": kwargs.get('stop', [])
            }
            
            # 添加验证码
            if auth.captcha_data:
                payload['verifyIntelligentCaptchaRequest'] = auth.captcha_data
            
            try:
                logger.info(f"发送请求到: {url}")
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    stream=stream,
                    timeout=60
                )
                
                if response.status_code == 200:
                    logger.info(f"✅ API 调用成功 (Auth: {auth.auth_id})")
                    return response
                elif response.status_code == 401:
                    logger.warning(f"API 调用失败: 401 Unauthorized (Auth: {auth.auth_id})。将认证踢出池并重试。")
                    self.auth_pool.remove_auth(auth.auth_id) # 踢出认证
                    # 不返回，继续下一次循环进行重试
                else:
                    logger.error(f"API 调用失败: {response.status_code} (Auth: {auth.auth_id})")
                    logger.error(f"响应内容: {response.text}")
                    return None # 其他错误直接返回
                    
            except Exception as e:
                logger.error(f"请求异常: {e}")
                if attempt == RETRY_LIMIT - 1: # 最后一次尝试仍异常
                    return None
                time.sleep(1) # 等待1秒后重试
                continue
        
        logger.error(f"在 {RETRY_LIMIT} 次尝试后，API 调用仍然失败。")
        return None # 达到重试次数限制仍未成功
    
    def format_openai_response(self, sophnet_response: str, model: str, 
                              messages: List[Dict], stream: bool = False,
                              reasoning_tokens: int = 0) -> Dict:
        """将 Sophnet 响应格式化为 OpenAI 格式"""
        
        if stream:
            return None
        
        prompt_tokens = sum(len(m.get('content', '')) for m in messages) // 4
        completion_tokens = len(sophnet_response) // 4
        
        response = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": sophnet_response,
                        "refusal": None
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens
            },
            "system_fingerprint": f"fp_{uuid.uuid4().hex[:6]}"
        }
        
        if reasoning_tokens > 0:
            response["usage"]["completion_tokens_details"] = {
                "reasoning_tokens": reasoning_tokens
            }
        
        return response
    
    def stream_generator(self, response: requests.Response, model: str) -> Generator:
        """生成 OpenAI 格式的流式响应，支持 reasoning_content"""
        
        chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        
        think_tag_sent = False
        think_close_tag_sent = False
        has_reasoning = False
        
        for line in response.iter_lines():
            if line:
                line = line.decode('utf-8')
                if line.startswith('data: '):
                    data = line[6:]
                    
                    if data == '[DONE]':
                        if has_reasoning and not think_close_tag_sent:
                            openai_chunk = {
                                "id": chat_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"content": "</think>\n\n"},
                                        "finish_reason": None
                                    }
                                ]
                            }
                            yield f"data: {json.dumps(openai_chunk)}\n\n"
                        
                        yield f"data: [DONE]\n\n"
                        break
                    
                    try:
                        sophnet_data = json.loads(data)
                        if 'choices' in sophnet_data and len(sophnet_data['choices']) > 0:
                            choice = sophnet_data['choices'][0]
                            delta = choice.get('delta', {})
                            
                            content = delta.get('content', '')
                            reasoning_content = delta.get('reasoning_content', '')
                            finish_reason = choice.get('finish_reason')
                            
                            if reasoning_content:
                                has_reasoning = True
                                
                                if not think_tag_sent:
                                    openai_chunk = {
                                        "id": chat_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"content": "<think>"},
                                                "finish_reason": None
                                            }
                                        ]
                                    }
                                    yield f"data: {json.dumps(openai_chunk)}\n\n"
                                    think_tag_sent = True
                                
                                openai_chunk = {
                                    "id": chat_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": reasoning_content},
                                            "finish_reason": None
                                        }
                                    ]
                                }
                                yield f"data: {json.dumps(openai_chunk)}\n\n"
                            
                            if content:
                                if has_reasoning and not think_close_tag_sent:
                                    openai_chunk = {
                                        "id": chat_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"content": "</think>\n\n"},
                                                "finish_reason": None
                                            }
                                        ]
                                    }
                                    yield f"data: {json.dumps(openai_chunk)}\n\n"
                                    think_close_tag_sent = True
                                
                                openai_chunk = {
                                    "id": chat_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": content},
                                            "finish_reason": finish_reason
                                        }
                                    ]
                                }
                                yield f"data: {json.dumps(openai_chunk)}\n\n"
                        
                    except Exception as e:
                        logger.error(f"解析流式响应失败: {e}")


# 创建全局对象
auth_pool = AuthPool(min_pool_size=3, max_pool_size=10)
auth_fetcher = SophnetAuthFetcher(headless=True)  # 调试时使用 headless=False
api = SophnetOpenAIAPI(auth_pool)

# 创建 Flask 应用
app = Flask(__name__)
CORS(app)


@app.route('/v1/models', methods=['GET'])
def list_models():
    """列出可用模型"""
    models = []
    for model_id in SUPPORTED_MODELS:
        models.append({
            "id": model_id,
            "object": "model",
            "created": int(time.time()) - 86400,
            "owned_by": "sophnet",
            "permission": [],
            "root": model_id,
            "parent": None
        })
    
    return jsonify({"object": "list", "data": models})


@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    """聊天完成接口"""
    try:
        data = request.get_json()
        messages = data.get('messages', [])
        model = data.get('model', 'DeepSeek-V3-Fast')
        stream = data.get('stream', False)
        
        if model not in SUPPORTED_MODELS:
            return jsonify({
                "error": {
                    "message": f"Model {model} not found",
                    "type": "invalid_request_error",
                    "code": "model_not_found"
                }
            }), 404
        
        # 调用 API
        response = api.call_sophnet_api(
            messages=messages,
            model=model,
            stream=stream,
            temperature=data.get('temperature', 1.0),
            top_p=data.get('top_p', 1.0),
            max_tokens=data.get('max_tokens', 2048),
            frequency_penalty=data.get('frequency_penalty', 0),
            presence_penalty=data.get('presence_penalty', 0),
            stop=data.get('stop', [])
        )
        
        if not response:
            return jsonify({
                "error": {
                    "message": "Failed to get response from Sophnet API",
                    "type": "api_error",
                    "code": "upstream_error"
                }
            }), 500
        
        if stream:
            return Response(
                stream_with_context(api.stream_generator(response, model)),
                content_type='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache',
                    'X-Accel-Buffering': 'no'
                }
            )
        else:
            # 非流式响应处理
            full_response = []
            reasoning_content = []
            reasoning_tokens = 0
            
            for line in response.iter_lines():
                if line:
                    line = line.decode('utf-8')
                    if line.startswith('data: ') and line != 'data: [DONE]':
                        try:
                            data = json.loads(line[6:])
                            if 'choices' in data and len(data['choices']) > 0:
                                delta = data['choices'][0].get('delta', {})
                                
                                content = delta.get('content', '')
                                if content:
                                    full_response.append(content)
                                
                                reasoning = delta.get('reasoning_content', '')
                                if reasoning:
                                    reasoning_content.append(reasoning)
                            
                            if 'usage' in data:
                                usage = data['usage']
                                if 'completion_tokens_details' in usage:
                                    reasoning_tokens = usage['completion_tokens_details'].get('reasoning_tokens', 0)
                                    
                        except:
                            pass
            
            final_content = ''
            if reasoning_content:
                final_content = '<think>' + ''.join(reasoning_content) + '</think>\n\n'
            final_content += ''.join(full_response)
            
            return jsonify(api.format_openai_response(
                final_content, 
                model, 
                messages,
                reasoning_tokens=reasoning_tokens
            ))
    
    except Exception as e:
        logger.error(f"处理请求失败: {e}")
        return jsonify({
            "error": {
                "message": str(e),
                "type": "internal_error",
                "code": "internal_error"
            }
        }), 500


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查接口"""
    pool_status = auth_pool.get_pool_status()
    return jsonify({
        "status": "healthy",
        "timestamp": int(time.time()),
        "pool_status": pool_status
    })


@app.route('/pool/status', methods=['GET'])
def pool_status():
    """获取认证池状态"""
    return jsonify(auth_pool.get_pool_status())


async def get_first_auth():
    """获取第一个认证"""
    logger.info("🚀 正在获取第一个认证...")
    auth = await auth_fetcher.fetch_auth()
    if auth:
        logger.info("✅ 成功获取第一个认证")
        return auth
    else:
        logger.error("❌ 获取第一个认证失败")
        return None


if __name__ == '__main__':
    # 获取第一个认证
    first_auth = asyncio.run(get_first_auth())
    
    if first_auth:
        # 将第一个认证添加到池中
        auth_pool.add_auth(first_auth)
        
        # 启动服务器
        logger.info("="*50)
        logger.info("🚀 Sophnet OpenAI 兼容 API 服务器")
        logger.info("📍 访问地址: http://localhost:8080")
        logger.info("="*50)
        logger.info("📚 API 端点:")
        logger.info("   GET  /v1/models          - 列出可用模型")
        logger.info("   POST /v1/chat/completions - 聊天完成")
        logger.info("   GET  /health             - 健康检查和池状态")
        logger.info("   GET  /pool/status        - 详细认证池状态")
        logger.info("="*50)
        logger.info("✨ 特性:")
        logger.info("   - 认证池自动管理")
        logger.info("   - 每个认证限制10次使用")
        logger.info("   - 30秒自动刷新机制")
        logger.info("   - 支持思考模型(R1)的reasoning输出")
        logger.info("="*50)
        
        # 启动自动刷新线程，让它继续获取其他认证
        auth_pool.start_refresh_thread(auth_fetcher.fetch_auth)
        
        app.run(host='0.0.0.0', port=8080, debug=False)
    else:
        logger.error("服务启动失败，因为未能获取到第一个认证。请检查网络或SophnetAuthFetcher配置。")