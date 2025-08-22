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
from playwright.sync_api import sync_playwright
import queue
from collections import deque
import hashlib

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
    """认证信息数据类，增加使用计数"""
    project_id: str
    auth_headers: Dict[str, str]
    captcha_data: Dict[str, Any]
    timestamp: float
    use_count: int = 0
    max_uses: int = 10
    auth_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    
    def is_valid(self) -> bool:
        """检查认证是否仍然可用"""
        # 检查使用次数
        if self.use_count >= self.max_uses:
            return False
        # 检查时间（超过5分钟也失效）
        if (time.time() - self.timestamp) > 300:
            return False
        return True
    
    def use(self):
        """使用一次认证"""
        self.use_count += 1
        logger.info(f"Auth {self.auth_id} 使用次数: {self.use_count}/{self.max_uses}")


class AuthPool:
    """认证信息池管理器 - 增强容错版"""
    
    def __init__(self, min_pool_size=3, max_pool_size=10):
        self.min_pool_size = min_pool_size
        self.max_pool_size = max_pool_size
        self.pool = deque(maxlen=max_pool_size)
        self.lock = threading.Lock()
        self.refresh_thread = None
        self.stop_refresh = False
        self.stats = {
            'total_created': 0,
            'total_used': 0,
            'total_expired': 0,
            'total_failures': 0,
            'recovery_attempts': 0
        }
        # 新增：失败追踪和自适应策略
        self.consecutive_failures = 0
        self.max_consecutive_failures = 5
        self.backoff_multiplier = 1.0
        self.last_success_time = time.time()
        
    def add_auth(self, auth: AuthInfo):
        """添加认证到池中 - 增强验证版"""
        if not auth or not self._validate_auth_info(auth):
            logger.warning("无效的认证信息，跳过添加")
            return False
            
        with self.lock:
            # 移除无效的认证
            self.pool = deque([a for a in self.pool if a.is_valid()], maxlen=self.max_pool_size)
            self.pool.append(auth)
            self.stats['total_created'] += 1
            # 重置失败计数
            self.consecutive_failures = 0
            self.backoff_multiplier = 1.0
            self.last_success_time = time.time()
            logger.info(f"✅ 添加认证 {auth.auth_id} 到池中，当前池大小: {len(self.pool)}")
            return True
    
    def _validate_auth_info(self, auth: AuthInfo) -> bool:
        """验证认证信息的完整性"""
        if not auth.project_id:
            logger.warning("认证缺少 project_id")
            return False
        
        if not auth.auth_headers.get('cookie'):
            logger.warning("认证缺少必要的 cookie")
            return False
            
        # 检查关键 cookies
        cookie_str = auth.auth_headers.get('cookie', '')
        essential_cookies = ['sophnet_session', 'auth_token', 'user_id']  # 根据实际情况调整
        has_essential = any(cookie in cookie_str for cookie in essential_cookies) or len(cookie_str) > 50
        
        if not has_essential:
            logger.warning("认证缺少关键认证信息")
            return False
            
        logger.info(f"认证 {auth.auth_id} 验证通过")
        return True
    
    def remove_auth(self, auth: AuthInfo):
        """从池中移除指定认证"""
        with self.lock:
            try:
                self.pool.remove(auth)
                self.stats['total_expired'] += 1
                logger.info(f"移除失效认证 {auth.auth_id}")
            except ValueError:
                pass
    
    def get_auth(self) -> Optional[AuthInfo]:
        """从池中获取一个可用的认证 - 增强容错版"""
        with self.lock:
            # 清理无效认证
            valid_auths = [a for a in self.pool if a.is_valid()]
            self.pool = deque(valid_auths, maxlen=self.max_pool_size)
            
            current_size = len(self.pool)
            
            # 如果池为空或过小，发出紧急警告并触发紧急恢复
            if not self.pool:
                logger.error("🚨 认证池完全为空！触发紧急恢复")
                self.stats['total_failures'] += 1
                self.consecutive_failures += 1
                self._trigger_emergency_recovery()
                return None
            elif current_size <= 2:
                logger.warning(f"⚡ 认证池极低 ({current_size} 个)，触发紧急补充")
                self.stats['recovery_attempts'] += 1
            elif current_size < self.min_pool_size:
                logger.warning(f"📉 认证池低于最小值 ({current_size}/{self.min_pool_size})")
            
            # 选择最优认证：优先选择使用次数少且时间较新的
            best_auth = self._select_best_auth()
            if not best_auth:
                logger.error("未找到可用的认证")
                return None
            
            # 预测池状态变化
            remaining_after_use = current_size
            if best_auth.use_count >= best_auth.max_uses - 1:
                remaining_after_use -= 1
                self.pool.remove(best_auth)
                self.stats['total_expired'] += 1
                logger.info(f"🗑️ 认证 {best_auth.auth_id} 达到使用上限，从池中移除")
                
                # 如果移除后池会变得很小，发出警告
                if remaining_after_use <= 1:
                    logger.warning(f"🔥 移除后认证池仅剩 {remaining_after_use} 个，需要快速补充！")
            
            best_auth.use()
            self.stats['total_used'] += 1
            
            # 记录使用情况以便监控
            logger.info(f"📊 使用认证 {best_auth.auth_id} ({best_auth.use_count}/{best_auth.max_uses}), 池剩余: {remaining_after_use}")
            
            return best_auth
    
    def _select_best_auth(self) -> Optional[AuthInfo]:
        """选择最优认证：综合考虑使用次数和时间"""
        if not self.pool:
            return None
            
        # 计算每个认证的评分 (越低越好)
        def auth_score(auth):
            age_factor = (time.time() - auth.timestamp) / 60  # 年龄因子(分钟)
            usage_factor = auth.use_count / auth.max_uses  # 使用率因子
            return age_factor * 0.3 + usage_factor * 0.7
        
        return min(self.pool, key=auth_score)
    
    def _trigger_emergency_recovery(self):
        """触发紧急恢复机制"""
        logger.warning("🚨 触发紧急认证恢复机制")
        if self.consecutive_failures >= self.max_consecutive_failures:
            logger.error(f"连续失败次数达到 {self.consecutive_failures}，增加退避时间")
            self.backoff_multiplier = min(self.backoff_multiplier * 2, 8.0)
    
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
    
    def start_refresh_thread(self, auth_fetcher):
        """启动自动刷新线程 - 智能容错版"""
        self.stop_refresh_flag = False
        
        def refresh_worker():
            logger.info("🔄 启动认证池自动刷新线程 (智能容错版)")
            
            while not self.stop_refresh_flag:
                try:
                    # 检查池大小和状态
                    with self.lock:
                        valid_auths = [a for a in self.pool if a.is_valid()]
                        current_size = len(valid_auths)
                        
                        # 计算即将过期的认证数量 (使用次数超过7次或时间超过4分钟)
                        soon_expire = len([a for a in valid_auths
                                         if a.use_count >= 7 or (time.time() - a.timestamp) > 240])
                        
                        # 检查是否需要应用退避策略
                        backoff_delay = self.backoff_multiplier
                    
                    # 智能补充策略：根据失败情况调整
                    need_replenish = False
                    target_fetch = 0
                    urgency_level = 0  # 紧急程度：0=正常，1=警告，2=紧急
                    
                    if current_size == 0:
                        need_replenish = True
                        target_fetch = self.min_pool_size
                        urgency_level = 2
                        logger.error(f"🚨 认证池完全空，紧急补充 {target_fetch} 个认证")
                    elif current_size < self.min_pool_size:
                        need_replenish = True
                        target_fetch = min(self.min_pool_size - current_size, 2)  # 限制单次获取数量
                        urgency_level = 2 if current_size <= 1 else 1
                        logger.info(f"🔥 池大小 ({current_size}) 低于最小值 ({self.min_pool_size})，需要补充 {target_fetch} 个认证")
                    elif soon_expire > 0 and (current_size - soon_expire) < self.min_pool_size:
                        need_replenish = True
                        target_fetch = 1  # 预防性补充
                        urgency_level = 1
                        logger.info(f"⚠️ 有 {soon_expire} 个认证即将过期，预防性补充认证")
                    elif current_size < (self.min_pool_size + 1):
                        need_replenish = True
                        target_fetch = 1  # 保持缓冲
                        urgency_level = 0
                        logger.info(f"🚀 主动维持认证池缓冲，当前 {current_size} 个")
                    
                    if need_replenish:
                        success_count = 0
                        for i in range(target_fetch):
                            if self.stop_refresh_flag:
                                break
                                
                            logger.info(f"🔄 获取新认证 {i+1}/{target_fetch}...")
                            
                            try:
                                auth = auth_fetcher()
                                if auth and self.add_auth(auth):
                                    success_count += 1
                                    logger.info(f"✅ 成功添加认证 {auth.auth_id}")
                                    
                                    # 成功时重置退避
                                    with self.lock:
                                        self.consecutive_failures = max(0, self.consecutive_failures - 1)
                                        if self.consecutive_failures == 0:
                                            self.backoff_multiplier = 1.0
                                else:
                                    logger.error(f"❌ 获取新认证失败")
                                    with self.lock:
                                        self.consecutive_failures += 1
                                        self.stats['total_failures'] += 1
                                        if self.consecutive_failures >= 3:
                                            self.backoff_multiplier = min(self.backoff_multiplier * 1.5, 8.0)
                                            logger.warning(f"连续失败 {self.consecutive_failures} 次，增加退避时间至 {self.backoff_multiplier:.1f}x")
                                            
                            except Exception as e:
                                logger.error(f"获取认证时异常: {e}")
                                with self.lock:
                                    self.consecutive_failures += 1
                                    self.stats['total_failures'] += 1
                            
                            # 根据紧急程度调整间隔
                            if i < target_fetch - 1:
                                interval = 1.0 if urgency_level >= 2 else (2.0 if urgency_level == 1 else 3.0)
                                time.sleep(interval * backoff_delay)
                        
                        # 记录本轮补充结果
                        if success_count > 0:
                            logger.info(f"✅ 本轮成功补充 {success_count}/{target_fetch} 个认证")
                        else:
                            logger.warning(f"⚠️ 本轮补充失败，0/{target_fetch} 成功")
                    
                    # 根据当前状态调整检查间隔
                    base_interval = 5  # 基础间隔5秒
                    if urgency_level >= 2:
                        check_interval = base_interval // 2  # 紧急情况快速检查
                    elif urgency_level == 1:
                        check_interval = base_interval
                    else:
                        check_interval = base_interval * 2  # 正常情况慢速检查
                        
                    # 应用退避延迟
                    check_interval = int(check_interval * backoff_delay)
                    
                    for _ in range(check_interval):
                        if self.stop_refresh_flag:
                            break
                        time.sleep(1)
                        
                except Exception as e:
                    logger.error(f"刷新线程错误: {e}")
                    with self.lock:
                        self.consecutive_failures += 1
                        self.stats['total_failures'] += 1
                    # 错误恢复等待时间也应用退避
                    time.sleep(3 * self.backoff_multiplier)
            
            logger.info("🛑 认证池刷新线程已停止")
        
        self.refresh_thread = threading.Thread(target=refresh_worker, daemon=True)
        self.refresh_thread.start()
    
    def stop_refresh(self):
        """停止刷新线程"""
        self.stop_refresh_flag = True
        if self.refresh_thread:
            self.refresh_thread.join(timeout=5)


class SophnetAuthFetcher:
    """认证获取器 - 完全使用之前可工作的版本"""
    
    def __init__(self, headless: bool = True):
        self.base_url = "https://www.sophnet.com"
        self.chat_url = "https://www.sophnet.com/#/playground/chat"
        self.headless = True  # 强制设置为 True，确保后台运行
        self.project_id = None
        self.auth_headers = {}
        self.captcha_data = {}
        self.request_queue = queue.Queue()
        
        # 记录浏览器配置
        logger.info(f"SophnetAuthFetcher 初始化 - 无头模式: {self.headless}")
        if not self.headless:
            logger.warning("⚠️  浏览器将以有界面模式运行！")
        
    def fetch_auth(self) -> Optional[AuthInfo]:
        """获取认证信息 - 完全复制之前可工作的方法"""
        logger.info("正在通过浏览器获取认证信息...")
        
        playwright = None
        browser = None
        page = None
        
        # 重置实例变量
        self.project_id = None
        self.auth_headers = {}
        self.captcha_data = {}
        
        try:
            playwright = sync_playwright().start()
            
            # 使用 Chromium，配置为完全后台运行模式
            browser = playwright.chromium.launch(
                headless=True,  # 强制启用无头模式
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-web-security',
                    '--disable-features=IsolateOrigins,site-per-process',
                    '--disable-gpu',  # 禁用GPU加速
                    '--no-first-run',  # 禁用首次运行提示
                    '--disable-background-timer-throttling',
                    '--disable-renderer-backgrounding',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-ipc-flooding-protection',
                    '--disable-default-apps',
                    '--disable-extensions',
                    '--disable-plugins',
                    '--disable-sync',
                    '--disable-translate',
                    '--hide-scrollbars',
                    '--mute-audio',
                    '--no-default-browser-check',
                    '--no-zygote'  # 完全禁用任何UI相关进程
                ]
            )
            
            # 创建浏览器上下文 - 完全复制之前的配置
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                locale='zh-CN',
                timezone_id='Asia/Shanghai'
            )
            
            page = context.new_page()
            
            # 设置请求拦截器 - 完全复制之前的方法
            def handle_route(route):
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
                route.continue_()
            
            # 拦截所有网络请求
            page.route("**/*", handle_route)
            
            # 额外注入 JavaScript 拦截器（备用方案）- 完全复制之前的
            page.add_init_script("""
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
            
            # 快速访问聊天页面，仅等待DOM加载完成
            logger.info("正在快速加载聊天页面...")
            page.goto(self.chat_url, wait_until='domcontentloaded')
            
            # 立即获取当前cookies
            logger.info("正在提取初始cookies...")
            cookies = context.cookies()
            initial_cookies = '; '.join([f"{c['name']}={c['value']}" for c in cookies])
            if initial_cookies:
                self.auth_headers['cookie'] = initial_cookies
                logger.info(f"✅ 获取到初始cookies: {len(cookies)} 个")
            
            # 短暂等待让页面基本渲染完成
            time.sleep(1)
            
            # 尝试找到输入框，但不要求完全加载
            logger.info("寻找聊天输入框...")
            input_selectors = [
                'textarea[placeholder="请输入内容"]',
                'textarea.el-textarea__inner',
                'textarea[autofocus]',
                '.el-textarea textarea'
            ]
            
            input_box = None
            for selector in input_selectors:
                try:
                    # 使用较短的超时时间
                    page.wait_for_selector(selector, timeout=3000)
                    input_box = page.locator(selector).first
                    logger.info(f"✅ 找到输入框: {selector}")
                    break
                except:
                    continue
            
            # 如果没找到输入框，尝试其他方法获取认证信息
            if not input_box:
                logger.warning("⚠️ 未找到输入框，尝试直接从页面获取认证信息...")
                
                # 尝试从页面的localStorage或其他地方获取信息
                try:
                    # 等待一下让页面JavaScript执行
                    time.sleep(2)
                    
                    # 更新cookies
                    cookies = context.cookies()
                    updated_cookies = '; '.join([f"{c['name']}={c['value']}" for c in cookies])
                    if updated_cookies:
                        self.auth_headers['cookie'] = updated_cookies
                        logger.info(f"✅ 更新cookies: {len(cookies)} 个")
                    
                    # 尝试从页面获取project_id
                    try:
                        current_url = page.url
                        if '/projects/' in current_url:
                            parts = current_url.split('/projects/')
                            if len(parts) > 1:
                                project_part = parts[1].split('/')[0]
                                self.project_id = project_part
                                logger.info(f"✅ 从URL获取project_id: {self.project_id}")
                    except:
                        pass
                    
                except Exception as e:
                    logger.warning(f"从页面获取认证信息失败: {e}")
            
            else:
                # 如果找到输入框，发送快速测试消息
                logger.info("发送测试消息...")
                try:
                    # 清空并输入消息
                    input_box.click()
                    time.sleep(0.3)  # 减少等待时间
                    input_box.fill("test")
                    time.sleep(0.3)  # 减少等待时间
                    
                    # 按回车发送
                    input_box.press('Enter')
                    logger.info("📤 已发送测试消息")
                    
                    # 等待请求被拦截，减少等待时间
                    logger.info("等待拦截请求...")
                    for i in range(5):  # 减少等待循环次数
                        if self.project_id and self.auth_headers.get('authorization'):
                            logger.info(f"✅ 成功获取认证信息 (等待 {i+1}秒)")
                            break
                        time.sleep(1)
                        
                        # 更新cookies
                        cookies = context.cookies()
                        updated_cookies = '; '.join([f"{c['name']}={c['value']}" for c in cookies])
                        if updated_cookies:
                            self.auth_headers['cookie'] = updated_cookies
                        
                except Exception as e:
                    logger.warning(f"发送测试消息失败: {e}")
            
            # 最终检查和构建认证信息
            logger.info("正在构建最终认证信息...")
            
            # 确保有基本的cookies
            if not self.auth_headers.get('cookie'):
                cookies = context.cookies()
                if cookies:
                    cookie_str = '; '.join([f"{c['name']}={c['value']}" for c in cookies])
                    self.auth_headers['cookie'] = cookie_str
                    logger.info(f"✅ 最终获取到cookies: {len(cookies)} 个")
            
            # 尝试从页面获取project_id（如果还没有）
            if not self.project_id:
                try:
                    # 尝试从当前URL获取
                    current_url = page.url
                    if '/projects/' in current_url:
                        parts = current_url.split('/projects/')
                        if len(parts) > 1:
                            project_part = parts[1].split('/')[0]
                            self.project_id = project_part
                            logger.info(f"✅ 从URL获取project_id: {self.project_id}")
                    
                    # 尝试从localStorage获取
                    if not self.project_id:
                        stored_data = page.evaluate("() => localStorage.getItem('lastInterceptedRequest')")
                        if stored_data:
                            data = json.loads(stored_data)
                            logger.info(f"从localStorage获取到数据: {data.get('url', 'N/A')}")
                except Exception as e:
                    logger.warning(f"获取project_id失败: {e}")
                
                # 使用默认值作为最后手段
                if not self.project_id:
                    logger.warning("⚠️ 未能获取project_id，使用默认值")
                    self.project_id = "Ar79PWUQUAhjJOja2orHs"
            
            # 设置基本的认证头
            if not self.auth_headers.get('user-agent'):
                self.auth_headers['user-agent'] = random.choice(USER_AGENTS)
            
            if not self.auth_headers.get('accept'):
                self.auth_headers['accept'] = 'application/json'
            
            # 构建认证信息（即使没有完整信息也尝试构建）
            if self.project_id and self.auth_headers.get('cookie'):
                auth_info = AuthInfo(
                    project_id=self.project_id,
                    auth_headers=self.auth_headers,
                    captcha_data=self.captcha_data,
                    timestamp=time.time()
                )
                
                logger.info(f"✅ 快速获取认证信息完成!")
                logger.info(f"   Project ID: {auth_info.project_id}")
                logger.info(f"   Auth ID: {auth_info.auth_id}")
                logger.info(f"   有cookies: {bool(auth_info.auth_headers.get('cookie'))}")
                logger.info(f"   有authorization: {bool(auth_info.auth_headers.get('authorization'))}")
                
                return auth_info
            else:
                logger.error("未能获取基本认证信息（project_id或cookies缺失）")
                return None
                
        except Exception as e:
            logger.error(f"获取认证失败: {e}")
            import traceback
            traceback.print_exc()
            return None
            
        finally:
            if page:
                page.close()
            if browser:
                browser.close()
            if playwright:
                playwright.stop()


class SophnetOpenAIAPI:
    """Sophnet OpenAI 兼容 API"""
    
    def __init__(self, auth_pool: AuthPool):
        self.auth_pool = auth_pool
        self.base_url = "https://www.sophnet.com"
        
    def call_sophnet_api(self, messages: List[Dict], model: str, stream: bool = False,
                         **kwargs) -> Optional[requests.Response]:
        """调用 Sophnet API"""
        
        max_retries = 3  # 最多重试3次
        
        for retry_count in range(max_retries):
            # 从池中获取认证
            auth = self.auth_pool.get_auth()
            if not auth:
                logger.error("无法从池中获取认证")
                return None
            
            logger.info(f"使用认证 {auth.auth_id} (已用 {auth.use_count}/{auth.max_uses}) - 尝试 {retry_count + 1}/{max_retries}")
            
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
                    logger.error(f"API 调用失败: {response.status_code} (Auth: {auth.auth_id})")
                    logger.error(f"响应内容: {response.text}")
                    
                    # 检查是否是认证失效的错误
                    try:
                        error_data = response.json()
                        if error_data.get("message") == "You must log in first" or error_data.get("status") == 10025:
                            logger.warning(f"🔴 认证 {auth.auth_id} 已失效，从认证池中移除")
                            # 从认证池中移除失效的认证
                            self.auth_pool.remove_auth(auth)
                            # 如果还有重试次数，继续尝试下一个认证
                            if retry_count < max_retries - 1:
                                logger.info(f"🔄 准备使用下一个认证重试...")
                                continue
                    except:
                        pass
                    
                    # 如果无法解析错误或已经是最后一次重试，返回None
                    if retry_count == max_retries - 1:
                        logger.error(f"已达到最大重试次数，认证失败")
                        return None
                    else:
                        continue
                else:
                    logger.error(f"API 调用失败: {response.status_code} (Auth: {auth.auth_id})")
                    logger.error(f"响应内容: {response.text}")
                    # 对于其他错误，不重试直接返回None
                    return None
                    
            except Exception as e:
                logger.error(f"请求异常: {e}")
                # 对于网络异常等，如果还有重试次数，可以尝试下一个认证
                if retry_count < max_retries - 1:
                    logger.info(f"🔄 请求异常，尝试使用下一个认证...")
                    continue
                return None
        
        # 如果所有重试都失败了
        logger.error("所有重试都失败了")
        return None
    
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


def initialize():
    """初始化：填充认证池并启动刷新线程"""
    logger.info("🚀 正在初始化服务...")
    
    # 初始填充认证池
    logger.info(f"正在填充认证池 (目标: {auth_pool.min_pool_size} 个认证)...")
    
    for i in range(auth_pool.min_pool_size):
        logger.info(f"获取认证 {i+1}/{auth_pool.min_pool_size}...")
        auth = auth_fetcher.fetch_auth()
        if auth:
            auth_pool.add_auth(auth)
        else:
            logger.warning(f"获取认证 {i+1} 失败")
        
        # 避免过快请求
        if i < auth_pool.min_pool_size - 1:
            time.sleep(2)
    
    # 启动自动刷新线程
    auth_pool.start_refresh_thread(auth_fetcher.fetch_auth)
    
    pool_status = auth_pool.get_pool_status()
    logger.info(f"✅ 初始化完成! 认证池状态: {pool_status['pool_size']} 个可用认证")


if __name__ == '__main__':
    # 初始化
    initialize()
    
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
    
    app.run(host='0.0.0.0', port=8080, debug=False)