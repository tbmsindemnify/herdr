"""JSON-only settings adapter for Herdr's native client. No GUI imports."""
import json
import re
import shlex
import sys
import tomllib
from pathlib import Path
import settings as existing
from local_models import installed_models, model_profile
from tile import API

DRAFT = existing.STATE / 'change-draft.txt'

def model_catalog():
    sys.path.insert(0,str(Path.home()/'.local/lib/qwen-chat'))
    from catalog import catalog
    return catalog(installed_models(),24*1024**3)


def row(key, label, value=None):
    return dict(id=key, label=label, value=value or '', editable=value is not None)


def keybindings():
    defaults = existing.subprocess.check_output(['/usr/bin/herdr', '--default-config'], text=True)
    block = re.search(r'^\[keys\]\s*\n(.*?)(?=^\[|\Z)', defaults, re.M | re.S)
    keys = {}
    for line in (block.group(1) if block else '').splitlines():
        try: keys.update(tomllib.loads(re.sub(r'^#\s?', '', line)))
        except tomllib.TOMLDecodeError: pass
    keys.update(tomllib.loads(existing.CONFIG.read_text()).get('keys', {}))
    return keys


def rows(section, api):
    if section == 'opacity':
        match = re.search(r'^alpha\s*=\s*([\d.]+)', existing.FOOT.read_text(), re.M)
        return [row('alpha', 'Terminal opacity (0.05–1; applies when terminal reopens)', match.group(1) if match else '1')]
    if section == 'keys':
        return [row(key, key, str(value)) for key, value in sorted(keybindings().items())]
    if section == 'agents':
        result = []
        for i, profile in enumerate(existing.agent_profiles()):
            result.append(row(f'launch:{i}', 'Launch ' + profile['name'] + ' in a new pane'))
            for field in ['name', 'command', 'logo']:
                result.append(row(f'{i}:{field}', profile['name'] + ' · ' + field, profile.get(field, '')))
        result.append(row('add', 'Add agent (name | command | optional logo)', ''))
        return result
    if section == 'models':
        return [row(m['id'],m['label']+' · '+m['status']) for m in model_catalog()]
    if section in ('skills', 'plugins', 'media'):
        return [row(str(i), item['name'] + ' · ' + item['description'][:100]) for i, item in enumerate(existing.catalog()[section])]
    if section == 'request':
        return [row('draft', 'Change request draft', DRAFT.read_text() if DRAFT.exists() else '')] + [
            row('send:' + p['terminal_id'], 'Send draft to ' + (p.get('label') or p['agent']) + ' · ' + p['pane_id'])
            for p in api.call('pane.list')['panes'] if p.get('agent')]
    return []


def apply(section, key, value, api):
    if section == 'opacity':
        alpha = float(value)
        if not .05 <= alpha <= 1: raise ValueError('Opacity must be between 0.05 and 1.')
        text = existing.FOOT.read_text()
        if not re.search(r'^alpha\s*=', text, re.M): raise ValueError('No alpha setting found in terminal configuration.')
        existing.backup_write(existing.FOOT, re.sub(r'^alpha\s*=.*$', f'alpha={alpha:.2f}', text, flags=re.M))
        return 'Saved. Terminal opacity applies when its window is reopened.'
    if section == 'keys':
        if key not in keybindings(): raise ValueError('Unknown shortcut.')
        text = existing.CONFIG.read_text()
        keys = tomllib.loads(text).get('keys', {})
        keys[key] = value
        updated = existing.replace_section(text, 'keys', '\n'.join(f'{k} = {json.dumps(v)}' for k, v in keys.items()))
        tomllib.loads(updated); existing.backup_write(existing.CONFIG, updated)
        api.call('server.reload_config')
        return 'Shortcut saved. Herdr reports invalid bindings in its configuration warning.'
    if section == 'agents':
        profiles = existing.agent_profiles()
        if key.startswith('launch:'):
            existing.launch_agent(api, profiles[int(key.split(':')[1])]); return 'Agent opened in a new pane.'
        if key == 'add':
            parts = value.split('|', 2)
            if len(parts) < 2 or not parts[0].strip() or not shlex.split(parts[1]): raise ValueError('Enter name | command | optional logo.')
            profile = dict(name=parts[0].strip(), command=parts[1].strip(), logo=parts[2].strip() if len(parts)>2 else '')
            profiles.append(profile)
        else:
            index, field = key.split(':'); profile = profiles[int(index)]
            if field not in ('name', 'command', 'logo'): raise ValueError('Unknown agent field.')
            if field == 'name' and not value.strip(): raise ValueError('Enter an agent name.')
            if field == 'command' and not shlex.split(value): raise ValueError('Enter an agent command.')
            profile[field] = value
        if profile['logo'] and not Path(profile['logo']).is_file(): raise ValueError('Logo file does not exist.')
        existing.save_agents(profiles); return 'Agent profile saved.'
    if section == 'models':
        entry=next((m for m in model_catalog() if m['id']==key),None)
        if not entry: raise ValueError('Model is no longer in the catalog. Refresh the menu.')
        if entry['kind'] in ('codex','claude'):
            command=[entry['kind']]+(['--model',entry['model']] if entry['model']!='default' else [])
        elif entry['kind'] in ('chat','bank') and entry['status']=='ready':
            command=[str(Path.home()/'.local/bin/qwen-chat'),'--model',key]
        else: return entry['description']
        existing.launch_agent(api,{'name':entry['label'],'command':shlex.join(command)})
        return 'Opened in a pane. Availability is checked by its runtime; no prompt was sent.'
    if section in ('skills', 'plugins', 'media'):
        item = existing.catalog()[section][int(key)]
        text = 'Use ' + item['name'] + '. ' + ('Read ' + item['path'] + ' first. ' if item.get('path') else 'Verify provider access before use. ')
        existing.STATE.mkdir(parents=True, exist_ok=True)
        DRAFT.write_text((DRAFT.read_text() if DRAFT.exists() else '') + text)
        return 'Added to Request draft. Review it there before sending. ' + item['availability']
    if section == 'request':
        if key == 'draft':
            existing.STATE.mkdir(parents=True, exist_ok=True); DRAFT.write_text(value)
            return 'Draft saved. Select a recipient and Apply to send.'
        draft = DRAFT.read_text().strip() if DRAFT.exists() else ''
        if not draft: raise ValueError('Write a request draft first.')
        target = next(p for p in api.call('pane.list')['panes'] if p['terminal_id'] == key.removeprefix('send:') and p.get('agent'))
        api.call('pane.send_input', pane_id=target['pane_id'], text='Requested app change: ' + draft, keys=['Enter'])
        return 'Request sent to the selected agent.'
    raise ValueError('Unknown settings section.')


if __name__ == '__main__':
    request = json.loads(sys.argv[1]); api = API(sys.argv[2])
    try:
        if request['section'] == 'optimize':
            import workspace_optimize
            pane = request.get('pane')
            if not pane: raise ValueError('Open Optimize from a chat pane.')
            message = workspace_optimize.apply(api, pane, request['id'], request.get('value', '')) if request.get('id') is not None else ''
            print(json.dumps(dict(rows=workspace_optimize.entries(api, pane), message=message)))
            sys.exit(0)
        message = apply(request['section'], request['id'], request.get('value', ''), api) if request.get('id') is not None else ''
        print(json.dumps(dict(rows=rows(request['section'], api), message=message)))
    except Exception as exc:
        print(json.dumps(dict(error=str(exc))))
