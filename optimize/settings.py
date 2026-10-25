#!/usr/bin/env python3
"""Native harness settings, capability catalogs, agent profiles, and change requests."""
import argparse
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
import tomllib
from tile import API

BASE = Path(__file__).parent
STATE = Path.home() / '.config/herdr-harness'
AGENTS = STATE / 'agents.json'
CONFIG = Path.home() / '.config/herdr/config.toml'
FOOT = Path.home() / '.config/foot/herdr-prune.ini'
THEMES = {'Prune': ('rose-pine', '281a29', 'eadfeb'),
          'Midnight': ('tokyo-night', '151a2b', 'dce6ff'),
          'Graphite': ('one-dark', '202124', 'e5e7eb')}


def agent_profiles():
    if AGENTS.exists():
        return json.loads(AGENTS.read_text())
    return [{'name': n, 'command': c, 'logo': str(Path.home()/'.config/herdr-fractal/logos'/logo)}
            for n,c,logo in [('Codex','codex','line-codex.svg'),('Claude','claude','line-claude.svg'),
                             ('Hermes','hermes','line-qwen.svg'),('Jax','ollama run qwen3:4b-instruct','line-qwen.svg'),
                             ('Antigravity','agy','line-agy.svg')]]


def save_agents(profiles):
    STATE.mkdir(parents=True, exist_ok=True)
    temp = AGENTS.with_suffix('.tmp')
    temp.write_text(json.dumps(profiles, indent=2) + '\n'); temp.replace(AGENTS)


def catalog():
    skills = {}
    for root in [Path.home()/'.codex/skills', Path.home()/'.agents/skills', Path.home()/'.codex/plugins/cache']:
        for path in root.rglob('SKILL.md'):
            try:
                text = path.read_text()
                name = re.search(r'^name:\s*(.+)$', text, re.M)
                desc = re.search(r'^description:\s*(.+)$', text, re.M)
                label = name.group(1).strip('"\'') if name else path.parent.name
                skills.setdefault(label, {'name': label, 'description': desc.group(1)[:250] if desc else 'Installed skill',
                    'path': str(path), 'availability': 'Installed instructions; selected agent must read and apply them.'})
            except (OSError, UnicodeError): pass
    # Optional: a cache of the configuring agent's connected tools (MCP). Absent on a fresh install.
    tools_path = BASE/'connected-tools.json'
    data = json.loads(tools_path.read_text()) if tools_path.exists() else {'tools': []}
    groups = {}
    media = []
    for tool in data['tools']:
        parts = tool['name'].split('__')
        group = parts[1] if len(parts)>2 else parts[0]
        groups.setdefault(group, []).append(tool)
        if re.search(r'imagegen|image_gen|higgsfield|video_gen|sora|kling|veo|runway|flux', tool['name'], re.I):
            media.append({'name': tool['name'], 'description': tool['description'],
                          'availability': 'Connected in the configuring agent; provider access must be checked by your selected agent.'})
    plugins = [{'name': n, 'description': f'{len(v)} cataloged tools',
                'availability': 'Cataloged connector; this standalone app does not inherit its credentials.'} for n,v in groups.items()]
    for root in (Path.home()/'.codex/plugins/cache').glob('*'):
        for package in root.iterdir() if root.is_dir() else []:
            if package.is_dir(): plugins.append({'name':package.name,'description':'Installed plugin package',
                'availability':'Installed package; the selected agent must verify its tools and authentication.'})
    return {'skills': sorted(skills.values(), key=lambda x:x['name'].lower()),
            'plugins': sorted(plugins,key=lambda x:x['name']), 'media': media}


def replace_section(text, name, body):
    pattern = re.compile(r'^\[' + re.escape(name) + r'\]\s*\n.*?(?=^\[|\Z)', re.M|re.S)
    replacement = '[' + name + ']\n' + body.strip() + '\n\n'
    return pattern.sub(lambda _: replacement, text, count=1) if pattern.search(text) else text + '\n' + replacement


def backup_write(path, text):
    backups = STATE / 'backups'; backups.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backups / f'{time.time_ns()}-{path.name}')
    temp = path.with_suffix(path.suffix+'.tmp'); temp.write_text(text); temp.replace(path)


def launch_agent(api, profile):
    command = shlex.split(profile['command'])
    if not command or shutil.which(command[0]) is None:
        raise ValueError('This agent command is not installed. Edit the profile command first.')
    snapshot = api.call('session.snapshot')['snapshot']
    pane = snapshot['focused_pane_id']
    layout = next(l for l in snapshot['layouts'] if l['tab_id']==snapshot['focused_tab_id'])
    rect = next(p['rect'] for p in layout['panes'] if p['pane_id']==pane)
    response = api.call('pane.split', target_pane_id=pane, direction='right' if rect['width']>2*rect['height'] else 'down', ratio=.5, focus=True)
    created = response.get('pane') or response.get('new_pane')
    if not created:
        raise RuntimeError('The pane was created, but its identifier was unavailable; inspect the workspace.')
    api.call('pane.rename', pane_id=created['pane_id'], label=profile['name'])
    api.call('pane.send_input', pane_id=created['pane_id'], text=shlex.join(command), keys=['Enter'])


def main():
    import gi
    gi.require_version('Gtk','4.0')
    from gi.repository import Gtk, Gdk, Gio, GLib
    parser=argparse.ArgumentParser(); parser.add_argument('--section', default='settings'); parser.add_argument('--socket', required=True)
    parser.add_argument('--config',type=Path); parser.add_argument('--foot-config',type=Path)
    args=parser.parse_args(); api=API(args.socket); entries=catalog()
    global CONFIG, FOOT
    CONFIG=args.config or CONFIG; FOOT=args.foot_config or FOOT
    app=Gtk.Application(application_id='com.tbm.HerdrSettings', flags=Gio.ApplicationFlags.NON_UNIQUE)
    def activate(app):
        window=Gtk.ApplicationWindow(application=app,title='Herdr — Settings'); window.set_default_size(1080,780)
        root=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=12,margin_start=20,margin_end=20,margin_top=20,margin_bottom=20); window.set_child(root)
        header=Gtk.Box(spacing=8); root.append(header)
        title=Gtk.Label(label='Your agent workspace',hexpand=True,xalign=0); title.add_css_class('title-1'); header.append(title)
        status=Gtk.Label(label='Changes stay local. Existing terminal sessions are preserved.',wrap=True,xalign=0)
        def textview(text='',height=180):
            view=Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR,monospace=True); view.get_buffer().set_text(text)
            scroll=Gtk.ScrolledWindow(vexpand=True,min_content_height=height); scroll.set_child(view)
            return view,scroll
        def get(view):
            b=view.get_buffer(); return b.get_text(b.get_start_iter(),b.get_end_iter(),False)
        def run(action):
            try: action()
            except Exception as e: status.set_text(str(e))
        notebook=Gtk.Notebook(vexpand=True); root.append(notebook)
        request_view,request_scroll=textview('',200)
        def use_item(item):
            request_view.get_buffer().insert_at_cursor('Use '+item['name']+' for this task. '+(f'Read {item["path"]} first. ' if item.get('path') else 'Verify provider access and capability before using it. '))
            notebook.set_current_page(3)
            status.set_text(item['availability'])
        for section,label in [('skills','Skills'),('plugins','Plugins'),('media','Image & Video Apps')]:
            menu=Gtk.MenuButton(label=label+' ▾'); pop=Gtk.Popover(); box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=6,margin_start=10,margin_end=10,margin_top=10,margin_bottom=10)
            search=Gtk.SearchEntry(placeholder_text='Search '+label.lower()); box.append(search)
            listing=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=4)
            scroll=Gtk.ScrolledWindow(min_content_width=430,min_content_height=360,max_content_height=480); scroll.set_child(listing); box.append(scroll)
            buttons=[]
            for item in entries[section]:
                button=Gtk.Button(label=item['name']); button.set_tooltip_text(item['description']+'\n'+item['availability'])
                button.connect('clicked',lambda _, item=item,pop=pop: (use_item(item),pop.popdown())); listing.append(button); buttons.append((button,item))
            def filter_list(entry,buttons=buttons):
                query=entry.get_text().lower()
                for button,item in buttons: button.set_visible(query in (item['name']+' '+item['description']).lower())
            search.connect('search-changed',filter_list); pop.set_child(box); menu.set_popover(pop); header.append(menu)
            if args.section==section: GLib.idle_add(menu.popup)
        appearance=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=14,margin_top=16)
        appearance.append(Gtk.Label(label='Theme and transparency',xalign=0))
        themes=Gtk.DropDown.new_from_strings(['Keep current appearance']+list(THEMES)); appearance.append(themes)
        alpha=.15
        match=re.search(r'^alpha\s*=\s*([\d.]+)',FOOT.read_text(),re.M)
        if match: alpha=float(match.group(1))
        appearance.append(Gtk.Label(label='Background opacity (0 = transparent; 1 = solid)',xalign=0))
        slider=Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,.05,1,.05); slider.set_value(alpha); slider.set_digits(2); appearance.append(slider)
        def save_appearance():
            foot=FOOT.read_text(); foot=re.sub(r'^alpha\s*=.*$',f'alpha={slider.get_value():.2f}',foot,flags=re.M)
            selection=themes.get_selected()
            if selection:
                theme,bg,fg=THEMES[list(THEMES)[selection-1]]
                config=CONFIG.read_text(); section=tomllib.loads(config).get('theme',{}); section['name']=theme
                # Preserve the custom and appearance-specific subtables verbatim.
                body='\n'.join(k+' = '+json.dumps(v) for k,v in section.items() if not isinstance(v,dict))
                config=replace_section(config,'theme',body); tomllib.loads(config); backup_write(CONFIG,config)
                foot=re.sub(r'^background\s*=.*$', 'background='+bg,foot,flags=re.M); foot=re.sub(r'^foreground\s*=.*$', 'foreground='+fg,foot,flags=re.M)
            backup_write(FOOT,foot)
            api.call('server.reload_config')
            status.set_text('Saved. Herdr theme reloaded; terminal transparency applies when its window is reopened. Sessions remain running.')
        button=Gtk.Button(label='Save appearance'); button.connect('clicked',lambda _:run(save_appearance)); appearance.append(button)
        notebook.append_page(appearance,Gtk.Label(label='Appearance'))
        keys=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=10,margin_top=12)
        keys.append(Gtk.Label(label='Super+Alt+arrows: rearrange tiles. Shift+Enter: steer/send. Existing shortcuts below remain editable.',wrap=True,xalign=0))
        defaults=subprocess.check_output(['/usr/bin/herdr','--default-config'],text=True)
        block=re.search(r'^\[keys\]\s*\n(.*?)(?=^\[|\Z)',defaults,re.M|re.S)
        effective={}
        for line in (block.group(1) if block else '').splitlines():
            candidate=re.sub(r'^#\s?', '',line)
            try: effective.update(tomllib.loads(candidate))
            except tomllib.TOMLDecodeError: pass
        effective.update(tomllib.loads(CONFIG.read_text()).get('keys',{}))
        all_keys,all_scroll=textview('\n'.join(f'{k}: {v}' for k,v in sorted(effective.items())),180)
        all_keys.set_editable(False)
        expander=Gtk.Expander(label='Read all effective Herdr shortcuts'); expander.set_child(all_scroll); keys.append(expander)
        raw=CONFIG.read_text(); match=re.search(r'^\[keys\]\s*\n(.*?)(?=^\[|\Z)',raw,re.M|re.S)
        editor,scroll=textview(match.group(1) if match else ''); keys.append(scroll)
        def save_keys():
            text=replace_section(CONFIG.read_text(),'keys',get(editor)); tomllib.loads(text); backup_write(CONFIG,text)
            api.call('server.reload_config'); status.set_text('Shortcuts saved and configuration reloaded. Invalid bindings appear in Herdr’s configuration warning.')
        button=Gtk.Button(label='Save shortcuts'); button.connect('clicked',lambda _:run(save_keys)); keys.append(button); notebook.append_page(keys,Gtk.Label(label='Key bindings'))
        agents=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=10,margin_top=12)
        from local_models import picker
        agents.append(picker(Gtk, GLib, api, launch_agent, status))
        agents.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        profiles=agent_profiles(); names=Gtk.DropDown.new_from_strings([p['name'] for p in profiles]); agents.append(names)
        name=Gtk.Entry(placeholder_text='Agent name'); command=Gtk.Entry(placeholder_text='Installed command, for example codex'); logo=Gtk.Entry(placeholder_text='Local SVG/PNG logo path')
        for label,entry in [('Name',name),('Command',command),('Logo path',logo)]: agents.append(Gtk.Label(label=label,xalign=0)); agents.append(entry)
        def select(*_):
            p=profiles[names.get_selected()]; name.set_text(p['name']); command.set_text(p['command']); logo.set_text(p.get('logo',''))
        names.connect('notify::selected',select); select()
        def save_profile():
            item={'name':name.get_text().strip(),'command':command.get_text().strip(),'logo':logo.get_text().strip()}
            if not item['name'] or not shlex.split(item['command']): raise ValueError('Enter an agent name and command.')
            if item['logo'] and not Path(item['logo']).is_file(): raise ValueError('Choose an existing local logo file.')
            existing=next((i for i,p in enumerate(profiles) if p['name']==item['name']),None)
            if existing is None: profiles.append(item)
            else: profiles[existing]=item
            save_agents(profiles); names.set_model(Gtk.StringList.new([p['name'] for p in profiles])); status.set_text('Agent profile saved. Logos refresh automatically; launch starts a new tile.')
        for label,callback in [('Save / add agent',save_profile),('Launch in a new tile',lambda:launch_agent(api,profiles[names.get_selected()]))]:
            button=Gtk.Button(label=label); button.connect('clicked',lambda _,callback=callback:run(callback)); agents.append(button)
        notebook.append_page(agents,Gtk.Label(label='Agents and logos'))
        requests=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=10,margin_top=12)
        requests.append(Gtk.Label(label='Request a change to this app. Choose an agent; review the request before sending.',xalign=0,wrap=True)); requests.append(request_scroll)
        panes=api.call('pane.list')['panes']; targets=[p for p in panes if p.get('agent')]
        target=Gtk.DropDown.new_from_strings([p.get('label') or f'{p.get("agent")} · {p["pane_id"]}' for p in targets] or ['No agent is running']); requests.append(target)
        def send_request():
            text=get(request_view).strip()
            if not text: raise ValueError('Describe the change first.')
            if not targets: raise ValueError('Start an agent in a tile first.')
            terminal=targets[target.get_selected()]['terminal_id']
            current=next(p for p in api.call('pane.list')['panes'] if p['terminal_id']==terminal)
            api.call('pane.send_input',pane_id=current['pane_id'],text='Requested app change: '+text,keys=['Enter'])
            status.set_text('Change request sent through the selected agent’s native input. Check its tile for progress.')
        button=Gtk.Button(label='Send change request'); button.connect('clicked',lambda _:run(send_request)); requests.append(button)
        notebook.append_page(requests,Gtk.Label(label='Request a change'))
        root.append(status); window.present()
        if args.section == 'models': notebook.set_current_page(2)
    app.connect('activate',activate); app.run([])


if __name__=='__main__': main()
