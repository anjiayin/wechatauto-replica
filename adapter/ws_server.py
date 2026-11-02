# -*- coding: utf-8 -*-
"""WebSocket 客户端 - 连接到 zhb_client 的 WebSocket 服务器并处理命令"""

import asyncio
import json
import time
import traceback
import websockets
from typing import Dict, Any

from adapter.config import WS_HOST, WS_PORT, RECONNECT_INTERVAL


class WxWebSocketClient:
    """微信 WebSocket 客户端 - 连接到 zhb_client"""

    def __init__(self, bridge):
        self.bridge = bridge
        self._ws = None
        self._loop = None
        self._connected = False
        self._running = True

    async def start(self):
        """启动 WebSocket 客户端，自动重连"""
        self._loop = asyncio.get_event_loop()
        ws_url = f'ws://{WS_HOST}:{WS_PORT}'
        print(f'[INFO] WebSocket 客户端目标: {ws_url}', flush=True)

        while self._running:
            try:
                print(f'[INFO] 正在连接到 zhb_client {ws_url} ...', flush=True)
                async with websockets.connect(ws_url, max_size=50*1024*1024) as ws:
                    self._ws = ws
                    self._connected = True
                    print(f'[INFO] 已连接到 zhb_client', flush=True)

                    # 发送连接成功事件，告知服务器我们的身份信息
                    await self._send_json({
                        'post_type': 'meta_event',
                        'event': 'connected',
                        **self.bridge.get_self_info(),
                    })

                    # 处理服务器发来的命令
                    async for message in ws:
                        try:
                            data = json.loads(message)
                            await self._handle_command(data)
                        except json.JSONDecodeError:
                            print(f'[ERROR] 收到非 JSON 消息: {str(message)[:100]}', flush=True)
                        except Exception as e:
                            print(f'[ERROR] 处理消息异常: {e}', flush=True)
                            traceback.print_exc()

            except websockets.exceptions.ConnectionClosed as e:
                print(f'[WARN] 连接关闭: {e}', flush=True)
            except ConnectionRefusedError:
                print(f'[WARN] 连接被拒绝，zhb_client 可能尚未启动', flush=True)
            except OSError as e:
                print(f'[WARN] 网络错误: {e}', flush=True)
            except Exception as e:
                print(f'[ERROR] 连接异常: {type(e).__name__}: {e}', flush=True)
                traceback.print_exc()
            finally:
                self._ws = None
                self._connected = False

            if self._running:
                print(f'[INFO] {RECONNECT_INTERVAL} 秒后重连...', flush=True)
                await asyncio.sleep(RECONNECT_INTERVAL)

    async def _send_json(self, data: dict):
        """发送 JSON 消息到服务器"""
        if self._ws and self._connected:
            try:
                await self._ws.send(json.dumps(data, ensure_ascii=False))
            except Exception as e:
                print(f'[ERROR] 发送消息失败: {e}', flush=True)

    async def _handle_command(self, data: Dict):
        """处理 zhb_client 发来的命令"""
        action = data.get('action', '')
        params = data.get('params', {})
        echo = data.get('echo', '')

        if not action:
            # 可能是服务器推送的信息，忽略
            return

        print(f'[INFO] 收到命令: {action}, echo={echo}', flush=True)

        response = {'echo': echo}

        try:
            if action == 'send_private_msg':
                result = await self.bridge.send_private_msg(
                    params.get('wxid', ''),
                    params.get('message', ''),
                )
                response.update(result)

            elif action == 'send_group_msg':
                result = await self.bridge.send_group_msg(
                    params.get('group_id', ''),
                    params.get('message', ''),
                )
                response.update(result)

            elif action == 'send_image':
                result = await self.bridge.send_image(
                    params.get('wxid', ''),
                    params.get('file_path', ''),
                )
                response.update(result)

            elif action == 'get_sessions':
                sessions = self.bridge.get_sessions(params.get('limit', 50))
                response['status'] = 'ok'
                response['data'] = sessions

            elif action == 'get_contacts':
                contacts = self.bridge.get_contacts(params.get('kind', 'friend'))
                response['status'] = 'ok'
                response['data'] = contacts

            elif action == 'get_groups':
                groups = self.bridge.get_groups()
                response['status'] = 'ok'
                response['data'] = groups

            elif action == 'get_group_members':
                members = self.bridge.get_group_members(params.get('group_id', ''))
                response['status'] = 'ok'
                response['data'] = members

            elif action == 'get_messages':
                messages = self.bridge.get_messages(
                    params.get('wxid', ''),
                    params.get('limit', 20),
                )
                response['status'] = 'ok'
                response['data'] = messages

            else:
                response['status'] = 'failed'
                response['message'] = f'未知动作: {action}'

        except Exception as e:
            response['status'] = 'failed'
            response['message'] = f'执行异常: {e}'
            traceback.print_exc()

        await self._send_json(response)

    async def broadcast_message(self, message_data: Dict):
        """推送消息到 zhb_client"""
        await self._send_json(message_data)

    def stop(self):
        """停止客户端"""
        self._running = False
        if self._ws:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
