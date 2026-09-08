"""Raw MCP wire contract; this does not impersonate a live Claude UI test."""
import asyncio
import json
import os
from pathlib import Path
import re
import sys

import pytest


@pytest.mark.asyncio
async def test_native_notification_round_trip_over_stdio(tmp_path):
    root = Path(__file__).resolve().parents[1]
    program = f'''
from pathlib import Path
from teammate_mcp import registry, server
registry.REGISTRY_PATH = Path({str(tmp_path / 'registry.json')!r})
registry.LOCK_PATH = Path({str(tmp_path / 'registry.lock')!r})
server.MAILBOX_ROOT = Path({str(tmp_path / 'mailbox')!r})
server._auto_register_from_env = lambda: None
server.main()
'''
    env = {k: v for k, v in os.environ.items() if k not in
           ('TERM_SESSION_ID', 'ITERM_SESSION_ID', 'TEAMMATE_LABEL', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'TEAMMATE_LEGACY_PANE_INPUT')}
    env['TEAMMATE_LOG_FILE'] = '0'
    proc = await asyncio.create_subprocess_exec(sys.executable, '-c', program, cwd=root, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    events = []
    async def receive():
        line = await asyncio.wait_for(proc.stdout.readline(), 10)
        assert line, 'MCP process exited'
        return json.loads(line)
    async def rpc(number, method, params):
        proc.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params}) + '\n').encode())
        await proc.stdin.drain()
        while True:
            message = await receive()
            if message.get('id') == number:
                assert 'error' not in message, message
                return message['result']
            events.append(message)
    async def event(kind):
        while True:
            for i, item in enumerate(events):
                if item.get('method') == 'notifications/claude/channel' and item['params']['meta']['kind'] == kind:
                    return events.pop(i)
            events.append(await receive())
    try:
        initialized = await rpc(1, 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
            'clientInfo': {'name': 'claude-code-wire-test', 'version': 'test'}})
        assert 'claude/channel' in initialized['capabilities']['experimental']
        proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'); await proc.stdin.drain()
        await rpc(2, 'tools/list', {})
        handshake = await event('handshake')
        nonce = re.search(r"nonce='([^']+)'", handshake['params']['content']).group(1)
        ready = await rpc(3, 'tools/call', {'name': 'channel_ready', 'arguments': {'nonce': nonce}})
        status = json.loads(ready['content'][0]['text'])
        assert status['ready'] is True
        connection = await rpc(30, 'tools/call', {'name': 'connection_status', 'arguments': {}})
        connection = json.loads(connection['content'][0]['text'])
        assert connection['auto_delivery'] is True and connection['policy'] == 'native-queue'
        await rpc(4, 'tools/call', {'name': 'ask', 'arguments': {'target': status['label'], 'question': 'WIRE-TEST-MESSAGE'}})
        question = await event('question')
        assert 'WIRE-TEST-MESSAGE' in question['params']['content']
        job_id = question['params']['meta']['job_id']
        await rpc(5, 'tools/call', {'name': 'mark_processed', 'arguments': {'job_id': job_id}})
        assert not list((tmp_path / 'mailbox' / status['label'] / 'inbox').glob('*.json'))
    finally:
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except TimeoutError:
            proc.kill(); await proc.wait()
