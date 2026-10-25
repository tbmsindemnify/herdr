"""Read-only provider discovery and pane-owned, pending-launch optimization profiles.

No function in this module writes provider settings, installs extensions, or launches
an agent. Configured availability is deliberately separate from live session state.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import tomllib

REVIEW_DATE = '2026-10-24'
COMPACT_FOCUS = ('Preserve the active objective, user corrections and constraints, decisions, '
                 'changed file paths, unresolved issues, verification results and next steps. '
                 'Summarize redundant exploration and tool output.')
POLICIES = {
    'claude': {
        'provider': 'claude', 'review_date': REVIEW_DATE,
        'sources': [
            'https://code.claude.com/docs/en/best-practices',
            'https://code.claude.com/docs/en/costs',
            'https://code.claude.com/docs/en/skills',
            'https://code.claude.com/docs/en/settings',
            'https://code.claude.com/docs/en/plugins/cli-reference',
            'https://claude.com/resources/articles/building-agents-when-to-fold-subagents-into-your-main-agent-loop',
        ],
        'compact': {'supported': True, 'command': '/compact', 'supports_focus': True, 'focus': COMPACT_FOCUS},
        'guidance': ('State the goal, relevant context, constraints and a concrete verification check. '
                     'Keep instructions concise and consistent. Load relevant skills progressively; '
                     'use deferred tool search and keep needed plugins. Use independent subagents only '
                     'where context isolation or parallel work helps. Compact ongoing work with a '
                     'preservation hint; do not clear its history. Measure task completion, not just token savings.'),
    },
    'codex': {
        'provider': 'codex', 'review_date': REVIEW_DATE,
        'sources': [
            'https://learn.chatgpt.com/docs/learn/best-practices',
            'https://learn.chatgpt.com/docs/prompting',
            'https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra',
            'https://learn.chatgpt.com/docs/build-skills',
            'https://developers.openai.com/codex/plugins',
            'https://developers.openai.com/codex/config-reference',
            'https://developers.openai.com/codex/cli/slash-commands',
        ],
        'compact': {'supported': True, 'command': '/compact', 'supports_focus': False, 'focus': ''},
        'guidance': ('Describe the desired outcome, relevant repository context, constraints and acceptance checks. '
                     'Preserve user intent and active work. Keep the prompt direct; avoid needless scaffolding '
                     'and demands for hidden chain of thought. Discover tools and skills progressively, '
                     'retain explicitly requested capabilities, and verify the real output. Use native '
                     '/compact for the current conversation; configuration overrides require a managed launch or resume.'),
    },
}


def policy(provider: str) -> dict:
    key = str(provider).lower()
    if key not in POLICIES:
        raise ValueError(f'No reviewed optimization policy for provider: {provider}')
    return copy.deepcopy(POLICIES[key])


def guidance(provider: str) -> str:
    return policy(provider)['guidance']


def _read(path: Path, warnings: list[str]) -> dict:
    if not path.is_file():
        return {}
    try:
        if path.stat().st_size > 8_000_000:
            raise ValueError('file exceeds inventory size limit')
        data = tomllib.loads(path.read_text()) if path.suffix == '.toml' else json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError('expected an object')
        return data
    except (OSError, ValueError) as exc:
        # Never echo content or parser excerpts: provider files may contain secrets.
        warnings.append(f'Could not read {path.name}: {type(exc).__name__}')
        return {}


def _skill_files(root: Path):
    pending, seen = [(root, 0)], set()
    while pending:
        folder, depth = pending.pop()
        try:
            resolved = folder.resolve()
            if resolved in seen or not folder.is_dir():
                continue
            seen.add(resolved)
            skill = folder / 'SKILL.md'
            if skill.is_file():
                yield skill
                continue
            if depth < 5:
                pending.extend((p, depth + 1) for p in sorted(folder.iterdir())
                               if p.is_dir() and (not p.name.startswith('.') or p.name == '.system'))
        except OSError:
            continue


def _skill(path: Path, provider: str, plugin: dict | None = None) -> dict | None:
    try:
        text = path.read_text(errors='replace')[:8192]
    except OSError:
        return None
    name, description = path.parent.name, ''
    if text.startswith('---'):
        front = text.split('---', 2)[1]
        for key, value in re.findall(r'^(name|description):\s*(.*)$', front, flags=re.M):
            value = value.strip().strip('"\'')
            if value not in ('>', '|', ''):
                if key == 'name':
                    name = value
                else:
                    description = value[:700]
    if plugin:
        name = f"{plugin['name']}:{name}"
    return {'name': name, 'path': str(path.resolve()), 'description': description,
            'provider': provider, 'plugin_id': plugin['id'] if plugin else None,
            'enabled': plugin['enabled'] if plugin else True,
            'availability': 'configured; live session not inspected'}


def _version_key(path: Path):
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in re.split(r'(\d+)', path.name))


def _manifest(directory: Path, warnings: list[str]) -> dict:
    for part in ('.codex-plugin', '.claude-plugin'):
        path = directory / part / 'plugin.json'
        if path.is_file():
            return _read(path, warnings)
    return {}


def _claude_plugins(home: Path, cwd: Path | None, settings: dict, warnings: list[str]) -> list[dict]:
    registry = _read(home / '.claude/plugins/installed_plugins.json', warnings).get('plugins', {})
    if not isinstance(registry, dict):
        warnings.append('Claude plugin registry has unsupported shape')
        return []
    enabled = settings.get('enabledPlugins', {})
    result = []
    for plugin_id, entries in sorted(registry.items()):
        if not isinstance(entries, list):
            continue
        valid = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('installPath'), str):
                continue
            project = entry.get('projectPath')
            if entry.get('scope') in ('project', 'local') and (not cwd or not project or Path(project).resolve() != cwd):
                continue
            directory = Path(entry['installPath']).expanduser()
            if directory.is_dir():
                valid.append(entry)
        if not valid:
            continue
        entry = max(valid, key=lambda x: ({'user': 0, 'project': 1, 'local': 2}.get(x.get('scope'), -1), x.get('lastUpdated', '')))
        directory = Path(entry['installPath']).expanduser().resolve()
        manifest = _manifest(directory, warnings)
        name = manifest.get('name') or plugin_id.split('@')[0]
        result.append({'id': plugin_id, 'name': str(name), 'enabled': enabled.get(plugin_id) if isinstance(enabled.get(plugin_id), bool) else None,
                       'path': str(directory), 'installed_path': str(directory), 'installed': True,
                       'scope': entry.get('scope'), 'description': str(manifest.get('description', ''))[:700]})
    return result


def _codex_plugins(home: Path, settings: dict, warnings: list[str]) -> list[dict]:
    configured = settings.get('plugins', {})
    cache = home / '.codex/plugins/cache'
    result = []
    if not cache.is_dir():
        return result
    # Cache layout and manifest/installation markers are local installation evidence,
    # not proof the running TUI has advertised these plugins or authenticated tools.
    for marketplace in sorted(cache.iterdir()):
        if not marketplace.is_dir() or marketplace.name.startswith('.'):
            continue
        for package in sorted(marketplace.iterdir()):
            if not package.is_dir() or package.name.startswith('.'):
                continue
            plugin_id = f'{package.name}@{marketplace.name}'
            versions = [p for p in package.iterdir() if p.is_dir() and not p.name.startswith('.')]
            candidates = [package, *sorted(versions, key=_version_key, reverse=True)]
            directory = next((p for p in candidates if (p / '.codex-plugin/plugin.json').is_file()
                              or (p / '.claude-plugin/plugin.json').is_file()), None)
            if directory is None:
                continue
            manifest = _manifest(directory, warnings)
            config = configured.get(plugin_id, {}) if isinstance(configured, dict) else {}
            enabled = config.get('enabled') if isinstance(config, dict) else None
            enabled = enabled if isinstance(enabled, bool) else None
            result.append({'id': plugin_id, 'name': str(manifest.get('name') or package.name),
                           'enabled': enabled, 'path': str(directory.resolve()),
                           'installed_path': str(directory.resolve()), 'installed': True,
                           'scope': 'cache', 'description': str(manifest.get('description', ''))[:700]})
    return result


def inventory(provider: str, cwd=None, *, home=None) -> dict:
    provider = policy(provider)['provider']
    home = Path(home).expanduser().resolve() if home else Path.home()
    cwd = Path(cwd).expanduser().resolve() if cwd else None
    warnings, settings = [], {}
    config_paths = [home / f'.{provider}/' / ('settings.json' if provider == 'claude' else 'config.toml')]
    if cwd:
        config_paths += ([cwd / '.claude/settings.json', cwd / '.claude/settings.local.json'] if provider == 'claude'
                         else [cwd / '.codex/config.toml'])
    # Read only extension-related fields into the returned state; never credentials,
    # environment, permission bypass settings, MCP headers, or unrelated configuration.
    for path in config_paths:
        data = _read(path, warnings)
        for key in ('enabledPlugins', 'skillOverrides', 'plugins', 'skills'):
            value = data.get(key)
            if isinstance(value, dict):
                settings.setdefault(key, {}).update(value)
    plugins = (_claude_plugins(home, cwd, settings, warnings) if provider == 'claude'
               else _codex_plugins(home, settings, warnings))
    roots = [home / '.claude/skills'] if provider == 'claude' else [home / '.agents/skills', home / '.codex/skills']
    if cwd:
        # Ancestor skill discovery, bounded at the repository root when present.
        for ancestor in [cwd, *cwd.parents]:
            roots.append(ancestor / ('.claude/skills' if provider == 'claude' else '.agents/skills'))
            if (ancestor / '.git').exists() or ancestor == home:
                break
    skills, seen = [], set()
    for root in roots:
        for path in _skill_files(root):
            item = _skill(path, provider)
            if item and item['path'] not in seen:
                seen.add(item['path']); skills.append(item)
    for plugin in plugins:
        root = Path(plugin['path']) / 'skills'
        for path in _skill_files(root):
            item = _skill(path, provider, plugin)
            if item and (item['path'], item['plugin_id']) not in seen:
                seen.add((item['path'], item['plugin_id'])); skills.append(item)
    disabled_rows = settings.get('skills', {}).get('config', []) if provider == 'codex' else []
    disabled_rows = disabled_rows if isinstance(disabled_rows, list) else []
    disabled_rows = [{**{key: x[key] for key in ('path', 'name') if isinstance(x.get(key), str)},
                      'enabled': x['enabled']} for x in disabled_rows
                     if isinstance(x, dict) and isinstance(x.get('enabled'), bool)
                     and any(isinstance(x.get(key), str) for key in ('path', 'name'))]
    for skill in skills:
        if skill['plugin_id']:
            continue
        if provider == 'claude':
            state = settings.get('skillOverrides', {}).get(skill['name'], 'on')
            state = state if state in ('on', 'off', 'name-only', 'user-invocable-only') else 'unknown'
            skill['visibility'] = state
            skill['enabled'] = state != 'off'
        else:
            rows = [x for x in disabled_rows if x.get('name') == skill['name']
                    or (x.get('path') and Path(x['path']).expanduser().resolve() == Path(skill['path']))]
            if rows:
                skill['enabled'] = rows[-1]['enabled']
    return {'provider': provider, 'cwd': str(cwd) if cwd else None,
            'skills': sorted(skills, key=lambda x: (x['name'], x['path'])), 'plugins': plugins,
            'skill_config': disabled_rows, 'warnings': warnings,
            'availability': 'Local installation/configuration only; session launch overrides, managed settings and live tool availability are not verified.'}


def _strings(selection: dict, key: str) -> list[str]:
    values = selection.get(key, [])
    if not isinstance(values, list) or any(not isinstance(x, str) for x in values):
        raise ValueError(f'{key} must be a list of installed names or paths')
    return list(dict.fromkeys(values))


def build_profile(provider: str, selection: dict, inventory: dict) -> dict:
    provider = policy(provider)['provider']
    if inventory.get('provider') != provider:
        raise ValueError('Provider and inventory do not match')
    if not isinstance(selection, dict):
        raise ValueError('Selection must be an object')
    plugins = {x['id']: x for x in inventory.get('plugins', []) if x.get('installed', True)}
    enable = set(_strings(selection, 'enable_plugins'))
    disable = set(_strings(selection, 'disable_plugins'))
    preserve = set(_strings(selection, 'preserve_plugins'))
    for plugin_id in enable | disable | preserve:
        if plugin_id not in plugins:
            raise ValueError(f'Unknown installed plugin: {plugin_id}')
    def canonical(value):
        return str(Path(value).expanduser().resolve())
    def resolve_skill(value):
        skills = inventory.get('skills', [])
        # Exact names preserve an explicitly named plugin capability. Path inputs
        # may be symlinks advertised in both standalone and plugin catalogues.
        matches = [x for x in skills if value == x.get('name')]
        if not matches and (value.startswith(('/', '~', './', '../')) or value.endswith('/SKILL.md')):
            target = canonical(value)
            matches = [x for x in skills if canonical(x['path']) == target]
        paths = {canonical(x['path']) for x in matches}
        if len(paths) != 1:
            raise ValueError(f'Unknown or ambiguous installed skill: {value}')
        matches.sort(key=lambda x: (bool(x.get('plugin_id')), x.get('name', ''), x.get('plugin_id') or ''))
        item = copy.deepcopy(matches[0])
        item['path'] = canonical(item['path'])
        return item
    def resolve_selection(key):
        resolved = {}
        for value in _strings(selection, key):
            item = resolve_skill(value)
            previous = resolved.get(item['path'])
            if previous is None or (previous.get('plugin_id') and not item.get('plugin_id')):
                resolved[item['path']] = item
        return list(resolved.values())
    selected = resolve_selection('skills')
    removed = resolve_selection('disable_skills')
    if {x['path'] for x in selected} & {x['path'] for x in removed}:
        raise ValueError('A skill cannot be both selected and disabled')
    for skill in selected:
        if skill.get('plugin_id'):
            enable.add(skill['plugin_id'])
    unsupported = [dict(kind='skill', name=x['name'], path=x['path'], plugin_id=x['plugin_id'],
                        status='unsupported_individual_plugin_skill',
                        reason='Individual plugin-skill removal has no verified pane-only configuration override; parent plugin retained unless explicitly disabled.')
                   for x in removed if x.get('plugin_id')]
    removed = [x for x in removed if not x.get('plugin_id')]
    enable |= preserve
    if enable & disable:
        raise ValueError('Explicitly selected or preserved plugins cannot also be disabled')
    changes = {key: key in enable for key in sorted(enable | disable)}
    settings, argv = {}, []
    if provider == 'claude':
        overrides = {x['name']: 'on' for x in selected if not x.get('plugin_id')}
        overrides.update({x['name']: 'off' for x in removed})
        if overrides:
            settings['skillOverrides'] = overrides
        if changes:
            settings['enabledPlugins'] = changes
        if settings:
            argv = ['--settings', json.dumps(settings, separators=(',', ':'))]
    else:
        for plugin_id, enabled in changes.items():
            argv.extend(['-c', f'plugins.{json.dumps(plugin_id)}.enabled={str(enabled).lower()}'])
        local_changes = [(x, True) for x in selected if not x.get('plugin_id')] + [(x, False) for x in removed]
        if local_changes:
            # Codex applies ordered name/path rules. Keep every existing rule in
            # place, including repeated selectors, then append explicit overrides.
            merged = copy.deepcopy(inventory.get('skill_config', []))
            merged.extend({'path': skill['path'], 'enabled': enabled} for skill, enabled in local_changes)
            settings['skills'] = {'config': merged}
            encoded = ','.join('{' + ','.join(
                key + '=' + (str(value).lower() if isinstance(value, bool) else json.dumps(value))
                for key, value in row.items()) + '}' for row in merged)
            argv.extend(['-c', f'skills.config=[{encoded}]'])
        if changes:
            settings['plugins'] = {key: {'enabled': value} for key, value in changes.items()}
    return {'provider': provider, 'review_date': REVIEW_DATE, 'status': 'pending_resume',
            'scope': 'session_launch_only', 'settings': settings, 'argv': argv,
            'selected_skills': selected, 'plugin_changes': changes, 'unsupported_changes': unsupported,
            'limitations': [
                'Prepared launch overrides only; no running agent configuration was changed.',
                'Existing terminal sessions cannot receive arbitrary pane-only skill settings through this helper.',
                'Apply only during a controlled provider launch or resume; do not run a second writer for an active session.',
                'Managed policy and unavailable plugins may prevent activation; verify the resumed session inventory.',
            ] + [f"{x['name']}: {x['reason']}" for x in unsupported]}


profile = build_profile
build = build_profile
