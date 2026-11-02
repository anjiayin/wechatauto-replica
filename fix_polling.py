# -*- coding: utf-8 -*-
import re

# 1. Update config.py - change default poll_interval to 5.0
with open('adapter/config.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace('"poll_interval": 1.0', '"poll_interval": 5.0')

with open('adapter/config.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Updated default poll_interval: 1.0s -> 5.0s')

# 2. Update db.py - add error suppression to _poll_once
with open('wechatauto/db.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find _poll_once method and add error counter
in_poll_once = False
modified = False
for i, line in enumerate(lines):
    if 'def _poll_once(self)' in line:
        in_poll_once = True
        # Add error counter initialization after method start
        indent = '        '
        lines.insert(i + 1, indent + 'error_count = getattr(self, "_poll_error_count", 0)\n')
        break

if not in_poll_once:
    print('ERROR: Could not find _poll_once method')
    exit(1)

# Find the get_new_messages call and wrap it with try-except
for i, line in enumerate(lines):
    if 'msgs = self.db.get_new_messages(user, since_seq=since)' in line and not modified:
        indent = '            '
        # Replace with try-except block
        lines[i] = indent + 'try:\n'
        lines.insert(i + 1, indent + '    msgs = self.db.get_new_messages(user, since_seq=since)\n')
        lines.insert(i + 2, indent + 'except sqlite3.DatabaseError as e:\n')
        lines.insert(i + 3, indent + '    self._poll_error_count = error_count + 1\n')
        lines.insert(i + 4, indent + '    if self._poll_error_count % 10 == 0:  # Log every 10 errors\n')
        lines.insert(i + 5, indent + '        sys.stderr.write("[wechatauto] 轮询错误(已抑制): %s\\n" % e)\n')
        lines.insert(i + 6, indent + '    continue\n')
        lines.insert(i + 7, indent + 'except Exception:\n')
        lines.insert(i + 8, indent + '    continue\n')
        modified = True
        break

if not modified:
    print('ERROR: Could not find get_new_messages call')
    exit(1)

with open('wechatauto/db.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)

print('Added error suppression to _poll_once')
