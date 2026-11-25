"""Task folders shared by the recorder and episode browser."""
import hashlib
import json
from pathlib import Path
import re


# Ignore connecting words when deriving a short, readable task folder name.
STOP_WORDS = {'a', 'an', 'the', 'and', 'into', 'in', 'on', 'of', 'to', 'with', 'then', 'please'}


def task_folder(root, instruction, write_metadata=True):
    root = Path(root)
    instruction = ' '.join(instruction.split())
    root.mkdir(parents=True, exist_ok=True)
    if not write_metadata:
        words = [word for word in re.findall(r'[^\W_]+', instruction.lower()) if word not in STOP_WORDS]
        short = '-'.join(words[:6])[:60].rstrip('-') or 'untitled-task'
        folder = root / f'{short}-{hashlib.sha256(instruction.encode()).hexdigest()[:8]}'
        folder.mkdir(exist_ok=True)
        if folder.resolve().parent != root.resolve():
            raise ValueError('Task folder must be inside the episodes directory.')
        return folder
    for manifest in root.glob('*/task.json'):
        if json.loads(manifest.read_text()).get('instruction') == instruction:
            folder = manifest.parent.resolve()
            if folder.parent != root.resolve():
                raise ValueError('Task folder must be inside the episodes directory.')
            return manifest.parent
    words = [word for word in re.findall(r'[^\W_]+', instruction.lower()) if word not in STOP_WORDS]
    short = '-'.join(words[:6])[:60].rstrip('-') or 'untitled-task'
    folder = root / short
    if folder.exists():
        digest = hashlib.sha256(instruction.encode()).hexdigest()
        folder = root / f'{short}-{digest[:8]}'
        if folder.exists():
            raise ValueError('Task folder name is already occupied. Check its task.json before recording.')
    folder.mkdir()
    (folder / 'task.json').write_text(json.dumps({'name': folder.name, 'instruction': instruction}, indent=2, ensure_ascii=False) + '\n')
    return folder


def episode_manifests(root, collection_mode=None):
    root = Path(root)
    # Legacy task folders, Mode 03 buffers, and Mode 04 run/stage folders.
    paths = [*root.glob('episode_*/episode.json'), *root.glob('*/episode_*/episode.json'),
             *root.glob('*/stage_*/episode_*/episode.json')]
    if collection_mode:
        selected = []
        for path in paths:
            episode = json.loads(path.read_text())
            mode = episode.get('context', {}).get('collection_mode')
            if mode == collection_mode or (collection_mode == 'gpt' and mode is None and episode.get('controller_owner') == 'gpt'):
                selected.append(path)
        paths = selected
    return sorted(paths, key=lambda path: (path.parent.name, str(path)))
