"""检查最新 session"""
import sys, json
sys.path.insert(0, '/app')
from pathlib import Path

# Find latest session
sessions_dir = Path('/app/sessions')
user_dirs = sorted(sessions_dir.glob('dm_*'))
if not user_dirs:
    print('无 session')
    sys.exit(0)

latest = user_dirs[-1]
session_files = sorted(latest.glob('*.json'))
if not session_files:
    print('无 session 文件')
    sys.exit(0)

active = latest / 'active.txt'
active_id = active.read_text().strip() if active.exists() else '?'

print(f'Session: {latest.name}, active: {active_id}')
print(f'文件数: {len(session_files)}')

# Load the active session
if active_id:
    sf = latest / f'{active_id}.json'
else:
    sf = session_files[-1]

if not sf.exists():
    print('active session 不存在')
    sys.exit(0)

data = json.loads(sf.read_text(encoding='utf-8'))
msgs = data.get('messages', [])
print(f'消息数: {len(msgs)}')
for i, m in enumerate(msgs):
    role = m.get('role', '?')
    content = m.get('content', '')
    tool_calls = m.get('tool_calls')
    if isinstance(content, list):
        texts = [c.get('text','') for c in content if c.get('type')=='text']
        content = ' '.join(texts)
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get('function',{}).get('name','?')
            args = tc.get('function',{}).get('arguments','')
            print(f'  [{i}][{role}] tool:{fn}({args[:120]})')
    elif role == 'tool':
        print(f'  [{i}][{role}] {content[:120]}')
    else:
        print(f'  [{i}][{role}] {content[:120].replace(chr(10)," ")}')
