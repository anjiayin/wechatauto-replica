# -*- coding: utf-8 -*-
"""微信适配程序入口 - 优化版"""

import asyncio
import sys
import os
import time
import signal
import threading
from typing import Dict

# 确保项目根目录在 sys.path 最前面（非打包模式）
if not getattr(sys, 'frozen', False):
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
else:
    _project_root = None
if _project_root and _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from adapter.config import WS_HOST, WS_PORT
from adapter.wx_bridge import WxBridge
from adapter.ws_server import WxWebSocketClient


class WechatAdapter:
    """微信适配程序主类"""

    def __init__(self):
        print('[DEBUG] 正在初始化微信桥接层...', flush=True)
        self.bridge = WxBridge()
        print('[DEBUG] 微信桥接层初始化完成', flush=True)
        self.ws_client = WxWebSocketClient(self.bridge)
        self._running = True

    def _on_message(self, msg: Dict, listener=None):
        """消息监听回调"""
        content_preview = msg.get("content", "")[:50]
        print(f"[MESSAGE] 收到微信消息: {content_preview}...", flush=True)
        
        sender_id = msg.get('sender_id', '')
        group_id = ''
        group_name = ''

        username = msg.get('username', '')
        if username and '@chatroom' in username:
            message_type = 'group'
            group_id = username
            group_name = username
        else:
            message_type = 'private'

        sender_nickname = msg.get('sender_username', '')
        if not sender_nickname and sender_id and sender_id != 2:
            sender_nickname = self.bridge.get_nickname(str(sender_id))

        message_data = {
            'post_type': 'message',
            'message_type': message_type,
            'wxid': str(sender_id) if sender_id != 2 else self.bridge.self_wxid,
            'self_wxid': self.bridge.self_wxid,
            'self_nickname': self.bridge.self_nickname,
            'sender_nickname': sender_nickname,
            'sender_remark': '',
            'group_id': group_id,
            'group_name': group_name,
            'content': msg.get('content', ''),
            'message_type_id': msg.get('type', '文本'),
            'message_id': msg.get('local_id', 0),
            'time': msg.get('create_time', int(time.time())),
        }

        if sender_id == 2:
            message_data['is_self'] = True

        print(f"[MESSAGE] 转发消息到 WebSocket", flush=True)
        if self.ws_client._loop:
            asyncio.run_coroutine_threadsafe(
                self.ws_client.broadcast_message(message_data),
                self.ws_client._loop,
            )

    def _start_listener_background(self):
        """在后台线程启动消息监听"""
        try:
            print('[DEBUG] 正在后台启动消息监听...', flush=True)
            self.bridge.start_listening(self._on_message)
            print('[DEBUG] 消息监听已启动', flush=True)
        except Exception as e:
            print(f'[ERROR] 启动消息监听失败: {e}', flush=True)
            import traceback
            traceback.print_exc()

    async def run(self):
        """启动适配程序"""
        print('=' * 60, flush=True)
        print('  微信适配程序 v1.0 (WebSocket 客户端模式)', flush=True)
        print(f'  连接目标: ws://{WS_HOST}:{WS_PORT}', flush=True)
        print('=' * 60, flush=True)

        # 在后台线程启动消息监听
        print('[DEBUG] 正在启动消息监听...', flush=True)
        listener_thread = threading.Thread(
            target=self._start_listener_background,
            name='listener-starter',
            daemon=True
        )
        listener_thread.start()

        try:
            # 启动 WebSocket 客户端（自动重连）
            await self.ws_client.start()
        except KeyboardInterrupt:
            print('\n[INFO] 收到退出信号', flush=True)
        finally:
            self.bridge.stop_listening()
            self.ws_client.stop()
            print('[INFO] 微信适配程序已停止', flush=True)


def main():
    """主入口"""
    print('[DEBUG] 程序启动', flush=True)
    
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass

    try:
        print('[DEBUG] 正在创建 WechatAdapter 实例...', flush=True)
        adapter = WechatAdapter()
        print('[DEBUG] WechatAdapter 实例创建成功', flush=True)

        def signal_handler(sig, frame):
            print('\n[INFO] 正在停止...', flush=True)
            adapter._running = False
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        
        print('[DEBUG] 正在启动 asyncio 事件循环...', flush=True)
        asyncio.run(adapter.run())
        print('[DEBUG] asyncio 事件循环已结束', flush=True)
    except Exception as e:
        print(f'[ERROR] 程序启动失败: {type(e).__name__}: {e}', flush=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    print('[DEBUG] __main__ 执行', flush=True)
    main()
