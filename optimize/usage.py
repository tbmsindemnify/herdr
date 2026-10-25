"""Provider-reported usage tied to the foreground process of each Herdr pane."""
import json
import os
from pathlib import Path
import sys
from tile import API


def _codex_root_path(processes):
    """Resolve only an unambiguous process-owned CLI root, never a subagent."""
    roots = set()
    for proc in processes:
        if proc.get('name') != 'codex':
            continue
        pid = proc.get('pid')
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            continue
        try:
            descriptors = list(Path(f'/proc/{pid}/fd').iterdir())
        except OSError:
            continue
        for fd in descriptors:
            try:
                path = Path(os.readlink(fd))
            except OSError:
                continue
            if not path.name.startswith('rollout-') or path.suffix != '.jsonl':
                continue
            try:
                # A bounded metadata read avoids scanning any conversation content.
                with path.open('rb') as file:
                    event = json.loads(file.readline(262_144))
                meta = event.get('payload') or {}
            except (OSError, ValueError, AttributeError):
                return None
            if (event.get('type') == 'session_meta' and isinstance(meta, dict)
                    and meta.get('source') == 'cli' and not meta.get('parent_thread_id')
                    and isinstance(meta.get('id'), str) and meta['id']):
                roots.add(path)
    return next(iter(roots)) if len(roots) == 1 else None


def session_path(api, pane):
    info = api.call('pane.process_info', pane_id=pane['pane_id'])['process_info']
    if pane.get('agent') == 'codex':
        return _codex_root_path(info.get('foreground_processes', []))
    for proc in info.get('foreground_processes', []):
        pid = proc['pid']
        if pane.get('agent') == 'claude' and proc['name'] == 'claude':
            meta = json.loads((Path.home()/'.claude/sessions'/f'{pid}.json').read_text())
            directory = meta['cwd'].replace('/', '-').replace('.', '-')
            path = Path.home()/'.claude/projects'/directory/(meta['sessionId']+'.jsonl')
            if path.exists(): return path
        if pane.get('agent') == 'agy' and proc['name'] == 'agy':
            for fd in Path(f'/proc/{pid}/fd').iterdir():
                try: path = Path(os.readlink(fd))
                except OSError: continue
                if '/conversations/' in str(path) and path.suffix == '.db':
                    tpath = Path.home()/f'.gemini/antigravity-cli/brain/{path.stem}/.system_generated/logs/transcript.jsonl'
                    if tpath.exists(): return tpath
    return None


def read_usage(path, agent):
    # Bound I/O; report last-request values, never call a partial tail a session total.
    with path.open('rb') as file:
        size = path.stat().st_size
        file.seek(max(0, size - 2_000_000))
        if file.tell(): file.readline()
        rows = file.read().splitlines()
    last = None
    for row in rows:
        try: event = json.loads(row)
        except ValueError: continue
        if agent == 'claude':
            usage = event.get('message', {}).get('usage')
            if not usage: continue
            cached = usage.get('cache_read_input_tokens', 0)
            inputs = usage.get('input_tokens', 0) + cached + usage.get('cache_creation_input_tokens', 0)
            last = dict(input=inputs, cached=cached, output=usage.get('output_tokens', 0), time=event.get('timestamp', ''))
        elif agent == 'agy':
            if 'input_tokens' not in event: continue
            cached = event.get('cache_read_tokens', 0)
            inputs = event.get('input_tokens', 0) + cached
            last = dict(input=inputs, cached=cached, output=event.get('output_tokens', 0), time=event.get('created_at', ''))
        else:
            payload = event.get('payload', {})
            info = payload.get('info') or {}
            usage = info.get('last_token_usage')
            if payload.get('type') != 'token_count' or not usage: continue
            window = info.get('model_context_window')
            if type(window) is not int or window <= 0:
                window = None
            last = dict(input=usage.get('input_tokens', 0), cached=usage.get('cached_input_tokens', 0),
                        output=usage.get('output_tokens', 0), time=event.get('timestamp', ''),
                        session_total=(info.get('total_token_usage') or {}).get('total_tokens'),
                        model_context_window=window)
    return last


def label(usage):
    if not usage: return 'Tokens / cache: unknown'
    n = lambda value: f'{value/1000:.1f}k' if value >= 1000 else str(value)
    rate = round(100 * usage['cached'] / usage['input'], 1) if usage['input'] else 0
    total = usage['input'] + usage['output']
    result = f"Last: {n(total)} tokens · in {n(usage['input'])} / out {n(usage['output'])} · cache {rate}% ({n(usage['cached'])})"
    if usage.get('session_total') is not None: result += f" · session {n(usage['session_total'])}"
    return result + ' · ' + usage['time'][11:19] + ' UTC'


def collect(api):
    result = {}
    for pane in api.call('pane.list')['panes']:
        try:
            path = session_path(api, pane) if pane.get('agent') in ('claude', 'codex', 'agy') else None
            result[pane['pane_id']] = label(read_usage(path, pane['agent']) if path else None)
        except (OSError, ValueError, KeyError, RuntimeError):
            result[pane['pane_id']] = 'Tokens / cache: unavailable'
    return result


if __name__ == '__main__':
    print(json.dumps(collect(API(sys.argv[1]))))
