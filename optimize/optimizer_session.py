"""Conservative PTY transactions for a single existing Herdr agent session.

Public API: capture(api, pane_id), locked(identity), current(api, identity),
clear_and_verify(...), insert_and_verify(...), compact(...).  read_draft is a
callable taking detection-screen text, returning exact editor text or None.
Callers must save the original draft before clearing it. No operation retries
an input write or restores text over intervening user input.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time

LOCK_ROOT = Path.home() / '.local/share/herdr-harness/workspace-optimization/locks'


class SessionError(RuntimeError):
    """A transaction stopped; delivered means input may already be in the PTY."""
    def __init__(self, message, *, delivered=False):
        super().__init__(message)
        self.delivered = delivered


@dataclass(frozen=True)
class Identity:
    socket_path: str
    socket_device: int
    socket_inode: int
    terminal_id: str
    agent: str
    processes: tuple
    transcript: str | None

    @property
    def key(self):
        encoded = json.dumps(self.as_dict(), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()[:32]

    def as_dict(self):
        return asdict(self)


def _process_start(pid):
    # /proc stat field 22 prevents accidental reuse of a recycled PID. The
    # command field may contain spaces or parentheses; split after its last ).
    stat = Path(f'/proc/{int(pid)}/stat').read_text()
    return stat.rsplit(')', 1)[1].split()[19]


def _processes(api, pane):
    rows = api.call('pane.process_info', pane_id=pane['pane_id'])['process_info'].get('foreground_processes', [])
    matching = [p for p in rows if p.get('name') == pane.get('agent')]
    if pane.get('agent') in ('claude', 'codex') and not matching:
        raise SessionError('The foreground provider process cannot be verified. Input retained.')
    selected = matching or rows
    if not selected:
        raise SessionError('The foreground process cannot be verified. Input retained.')
    return tuple(sorted((int(p['pid']), str(p.get('name', '')), _process_start(p['pid'])) for p in selected))


def _transcript(api, pane):
    if pane.get('agent') not in ('claude', 'codex', 'agy'):
        return None
    import usage
    try:
        path = usage.session_path(api, pane)
        return str(path.resolve()) if path else None
    except (OSError, ValueError, KeyError, RuntimeError):
        return None


def _socket(api):
    path = Path(api.path).resolve(strict=True)
    stat = path.stat()
    return str(path), stat.st_dev, stat.st_ino


def _idle(pane):
    if pane.get('agent_status') not in ('idle', 'done'):
        raise SessionError('The target agent is no longer idle. Input left untouched; saved drafts remain available.')


def capture(api, pane_id):
    pane = api.call('pane.get', pane_id=pane_id)['pane']
    _idle(pane)
    if not pane.get('agent') or not re.fullmatch(r'[A-Za-z0-9_-]+', pane['terminal_id']):
        raise SessionError('The target agent identity cannot be verified.')
    path, device, inode = _socket(api)
    return Identity(path, device, inode, pane['terminal_id'], pane['agent'],
                    _processes(api, pane), _transcript(api, pane))


@contextmanager
def locked(identity):
    """Reject concurrent jobs, including jobs from another Herdr client."""
    LOCK_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The lock binds the terminal/server, not the PID: session replacement must
    # not permit a second transaction while the first is winding down.
    scope = f'{identity.socket_path}:{identity.socket_device}:{identity.socket_inode}:{identity.terminal_id}'
    key = hashlib.sha256(scope.encode()).hexdigest()
    fd = os.open(LOCK_ROOT / (key + '.lock'), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SessionError('Optimize is already running for this terminal.') from exc
        yield
    finally:
        os.close(fd)


def current(api, identity, *, require_idle=True):
    if _socket(api) != (identity.socket_path, identity.socket_device, identity.socket_inode):
        raise SessionError('The Herdr server changed. Saved drafts retained; no further input sent.')
    matches = [p for p in api.call('pane.list')['panes'] if p['terminal_id'] == identity.terminal_id]
    if len(matches) != 1:
        raise SessionError('The original terminal is no longer uniquely available. Saved drafts retained.')
    pane = matches[0]
    if pane.get('agent') != identity.agent or _processes(api, pane) != identity.processes:
        raise SessionError('The provider process changed. Saved drafts retained; no further input sent.')
    if _transcript(api, pane) != identity.transcript:
        raise SessionError('The provider conversation changed. Saved drafts retained; no further input sent.')
    if require_idle:
        _idle(pane)
    return pane


def _draft(api, pane, read_draft):
    screen = api.call('pane.read', pane_id=pane['pane_id'], source='detection', format='text')['read']['text']
    return read_draft(screen)


def _send(api, **params):
    try:
        return api.call('pane.send_input', **params)
    except Exception as exc:
        # A lost response is not proof of non-delivery. Never retry the write.
        raise SessionError('Input delivery could not be confirmed. Saved drafts retained; do not retry automatically.', delivered=True) from exc


def _after_write_current(api, identity, *, require_idle=True):
    try:
        return current(api, identity, require_idle=require_idle)
    except Exception as exc:
        raise SessionError('The target changed or became unavailable after input delivery. Saved drafts retained; no further input sent.', delivered=True) from exc


def clear_and_verify(api, identity, expected_draft, read_draft, *, timeout=2.0):
    pane = current(api, identity)
    if _draft(api, pane, read_draft) != expected_draft:
        raise SessionError('The draft changed while optimizing. Saved result retained; input untouched.')
    if expected_draft == '':
        return {'verified': True, 'changed': False}
    _send(api, pane_id=pane['pane_id'], keys=['ctrl+e', 'ctrl+u'])
    deadline = time.monotonic() + timeout
    while True:
        pane = _after_write_current(api, identity)
        try:
            draft = _draft(api, pane, read_draft)
        except Exception as exc:
            raise SessionError('Editor readback failed after clearing. No replacement sent; original draft remains saved.', delivered=True) from exc
        if draft == '':
            return {'verified': True, 'changed': True}
        if draft != expected_draft or time.monotonic() >= deadline:
            raise SessionError('Could not verify an empty editor after clearing. No replacement sent; original draft remains saved.', delivered=True)
        time.sleep(0.05)


def _safe_text(text):
    if not isinstance(text, str) or any(ord(c) < 32 and c not in '\n\t' or 127 <= ord(c) < 160 for c in text):
        raise SessionError('Prepared text contains unsupported terminal control characters. Input retained.')
    return text


def insert_and_verify(api, identity, text, read_draft, *, timeout=2.0):
    text = _safe_text(text)
    pane = current(api, identity)
    if _draft(api, pane, read_draft) != '':
        raise SessionError('The editor is no longer empty. Saved result retained; input untouched.')
    _send(api, pane_id=pane['pane_id'], text=text, keys=[])
    deadline = time.monotonic() + timeout
    while True:
        pane = _after_write_current(api, identity)
        try:
            draft = _draft(api, pane, read_draft)
        except Exception as exc:
            raise SessionError('Prepared text was delivered without Enter, but the editor collapsed or wrapped it; exact readback is unverified. Do not retry automatically.', delivered=True) from exc
        if draft == text:
            return {'verified': True, 'submitted': False}
        if draft not in ('', None) or time.monotonic() >= deadline:
            raise SessionError('Prepared text was delivered without Enter; exact editor readback is unverified. Saved original and result remain available; do not retry automatically.', delivered=True)
        time.sleep(0.05)


def clean_focus(focus):
    # Remove C0/C1 controls, collapse whitespace, and bound the native command.
    return ' '.join(''.join(c if ord(c) >= 32 and not 127 <= ord(c) < 160 else ' ' for c in str(focus)).split())[:400]


def _completion(event, provider):
    if not isinstance(event, dict):
        return None
    if provider == 'claude' and event.get('type') == 'system' and event.get('subtype') == 'compact_boundary':
        return 'compact_boundary'
    if provider == 'codex' and event.get('type') == 'compacted' and isinstance(event.get('payload'), dict):
        return 'compacted'
    return None


def compact(api, identity, focus, read_draft, *, timeout=90.0, poll_interval=0.2):
    """Submit only a native compact command; verify a NEW transcript boundary.

    A boundary is accepted only together with an idle agent and an empty editor.
    Neither old screen text, an enqueue acknowledgment, nor token estimates are
    completion evidence. Failures never submit the saved task or retry compact.
    """
    if identity.agent not in ('claude', 'codex') or not identity.transcript:
        raise SessionError('Verified native compaction is unavailable for this provider/session.')
    pane = current(api, identity)
    if _draft(api, pane, read_draft) != '':
        raise SessionError('Compaction requires a verified empty editor. Nothing submitted.')
    transcript = Path(identity.transcript)
    before = transcript.stat()
    offset = before.st_size
    start_offset = offset
    pending = b''
    marker = None
    # Recheck after reading the checkpoint, immediately before submitting.
    pane = current(api, identity)
    if _draft(api, pane, read_draft) != '':
        raise SessionError('The draft changed before compaction. Nothing submitted.')
    command = '/compact'
    safe_focus = clean_focus(focus) if identity.agent == 'claude' else ''
    if safe_focus:
        command += ' ' + safe_focus
    _send(api, pane_id=pane['pane_id'], text=command, keys=['enter'])
    deadline = time.monotonic() + timeout
    while True:
        pane = _after_write_current(api, identity, require_idle=False)
        try:
            stat = transcript.stat()
        except OSError as exc:
            raise SessionError('The transcript became unavailable; compaction completion is unverified. Saved draft retained.', delivered=True) from exc
        if (stat.st_dev, stat.st_ino) != (before.st_dev, before.st_ino) or stat.st_size < offset:
            raise SessionError('The transcript rotated during compaction; completion is unverified. Saved draft retained.', delivered=True)
        if stat.st_size - start_offset > 4_000_000:
            raise SessionError('Compaction output exceeded the verification budget; saved draft retained.', delivered=True)
        try:
            with transcript.open('rb') as stream:
                stream.seek(offset)
                chunk = stream.read(4_000_001)
        except OSError as exc:
            raise SessionError('The transcript became unavailable; compaction completion is unverified. Saved draft retained.', delivered=True) from exc
        offset += len(chunk)
        pending += chunk
        while b'\n' in pending:
            line, pending = pending.split(b'\n', 1)
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            marker = _completion(event, identity.agent) or marker
        if marker and pane.get('agent_status') in ('idle', 'done'):
            try:
                empty = _draft(api, pane, read_draft) == ''
            except Exception as exc:
                raise SessionError('Compaction boundary recorded, but editor state cannot be verified. Saved draft retained.', delivered=True) from exc
            if empty:
                return {'verified': True, 'provider': identity.agent, 'event': marker,
                        'start_offset': start_offset, 'end_offset': offset, 'task_submitted': False}
        if time.monotonic() >= deadline:
            raise SessionError('Native compaction was requested but completion was not verified before timeout. Saved original/result retained; no task submitted and no automatic retry.', delivered=True)
        time.sleep(poll_interval)
