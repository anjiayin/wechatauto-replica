import re

with open(r'D:\Git\wechatauto-replica\adapter\main.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Add logging at the start of _on_message
content = content.replace(
    'def _on_message(self, msg: Dict, listener=None):\n        """消息监听回调"""',
    'def _on_message(self, msg: Dict, listener=None):\n        """消息监听回调"""\n        content_preview = msg.get("content", "")[:50]\n        print(f"[MESSAGE] 收到微信消息: {content_preview}...", flush=True)'
)

# Add logging before forwarding to WebSocket
content = content.replace(
    '        if self.ws_client._loop:',
    '        print(f"[MESSAGE] 转发消息到 WebSocket", flush=True)\n        if self.ws_client._loop:'
)

with open(r'D:\Git\wechatauto-replica\adapter\main.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Added logging to main.py')
