#!/usr/bin/env python3
"""Rearrange live Herdr panes without restarting their terminals."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess


def active_socket():
    window = json.loads(subprocess.check_output(['hyprctl', '-j', 'activewindow']))
    pid = window.get('pid')
    if not pid:
        raise RuntimeError('No active Herdr window')
    children = Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
    for child in children:
        if Path(f'/proc/{child}/comm').read_text().strip() != 'herdr':
            continue
        args = Path(f'/proc/{child}/cmdline').read_bytes().decode().split('\0')
        env = dict(v.split('=', 1) for v in Path(f'/proc/{child}/environ').read_bytes().decode().split('\0') if '=' in v)
        if '--remote' in args or '--machine' in args:
            raise RuntimeError('Remote pane rearrangement is not configured')
        base = Path.home() / '.config' / ('herdr-dev' if '/debug/' in args[0] else 'herdr')
        if '--session' in args:
            return base / 'sessions' / args[args.index('--session') + 1] / 'herdr.sock'
        return Path(env.get('HERDR_SOCKET_PATH', str(base / 'herdr.sock')))
    raise RuntimeError('The active window is not a Herdr client')


class API:
    def __init__(self, path):
        self.path = str(path)

    def call(self, method, **params):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(4)
            sock.connect(self.path)
            sock.sendall((json.dumps({'id': 'tbm-tiling', 'method': method, 'params': params}) + '\n').encode())
            data = json.loads(sock.makefile('rb').readline())
        if 'error' in data:
            raise RuntimeError(data['error']['message'])
        return data['result']


def pick_target(panes, source, direction):
    rect = source['rect']
    sx, sy = rect['x'] + rect['width'] / 2, rect['y'] + rect['height'] / 2
    def score(p):
        r = p['rect']
        dx = r['x'] + r['width'] / 2 - sx
        dy = (r['y'] + r['height'] / 2 - sy) * 2
        forward = {'l': -dx, 'r': dx, 'u': -dy, 'd': dy}[direction]
        # Prefer the requested side; at an edge use the nearest existing tile.
        return (forward <= 0, dx * dx + dy * dy)
    return min((p for p in panes if p['pane_id'] != source['pane_id']), key=score)


def rearrange(api, direction):
    snapshot = api.call('session.snapshot')['snapshot']
    layout = next(l for l in snapshot['layouts'] if l['tab_id'] == snapshot['focused_tab_id'])
    if len(layout['panes']) < 2:
        return {'changed': False, 'reason': 'Only one pane'}
    if layout.get('zoomed'):
        raise RuntimeError('Unzoom the pane before rearranging it')
    source = next(p for p in layout['panes'] if p['pane_id'] == snapshot['focused_pane_id'])
    target = pick_target(layout['panes'], source, direction)
    before = {p['terminal_id'] for p in snapshot['panes']}
    # 0.9.1 deliberately treats same-tab pane.move as a no-op. A temporary
    # tab lets the public API transfer the existing PTY back into a new split.
    moved = api.call('pane.move', pane_id=source['pane_id'], destination={
        'type': 'new_tab', 'workspace_id': layout['workspace_id'], 'label': 'Rearranging'}, focus=False)['move_result']
    if not moved['changed']:
        raise RuntimeError('The pane could not be rearranged')
    moved = api.call('pane.move', pane_id=moved['pane']['pane_id'], destination={
        'type': 'tab', 'tab_id': layout['tab_id'], 'target_pane_id': target['pane_id'],
        'split': 'down' if direction in 'ud' else 'right', 'ratio': .5}, focus=True)['move_result']
    if not moved['changed']:
        raise RuntimeError('The live pane is retained in the Rearranging tab; move it back before retrying')
    pane_id = moved['pane']['pane_id']
    if direction in 'ul':
        api.call('pane.swap', source_pane_id=pane_id, target_pane_id=target['pane_id'])
    api.call('pane.focus', pane_id=pane_id)
    after = api.call('session.snapshot')['snapshot']
    if before != {p['terminal_id'] for p in after['panes']}:
        raise RuntimeError('Terminal inventory changed during rearrangement; inspect the workspace')
    return {'changed': True, 'pane_id': pane_id, 'direction': direction, 'preserved_terminals': len(before)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('direction', choices=['l', 'r', 'u', 'd'])
    parser.add_argument('--socket', type=Path)
    args = parser.parse_args()
    path = args.socket or active_socket()
    with (path.parent / 'tbm-tiling.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        print(json.dumps(rearrange(API(path), args.direction)))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        subprocess.run(['notify-send', 'Herdr tiling', str(error)], check=False)
        raise
