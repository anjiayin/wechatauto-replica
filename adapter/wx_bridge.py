# -*- coding: utf-8 -*-
"""微信桥接层 - 封装 wechatauto 的数据库读取和 GUI 发送"""

import asyncio
import time
import traceback
from typing import Callable, List, Dict, Any

from wechatauto.db import WeChatDB, Listener, GroupMemberWatcher
from wechatauto.guia import WeChatGUI
from wechatauto.utils.lock import uilock

from adapter.config import (
    POLL_INTERVAL, SEND_LOCK_TIMEOUT, SEND_RETRY_COUNT,
    SEND_VERIFY, SEND_MIN_INTERVAL,
)


class WxBridge:
    """微信桥接层 - 封装消息监听和发送"""

    def __init__(self):
        self.db = WeChatDB()
        self.gui = None
        self.listener = None
        self._last_send_time = 0.0

        try:
            self_info = self.db.get_self_info()
            self.self_wxid = self_info.get('username', '')
            self.self_nickname = self_info.get('nick_name', '')
            print(f'[INFO] 已连接微信账号: {self.self_nickname} ({self.self_wxid})')
        except Exception as e:
            print(f'[ERROR] 获取微信信息失败: {e}')
            self.self_wxid = ''
            self.self_nickname = ''

    def start_listening(self, on_message_callback: Callable) -> None:
        """启动全量消息监听"""
        print('[DEBUG] 创建 Listener 实例...', flush=True)
        self.listener = Listener(self.db, interval=POLL_INTERVAL)
        print('[DEBUG] 调用 add_all 注册全局回调（遍历会话，可能需要时间）...', flush=True)
        self.listener.add_all(on_message_callback, discover=True, fast_init=True)
        print('[DEBUG] add_all 完成，启动监听线程...', flush=True)
        self.listener.start()
        print(f'[INFO] 消息监听已启动，轮询间隔: {POLL_INTERVAL}s', flush=True)

    def stop_listening(self) -> None:
        """停止消息监听"""
        if self.listener:
            self.listener.stop()
            print('[INFO] 消息监听已停止')

    def _ensure_gui(self) -> None:
        """确保 GUI 已初始化"""
        if not self.gui:
            self.gui = WeChatGUI()
            self.gui.bring_to_front()
            time.sleep(0.5)

    async def _throttle_send(self) -> None:
        """发送节流：确保两次发送之间有最小间隔"""
        elapsed = time.time() - self._last_send_time
        if elapsed < SEND_MIN_INTERVAL:
            await asyncio.sleep(SEND_MIN_INTERVAL - elapsed)

    async def send_private_msg(self, wxid: str, message: str) -> Dict[str, Any]:
        """发送私聊消息"""
        try:
            self._ensure_gui()
            await self._throttle_send()

            with uilock:
                for attempt in range(SEND_RETRY_COUNT):
                    try:
                        resp = self.gui.send_msg(message, wxid, verify=SEND_VERIFY)
                        if resp.is_success:
                            self._last_send_time = time.time()
                            return {'status': 'ok', 'message': '发送成功'}
                        else:
                            msg = resp.get('message', '未知错误')
                            print(f'[WARN] 发送失败 (尝试 {attempt+1}/{SEND_RETRY_COUNT}): {msg}')
                            if attempt < SEND_RETRY_COUNT - 1:
                                await asyncio.sleep(1)
                    except Exception as e:
                        print(f'[ERROR] 发送异常 (尝试 {attempt+1}): {e}')
                        if attempt < SEND_RETRY_COUNT - 1:
                            await asyncio.sleep(1)

            return {'status': 'failed', 'message': '发送失败，已达最大重试次数'}
        except Exception as e:
            traceback.print_exc()
            return {'status': 'failed', 'message': f'发送异常: {e}'}

    async def send_group_msg(self, group_id: str, message: str) -> Dict[str, Any]:
        """发送群消息"""
        return await self.send_private_msg(group_id, message)

    async def send_image(self, wxid: str, file_path: str) -> Dict[str, Any]:
        """发送图片"""
        try:
            self._ensure_gui()
            await self._throttle_send()

            with uilock:
                for attempt in range(SEND_RETRY_COUNT):
                    try:
                        resp = self.gui.send_image(file_path, wxid, verify=SEND_VERIFY)
                        if resp.is_success:
                            self._last_send_time = time.time()
                            return {'status': 'ok', 'message': '图片发送成功'}
                        else:
                            msg = resp.get('message', '未知错误')
                            print(f'[WARN] 图片发送失败 (尝试 {attempt+1}/{SEND_RETRY_COUNT}): {msg}')
                            if attempt < SEND_RETRY_COUNT - 1:
                                await asyncio.sleep(1)
                    except Exception as e:
                        print(f'[ERROR] 图片发送异常 (尝试 {attempt+1}): {e}')
                        if attempt < SEND_RETRY_COUNT - 1:
                            await asyncio.sleep(1)

            return {'status': 'failed', 'message': '图片发送失败'}
        except Exception as e:
            traceback.print_exc()
            return {'status': 'failed', 'message': f'图片发送异常: {e}'}

    def get_sessions(self, limit: int = 50) -> List[Dict]:
        try:
            return self.db.get_sessions(limit=limit)
        except Exception as e:
            print(f'[ERROR] 获取会话列表失败: {e}')
            return []

    def get_contacts(self, kind: str = 'friend') -> List[Dict]:
        try:
            return self.db.get_contacts(kind=kind)
        except Exception as e:
            print(f'[ERROR] 获取联系人列表失败: {e}')
            return []

    def get_groups(self) -> List[Dict]:
        try:
            return self.db.get_groups()
        except Exception as e:
            print(f'[ERROR] 获取群列表失败: {e}')
            return []

    def get_group_members(self, group_wxid: str) -> List[Dict]:
        try:
            watcher = GroupMemberWatcher(self.db, group_wxid)
            return watcher.members()
        except Exception as e:
            print(f'[ERROR] 获取群成员失败: {e}')
            return []

    def get_messages(self, wxid: str, limit: int = 20) -> List[Dict]:
        try:
            return self.db.get_messages(wxid, limit=limit)
        except Exception as e:
            print(f'[ERROR] 获取消息失败: {e}')
            return []

    def get_self_info(self) -> Dict:
        return {
            'self_wxid': self.self_wxid,
            'self_nickname': self.self_nickname,
        }

    def get_nickname(self, wxid: str) -> str:
        try:
            return self.db.get_nickname(wxid) or ''
        except Exception:
            return ''
