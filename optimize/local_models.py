"""Installed Ollama model picker and guarded interactive pane entry point."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.request

HOST = 'http://127.0.0.1:11434'


def installed_models():
    # Match the guarded local endpoint, regardless of inherited cloud/remote hosts.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(HOST + '/api/tags', timeout=5) as response:
        data = json.load(response)
    models = {}
    for item in data['models']:
        name = item.get('name', '')
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*', name):
            models[name] = {'name': name, 'size': max(0, int(item.get('size', 0)))}
    return sorted(models.values(), key=lambda model: (model['size'], model['name']))


def model_profile(name):
    if not shutil.which('ollama'):
        raise RuntimeError('Ollama is not installed.')
    if name not in {model['name'] for model in installed_models()}:
        raise ValueError('This model is no longer installed. Refresh the list.')
    return {'name': 'Local · ' + name,
            'command': shlex.join([sys.executable, str(Path(__file__).resolve()), '--run', name])}


def run_model(name):
    model_profile(name)  # Recheck inside the new pane before invoking Ollama.
    return subprocess.call([str(Path.home()/'.local/bin/qwen-chat'), '--model', name])


def picker(Gtk, GLib, api, launch_agent, status):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    heading = Gtk.Label(label='Local models', xalign=0)
    heading.add_css_class('heading'); box.append(heading)
    row = Gtk.Box(spacing=8); box.append(row)
    select = Gtk.DropDown.new_from_strings(['Finding installed models…'])
    select.set_enable_search(True); select.set_hexpand(True); select.set_sensitive(False)
    row.append(select)
    refresh = Gtk.Button(label='Refresh'); row.append(refresh)
    launch = Gtk.Button(label='Load in a new pane'); launch.set_sensitive(False); row.append(launch)
    detail = Gtk.Label(label='Installed Ollama models on this computer.', xalign=0, wrap=True)
    box.append(detail)
    models = []

    def selection_changed(*_):
        index = select.get_selected()
        if index < len(models):
            model = models[index]
            detail.set_text(f"{model['size'] / 1024**3:.1f} GiB on disk · Ollama · Loads when opened in a pane")

    select.connect('notify::selected', selection_changed)

    def busy(value):
        refresh.set_sensitive(not value)
        select.set_sensitive(not value and bool(models))
        launch.set_sensitive(not value and bool(models))

    def load_list(*_):
        previous = models[select.get_selected()]['name'] if select.get_selected() < len(models) else None
        busy(True); detail.set_text('Finding installed models…')
        def work():
            try:
                result, error = installed_models(), None
            except Exception as exc:
                result, error = [], str(exc)
            def finish():
                models[:] = result
                select.set_model(Gtk.StringList.new([m['name'] for m in models] or ['No installed models']))
                select.set_selected(next((i for i, m in enumerate(models) if m['name'] == previous), 0))
                busy(False)
                if models: selection_changed()
                else: detail.set_text('Local Ollama unavailable: ' + error if error else 'No models installed in local Ollama. Refresh after installing one.')
                return False
            GLib.idle_add(finish)
        threading.Thread(target=work, daemon=True).start()

    def load_pane(*_):
        index = select.get_selected()
        if index >= len(models): return
        name = models[index]['name']; busy(True)
        status.set_text('Opening ' + name + ' in a new pane…')
        def work():
            try:
                launch_agent(api, model_profile(name))
                message = 'Opened ' + name + '. Check its pane for loading progress; type /bye to exit.'
            except Exception as exc:
                message = 'Could not open local model: ' + str(exc)
            def finish():
                busy(False); status.set_text(message)
                return False
            GLib.idle_add(finish)
        threading.Thread(target=work, daemon=True).start()

    refresh.connect('clicked', load_list); launch.connect('clicked', load_pane)
    load_list()
    return box


if __name__ == '__main__':
    try:
        if len(sys.argv) != 3 or sys.argv[1] != '--run':
            raise ValueError('Choose a model from Herdr’s Local models dropdown.')
        sys.exit(run_model(sys.argv[2]))
    except Exception as exc:
        print('Local model could not start:', exc, file=sys.stderr)
        sys.exit(1)
