"""Inventario di una cartella e riordino secondo un piano approvato. Solo libreria standard; nessuna rete.

Lo script non decide nulla: l'agente scrive il piano, l'utente lo approva, lo script lo esegue.
Non sovrascrive, non cancella (salvo i metadati che il sistema rigenera, nelle cartelle da togliere,
e l'origine di uno spostamento tra dischi dopo averne verificato la copia), si ferma al primo problema
e annota ogni mossa nel giornale.
"""
from __future__ import annotations
import argparse
from collections import Counter
from contextlib import contextmanager
import fnmatch
import hashlib
import heapq
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time
import unicodedata

# Attributi Windows dei file solo nel cloud: leggerli li scaricherebbe.
CLOUD = 0x1000 | 0x40000 | 0x400000
# Flag macOS dei file "dataless" di iCloud e degli altri File Provider; stat.SF_DATALESS solo da Python 3.13.
DATALESS = 0x40000000 if sys.platform == 'darwin' else 0
# Bit "name surrogate" del reparse tag: il reparse point rimanda a un altro percorso (junction, link WSL).
SURROGATE = 0x20000000
# File system che di norma non distinguono maiuscole e minuscole: NTFS su Windows, APFS su macOS.
NOCASE = os.name == 'nt' or sys.platform == 'darwin'
# Metadati che Finder ed Esplora risorse rigenerano: non contano come contenuto di una cartella.
METADATA = {'.ds_store', 'thumbs.db'}
# Cartelle trattate come unità: l'inventario non scende al loro interno.
HEAVY = {n.casefold() for n in ('.git', '.hg', '.svn', 'node_modules', '.venv', 'venv', '__pycache__',
                                '.pytest_cache', '.mypy_cache', '.cache', '.uv-cache', '.tox', '.next')}
# Pacchetti macOS: cartelle che il Finder mostra come un solo documento o un'app; si spostano intere.
PACKAGES = {'.app', '.bundle', '.framework', '.plugin', '.workflow', '.photoslibrary', '.photolibrary',
            '.musiclibrary', '.tvlibrary', '.imovielibrary', '.theater', '.fcpbundle', '.logicx', '.band',
            '.rtfd', '.pages', '.numbers', '.key', '.xcodeproj', '.xcworkspace', '.playground', '.sparsebundle'}
# Segnali di cartella-progetto: si sposta intera o resta dov'è.
PROJECT = {n.casefold() for n in ('.git', '.venv', 'venv', 'node_modules', 'package.json', 'pyproject.toml',
                                  'requirements.txt', 'Cargo.toml', 'go.mod')}
# Nomi da segreto: l'inventario li segnala senza leggerli.
SECRETS = ('.env', '.env.*', '*.pem', '*.key', 'id_rsa*', 'id_ed25519*', '*.pfx', '*.p12', '*.kdbx',
           '*passw*', '*credenzial*', '*credential*')

def stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

def day(ns):
    return time.strftime('%Y-%m-%d', time.localtime(ns / 1e9))

def is_link(info):
    # Link simbolici e junction non si seguono e non si spostano; il criterio è quello di os.lstat.
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_reparse_tag', 0) & SURROGATE)

def is_cloud(info):
    return bool(getattr(info, 'st_file_attributes', 0) & CLOUD or getattr(info, 'st_flags', 0) & DATALESS)

def is_package(name):
    return Path(name).suffix.casefold() in PACKAGES

def keynote(path):
    # Le chiavi sono testo (PEM) o DER, le presentazioni Keynote sono zip: bastano i primi byte,
    # letti dallo script e mostrati a nessuno.
    try:
        with open(path, 'rb') as f:
            return f.read(4) == b'PK\x03\x04'
    except OSError:
        return False

def is_secret(name, path, info):
    # Un .key che è uno zip è una presentazione Keynote; uno solo nel cloud non si legge e resta un segreto.
    if not any(fnmatch.fnmatchcase(name.casefold(), p) for p in SECRETS):
        return False
    return not (name.casefold().endswith('.key') and not is_cloud(info) and keynote(path))

def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

# --- Inventario ---

def tally():
    return {'file': 0, 'cartelle': 0, 'byte': 0, 'solo_cloud': 0, 'estensioni': Counter(),
            'dal': None, 'al': None, 'pesanti': set(), 'parziale': False}

def add_file(rel, path, info, acc, state):
    size, mtime, name = info.st_size, info.st_mtime_ns, rel.rsplit('/', 1)[-1]
    ext = Path(name).suffix.casefold() or '(nessuna)'
    for t in (acc, state['totale']):
        t['file'] += 1
        t['byte'] += size
    acc['estensioni'][ext] += 1
    state['estensioni'][ext] += 1
    acc['dal'] = min(acc['dal'] or mtime, mtime)
    acc['al'] = max(acc['al'] or mtime, mtime)
    heapq.heappush(state['grandi'], (size, rel))
    if len(state['grandi']) > 10:
        heapq.heappop(state['grandi'])
    secret = is_secret(name, path, info)
    if secret:
        state['segreti'].append(rel)
    if is_cloud(info):
        acc['solo_cloud'] += 1
        state['totale']['solo_cloud'] += 1
    elif size and not secret:
        state['per_dimensione'].setdefault(size, []).append((rel, path))
    return secret

def visit(folder, rel, level, depth, acc, state):
    try:
        with os.scandir(folder) as listing:
            entries = sorted(listing, key=lambda e: e.name.casefold())
    except OSError as exc:
        state['errori'].append({'percorso': rel, 'errore': str(exc)})
        acc['parziale'] = True
        return
    if any(e.name.casefold() in PROJECT for e in entries):
        state['progetti'].append(rel)
    for entry in entries:
        child = f'{rel}/{entry.name}'
        try:
            info = os.stat(entry.path, follow_symlinks=False)
        except OSError as exc:
            state['errori'].append({'percorso': child, 'errore': str(exc)})
            continue
        if is_link(info):
            state['link'].append(child)
        elif stat.S_ISDIR(info.st_mode):
            acc['cartelle'] += 1
            state['totale']['cartelle'] += 1
            if entry.name.casefold() in HEAVY:
                acc['pesanti'].add(entry.name)
                state['pesanti'].add(entry.name)
            elif is_package(entry.name):
                state['pacchetti'].append(child)
            elif depth is None or level + 1 < depth:
                visit(entry.path, child, level + 1, depth, acc, state)
            else:
                acc['parziale'] = True
        elif stat.S_ISREG(info.st_mode) and entry.name.casefold() not in METADATA:
            add_file(child, entry.path, info, acc, state)

def summary(acc):
    out = {'file': acc['file'], 'cartelle': acc['cartelle'], 'byte': acc['byte'],
           'estensioni': dict(acc['estensioni'].most_common(5))}
    if acc['dal']:
        out.update(dal=day(acc['dal']), al=day(acc['al']))
    if acc['solo_cloud']:
        out['solo_cloud'] = acc['solo_cloud']
    if acc['pesanti']:
        out['pesanti'] = sorted(acc['pesanti'], key=str.casefold)
    if acc['parziale']:
        out['parziale'] = True
    return out

def duplicates(by_size, limit=20):
    # Impronte solo fra file locali della stessa dimensione: niente letture inutili.
    groups = []
    for size, files in by_size.items():
        if len(files) < 2:
            continue
        by_hash = {}
        for rel, path in files:
            try:
                by_hash.setdefault(digest(path), []).append(rel)
            except OSError:
                continue
        groups += [{'byte': size, 'percorsi': sorted(r)} for r in by_hash.values() if len(r) > 1]
    groups.sort(key=lambda g: g['byte'] * (len(g['percorsi']) - 1), reverse=True)
    return groups[:limit]

def inventory(root, depth=None, limit=300):
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise ValueError(f'Cartella non trovata: {root}')
    if depth is not None and depth < 1:
        raise ValueError('Profondità non valida: usare un intero >= 1')
    state = {'totale': {'file': 0, 'cartelle': 0, 'byte': 0, 'solo_cloud': 0}, 'estensioni': Counter(),
             'progetti': [], 'pacchetti': [], 'segreti': [], 'link': [], 'pesanti': set(), 'errori': [],
             'grandi': [], 'per_dimensione': {}}
    with os.scandir(base) as listing:
        entries = sorted(listing, key=lambda e: e.name.casefold())
    items = []
    for entry in entries:
        name = entry.name
        try:
            info = os.stat(entry.path, follow_symlinks=False)
        except OSError as exc:
            state['errori'].append({'percorso': name, 'errore': str(exc)})
            continue
        if is_link(info):
            state['link'].append(name)
            items.append({'nome': name, 'tipo': 'link'})
        elif stat.S_ISDIR(info.st_mode):
            state['totale']['cartelle'] += 1
            acc, item = tally(), {'nome': name, 'tipo': 'cartella'}
            if name.casefold() in HEAVY:
                acc['pesanti'].add(name)
                state['pesanti'].add(name)
            elif is_package(name):
                state['pacchetti'].append(name)
                item['pacchetto'] = True
            elif depth is None or depth > 1:
                visit(entry.path, name, 1, depth, acc, state)
            else:
                acc['parziale'] = True
            item.update(summary(acc))
            if name in state['progetti']:
                item['progetto'] = True
            items.append(item)
        elif stat.S_ISREG(info.st_mode) and name.casefold() not in METADATA:
            secret = add_file(name, entry.path, info, tally(), state)
            item = {'nome': name, 'tipo': 'file', 'byte': info.st_size, 'modificato': day(info.st_mtime_ns)}
            if is_cloud(info):
                item['solo_cloud'] = True
            if secret:
                item['segreto'] = True
            items.append(item)
    return {'radice': str(base), 'totale': state['totale'], 'voci': items[:limit],
            'altre_voci': max(0, len(items) - limit), 'estensioni': dict(state['estensioni'].most_common(15)),
            'progetti': state['progetti'][:50], 'pacchetti': state['pacchetti'][:50],
            'segreti': state['segreti'][:50], 'link': state['link'][:50],
            'duplicati': duplicates(state['per_dimensione']),
            'grandi': [{'percorso': rel, 'byte': size} for size, rel in sorted(state['grandi'], reverse=True)],
            'esclusioni_suggerite': sorted(state['pesanti'], key=str.casefold), 'errori': state['errori'][:50]}

# --- Piano ---

def relative(rel):
    # Percorso relativo alla radice, separato da '/': niente percorsi assoluti né risalite.
    p = Path(str(rel))
    if p.is_absolute() or p.drive or p.root or not p.parts or '..' in p.parts:
        raise ValueError(f'Percorso non valido nel piano: {rel}')
    return p.as_posix()

def folder(value):
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f'Radice del piano non trovata: {root}')
    return root

class Places:
    """Radici di un piano. Con "radice" i percorsi sono relativi a quella; con "radici" cominciano
    dal nome della radice: "Desktop/fattura.pdf", "Archivio/finanze/fattura.pdf"."""

    def __init__(self, roots):
        self.roots = roots

    def text(self, rel):
        rel = relative(rel)
        if '' not in self.roots:
            name, _, rest = rel.partition('/')
            if name not in self.roots:
                raise ValueError(f'Il percorso non comincia da una radice del piano: {rel}')
            if not rest:
                raise ValueError(f'Percorso non valido nel piano: {rel} è una radice')
        return rel

    def split(self, rel):
        if '' in self.roots:
            return self.roots[''], rel
        name, _, rest = rel.partition('/')
        return self.roots[name], rest

    def __call__(self, rel):
        base, rest = self.split(rel)
        return base / rest if rest else base

    def describe(self):
        if '' in self.roots:
            return {'radice': str(self.roots[''])}
        return {'radici': {name: str(path) for name, path in self.roots.items()}}

def load_plan(path):
    plan = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(plan, dict) or ('radice' in plan) == ('radici' in plan) or not isinstance(plan.get('mosse'), list):
        raise ValueError('Piano non valido: servono "mosse" e "radice" oppure "radici"')
    if 'radice' in plan:
        roots = {'': folder(plan['radice'])}
    else:
        if not isinstance(plan['radici'], dict) or not plan['radici']:
            raise ValueError('Piano non valido: "radici" associa un nome a ogni cartella')
        roots = {}
        for name, value in plan['radici'].items():
            if not name or name in ('.', '..') or any(c in name for c in '/\\:'):
                raise ValueError(f'Nome di radice non valido: {name}')
            roots[name] = folder(value)
        # Radici distinte e disgiunte: un percorso del piano ha un solo modo di essere scritto.
        names = list(roots)
        if len({n.casefold() for n in names}) < len(names):
            raise ValueError('Nomi di radice ripetuti: le maiuscole non contano')
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                pa, pb = (key(roots[n].as_posix().rstrip('/')) + '/' for n in (a, b))
                if pa.startswith(pb) or pb.startswith(pa):
                    raise ValueError(f'Le radici {a} e {b} si sovrappongono')
    places = Places(roots)
    moves = []
    for move in plan['mosse']:
        if not isinstance(move, dict) or 'da' not in move or 'a' not in move:
            raise ValueError('Mossa non valida: servono "da" e "a"')
        moves.append((places.text(move['da']), places.text(move['a'])))
    # Cartelle-contenitore da togliere se le mosse le lasciano vuote.
    if not isinstance(plan.get('vuote', []), list):
        raise ValueError('Piano non valido: "vuote" è un elenco di cartelle')
    return places, moves, [places.text(f) for f in plan.get('vuote', [])]

def journal_path(plan_path):
    return plan_path.with_name(plan_path.stem + '.giornale.jsonl')

def read_journal(journal):
    entries = []
    lines = journal.read_text(encoding='utf-8').splitlines() if journal.is_file() else []
    for number, line in enumerate(lines, 1):
        try:
            entries += [json.loads(line)] if line.strip() else []
        except ValueError:
            # Ogni riga precede la sua mossa: un'ultima riga troncata non corrisponde a una mossa fatta.
            raise ValueError(f'Giornale illeggibile alla riga {number}: {journal}. Se è l\'ultima, '
                             "è stata troncata da un'interruzione prima della sua mossa e si può togliere") from None
    return entries

def key(rel):
    # Percorsi confrontati come li confronta il file system: su macOS gli accenti possono arrivare
    # scomposti (NFD); su Windows e macOS le maiuscole non contano.
    rel = unicodedata.normalize('NFC', rel)
    return rel.casefold() if NOCASE else rel

def ancestors(rel):
    # 'a/b/c' -> ['a', 'a/b', 'a/b/c']
    parts = rel.split('/')
    return ['/'.join(parts[:i]) for i in range(1, len(parts) + 1)]

def linked(at, rel):
    # Vero se rel, o una cartella che lo contiene dentro la sua radice, è un link simbolico o una junction.
    base, rest = at.split(rel)
    return any(os.path.lexists(base / a) and is_link(os.stat(base / a, follow_symlinks=False)) for a in ancestors(rest))

# --- Spostamenti tra dischi ---

def volume(path):
    # Volume del percorso o, se non esiste ancora, della prima cartella che lo contiene.
    for p in (path, *path.parents):
        if os.path.lexists(p):
            return os.stat(p, follow_symlinks=False).st_dev
    return None

def crossing(src, dst):
    # Vero se src e dst stanno su volumi diversi: os.rename non basta, serve una copia.
    return volume(src) != volume(dst)

def partial(path):
    # Copia in corso, accanto alla destinazione: prende il nome finale solo dopo la verifica.
    return path.with_name(path.name + '.scrivano-copia')

def has_link(path):
    # Vero se nella cartella c'è un link simbolico o una junction: una copia non saprebbe riprodurli.
    stack = [path]
    while stack:
        with os.scandir(stack.pop()) as listing:
            for entry in listing:
                info = os.stat(entry.path, follow_symlinks=False)
                if is_link(info):
                    return True
                if stat.S_ISDIR(info.st_mode):
                    stack.append(entry.path)
    return False

def copy_tree(src, dst):
    # Copia senza sovrascrivere e rilegge ogni file copiato: l'impronta deve essere quella dell'origine.
    info = os.stat(src, follow_symlinks=False)
    if is_link(info):
        raise OSError(f'Link simbolico o junction: da spostare a mano: {src}')
    if stat.S_ISDIR(info.st_mode):
        os.mkdir(dst)
        with os.scandir(src) as listing:
            names = [e.name for e in listing]
        for name in names:
            copy_tree(src / name, dst / name)
        shutil.copystat(src, dst)
        return
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f'Né file né cartella: {src}')
    h = hashlib.sha256()
    with open(src, 'rb') as fin, open(dst, 'xb') as fout:
        for block in iter(lambda: fin.read(1024 * 1024), b''):
            h.update(block)
            fout.write(block)
    after = os.stat(src, follow_symlinks=False)
    if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
        raise OSError(f'File modificato durante la copia: {src}')
    shutil.copystat(src, dst)
    if digest(dst) != h.hexdigest():
        raise OSError(f"Copia diversa dall'originale: {dst}")

def remove(path):
    # Toglie l'origine di uno spostamento già copiato e verificato; non segue link né junction.
    info = os.stat(path, follow_symlinks=False)
    if is_link(info):
        raise OSError(f'Link simbolico o junction: da togliere a mano: {path}')
    if stat.S_ISDIR(info.st_mode):
        with os.scandir(path) as listing:
            names = [e.name for e in listing]
        for name in names:
            remove(path / name)
        os.rmdir(path)
        return
    try:
        os.unlink(path)
    except PermissionError:
        # Su Windows un file di sola lettura non si cancella finché non perde l'attributo.
        os.chmod(path, stat.S_IWRITE)
        os.unlink(path)

def contained(part, whole):
    # Vero se ogni file di part c'è anche in whole, identico: part è allora l'avanzo di una copia finita.
    if not os.path.lexists(whole):
        return False
    info, other = os.stat(part, follow_symlinks=False), os.stat(whole, follow_symlinks=False)
    if is_link(info) or is_link(other):
        return False
    if stat.S_ISDIR(info.st_mode):
        if not stat.S_ISDIR(other.st_mode):
            return False
        with os.scandir(part) as listing:
            names = [e.name for e in listing]
        return all(contained(part / name, whole / name) for name in names)
    return (stat.S_ISREG(info.st_mode) and stat.S_ISREG(other.st_mode) and info.st_size == other.st_size
            and digest(part) == digest(whole))

def transfer(src, dst, copied=lambda: None):
    # Spostamento tra dischi: copia con un nome temporaneo, verifica, prende il nome finale e solo allora
    # toglie l'origine. Un'interruzione lascia l'origine intera, o una copia completa e un avanzo da togliere.
    temp = partial(dst)
    copy_tree(src, temp)
    copied()
    if os.path.lexists(dst):
        raise FileExistsError(f'Destinazione comparsa durante il riordino: {dst}')
    os.rename(temp, dst)
    remove(src)

def finish(src, dst):
    # Ripresa di uno spostamento tra dischi interrotto mentre toglieva l'origine: ne restano dei file,
    # che si tolgono solo se la destinazione li ha identici. Restituisce quanti file ha tolto.
    if not contained(src, dst):
        raise OSError(f"L'origine non coincide con la copia già fatta: verifica a mano {src}")
    files = count_files(src) if os.path.isdir(src) else 1
    remove(src)
    return files

# --- Esecuzione ---

def status(at, src, dst, noted=False, copied=False):
    a, b = at(src), at(dst)
    has_src, has_dst = os.path.lexists(a), os.path.lexists(b)
    if has_src and has_dst:
        # Una copia tra dischi finita e verificata, interrotta mentre toglieva l'origine, si completa.
        return (None, 'da_finire') if copied else ('destinazione già occupata', None)
    if not has_src and not has_dst:
        return 'origine mancante', None
    if not has_src:
        # Già fatta solo se il giornale la registra; altrimenti il piano non descrive la cartella.
        return (None, 'fatta') if noted else ('origine mancante e destinazione già occupata', None)
    if linked(at, src):
        return 'link simbolico o junction: da spostare a mano', None
    if linked(at, dst):
        return 'la destinazione passa da un link simbolico o una junction', None
    if any(os.path.lexists(at(p)) and not os.path.isdir(at(p)) for p in ancestors(dst)[:-1]):
        return 'un file occupa il percorso di destinazione', None
    if os.path.lexists(partial(b)) and not noted:
        return f'copia temporanea già presente: {partial(b).name}', None
    if crossing(a, b) and os.path.isdir(a) and has_link(a):
        return 'cartella con link simbolici o junction diretta su un altro disco: da spostare a mano', None
    return None, 'da_fare'

def check(at, moves, empties=(), noted=frozenset(), copied=frozenset()):
    # Il piano descrive l'albero originale: origini e destinazioni non si sovrappongono.
    keys = [(key(s), key(d)) for s, d in moves]
    sources, targets = Counter(s for s, _ in keys), Counter(d for _, d in keys)
    states, problems = [], []
    for number, ((src, dst), (ks, kd)) in enumerate(zip(moves, keys), 1):
        state = None
        if sources[ks] > 1 or targets[kd] > 1:
            issue = 'origine o destinazione ripetuta nel piano'
        elif ks == kd:
            issue = ('origine e destinazione coincidono' if src == dst else
                     'cambia solo maiuscole e minuscole: passa da un nome intermedio, in due piani')
        elif any(a in sources for a in ancestors(kd)):
            issue = "destinazione dentro un'origine del piano"
        elif any(a in targets for a in ancestors(ks)):
            issue = 'origine dentro una destinazione del piano'
        elif any(a in sources for a in ancestors(ks)[:-1]):
            issue = "origine dentro un'altra origine del piano"
        elif any(a in targets for a in ancestors(kd)[:-1]):
            issue = "destinazione dentro un'altra destinazione del piano"
        else:
            issue, state = status(at, src, dst, (ks, kd) in noted, (ks, kd) in copied)
        states.append(state)
        if issue:
            problems.append({'mossa': number, 'da': src, 'a': dst, 'problema': issue})
    for folder in empties:
        if os.path.lexists(at(folder)) and (linked(at, folder) or not os.path.isdir(at(folder))):
            problems.append({'vuota': folder, 'problema': 'non è una cartella da svuotare'})
    return states, problems

def missing_parents(at, rel):
    return [f for f in ancestors(rel)[:-1] if not os.path.lexists(at(f))]

def only_metadata(folder):
    # Contenuto di una cartella fatta solo di metadati rigenerati dal sistema (anche di nessuno), altrimenti None.
    with os.scandir(folder) as listing:
        entries = list(listing)
    if all(e.name.casefold() in METADATA and e.is_file(follow_symlinks=False) for e in entries):
        return entries
    return None

def count_files(root):
    # File sotto la radice, senza seguire link e junction: gli spostamenti non devono cambiarne il numero.
    # I metadati restano fuori: il Finder li crea quando vuole, e vuote li toglie.
    total, stack = 0, [root]
    while stack:
        try:
            with os.scandir(stack.pop()) as listing:
                entries = list(listing)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink() or is_link(os.stat(entry.path, follow_symlinks=False)):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False) and entry.name.casefold() not in METADATA:
                    total += 1
            except OSError:
                continue
    return total

@contextmanager
def lock(folder):
    path = Path(folder) / 'riordina.lock'
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError(f'Un altro riordino è in corso o si è interrotto: verifica e poi elimina {path}') from None
    os.write(fd, stamp().encode('ascii'))
    os.close(fd)
    try:
        yield
    finally:
        os.unlink(path)

def note(log, **entry):
    # Riga scritta su disco prima della mossa: un'interruzione non ne perde traccia.
    log.write(json.dumps({**entry, 'ora': stamp()}, ensure_ascii=False) + '\n')
    log.flush()
    os.fsync(log.fileno())

def execute(plan_path, dry_run=False):
    at, moves, empties = load_plan(plan_path)
    journal = journal_path(plan_path)
    # Mosse annotate da un'esecuzione precedente dello stesso piano: le sole che possono risultare fatte.
    entries = read_journal(journal)
    noted = {(key(e['da']), key(e['a'])) for e in entries if 'da' in e}
    copied = {(key(e['copiata']), key(e['a'])) for e in entries if 'copiata' in e}
    states, problems = check(at, moves, empties, noted, copied)
    report = {**at.describe(), 'mosse': len(moves), 'da_fare': states.count('da_fare'),
              'gia_fatte': states.count('fatta'), 'problemi': problems}
    if 'da_finire' in states:
        report['da_finire'] = states.count('da_finire')
    if problems or dry_run:
        return report, int(bool(problems))
    done, error, removed, kept, dropped = 0, None, [], [], 0
    roots = list(at.roots.values())
    with lock(plan_path.parent), open(journal, 'a', encoding='utf-8') as log:
        # Copie lasciate a metà da un'esecuzione interrotta: le origini sono intere, si ricopiano.
        for (src, dst), state in zip(moves, states):
            if state == 'da_fare' and os.path.lexists(partial(at(dst))):
                remove(partial(at(dst)))
        before = sum(count_files(r) for r in roots)
        for (src, dst), state in zip(moves, states):
            if state not in ('da_fare', 'da_finire'):
                continue
            try:
                if state == 'da_finire':
                    dropped += finish(at(src), at(dst))
                    done += 1
                    continue
                for folder in missing_parents(at, dst):
                    os.mkdir(at(folder))
                    note(log, cartella=folder)
                if crossing(at(src), at(dst)):
                    note(log, da=src, a=dst, copia=True)
                    transfer(at(src), at(dst), lambda: note(log, copiata=src, a=dst))
                else:
                    note(log, da=src, a=dst)
                    if os.path.lexists(at(dst)):
                        raise FileExistsError(f'Destinazione comparsa durante il riordino: {dst}')
                    os.rename(at(src), at(dst))
                done += 1
            except OSError as exc:
                error = {'da': src, 'a': dst, 'errore': str(exc)}
                break
        for folder in empties if error is None else ():
            # Solo cartelle rimaste vuote, a parte i metadati: una con qualcosa dentro resta e si segnala.
            if not os.path.lexists(at(folder)):
                continue
            try:
                metadata = only_metadata(at(folder))
                if metadata is None:
                    kept.append(folder)
                    continue
                note(log, rimossa=folder, metadati=sorted(e.name for e in metadata))
                try:
                    for entry in metadata:
                        os.unlink(entry.path)
                except OSError:
                    kept.append(folder)  # un metadato in uso, come un Thumbs.db aperto: la cartella resta
                    continue
                os.rmdir(at(folder))
                removed.append(folder)
            except OSError as exc:
                error = {'vuota': folder, 'errore': str(exc)}
                break
        after = sum(count_files(r) for r in roots)
    # Completare una copia toglie i doppioni rimasti nell'origine: il conteggio scende di quelli.
    count = {'prima': before, 'dopo': after, **({'avanzi_tolti': dropped} if dropped else {})}
    report.update(eseguite=done, errore=error, cartelle_rimosse=removed, cartelle_rimaste=kept,
                  conteggio=count, giornale=str(journal))
    return report, int(bool(error) or before - dropped != after)

def undo(plan_path):
    at, _, _ = load_plan(plan_path)
    journal = journal_path(plan_path)
    if not journal.is_file():
        raise ValueError(f'Nessun giornale da annullare: {journal}')
    entries = read_journal(journal)
    restored, in_place, removed, recreated, conflicts, error = 0, 0, 0, 0, [], None
    with lock(plan_path.parent):
        for entry in reversed(entries):
            if 'copiata' in entry:
                continue  # fase di una mossa tra dischi: la mossa ha la sua riga
            if 'rimossa' in entry:
                # Una cartella svuotata e tolta torna prima che i file vi rientrino.
                if not os.path.lexists(at(entry['rimossa'])):
                    try:
                        for folder in missing_parents(at, entry['rimossa']) + [entry['rimossa']]:
                            os.mkdir(at(folder))
                        recreated += 1
                    except OSError as exc:
                        error = {'vuota': entry['rimossa'], 'errore': str(exc)}
                        break
                continue
            if 'cartella' in entry:
                # Una cartella creata dal piano si toglie se è rimasta vuota, a parte i metadati.
                try:
                    metadata = only_metadata(at(entry['cartella']))
                    if metadata is not None:
                        for item in metadata:
                            os.unlink(item.path)
                        os.rmdir(at(entry['cartella']))
                        removed += 1
                except OSError:
                    pass  # già assente o riempita nel frattempo: resta dov'è
                continue
            src, dst = at(entry['da']), at(entry['a'])
            has_src, has_dst = os.path.lexists(src), os.path.lexists(dst)
            try:
                if has_src and has_dst and crossing(src, dst) and contained(dst, src):
                    # Annullamento tra dischi interrotto mentre toglieva la destinazione: si completa.
                    remove(dst)
                    restored += 1
                    continue
                if has_src and not has_dst:
                    in_place += 1  # mossa mai avvenuta o già annullata
                    continue
                if has_src == has_dst:
                    problem = 'origine e destinazione presenti' if has_src else 'origine e destinazione assenti'
                    conflicts.append({'da': entry['da'], 'a': entry['a'], 'problema': problem})
                    continue
                for folder in missing_parents(at, entry['da']):
                    os.mkdir(at(folder))
                if crossing(dst, src):
                    if os.path.lexists(partial(src)):
                        remove(partial(src))  # copia lasciata a metà da un annullamento interrotto
                    transfer(dst, src)
                else:
                    os.rename(dst, src)
                restored += 1
            except OSError as exc:
                error = {'da': entry['da'], 'a': entry['a'], 'errore': str(exc)}
                break
        if error is None and not conflicts:
            # Giornale chiuso: una nuova esecuzione ne apre uno pulito.
            journal = journal.rename(journal.with_name(f'{journal.stem}.annullato-{time.strftime("%Y%m%d-%H%M%S")}.jsonl'))
    return ({**at.describe(), 'ripristinate': restored, 'gia_al_posto': in_place, 'cartelle_rimosse': removed,
             'cartelle_ricreate': recreated, 'conflitti': conflicts, 'errore': error, 'giornale': str(journal)},
            int(bool(error or conflicts)))

def main():
    # PowerShell e percorsi Unicode: output UTF-8 anche con locale Windows legacy.
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('inventario'); p.add_argument('radice', type=Path)
    p.add_argument('--profondita', type=int); p.add_argument('--max', type=int, default=300)
    p = sub.add_parser('esegui'); p.add_argument('piano', type=Path); p.add_argument('--prova', action='store_true')
    p = sub.add_parser('annulla'); p.add_argument('piano', type=Path)
    args = parser.parse_args()
    if args.command == 'inventario':
        out = inventory(args.radice, args.profondita, args.max)
        code = int(bool(out['errori']))
    elif args.command == 'esegui':
        out, code = execute(args.piano.resolve(), args.prova)
    else:
        out, code = undo(args.piano.resolve())
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return code

if __name__ == '__main__':
    if sys.version_info < (3, 10):
        # Su macOS il python3 degli strumenti per sviluppatori di Apple è spesso la 3.9.
        print(json.dumps({'error': f'Serve Python 3.10 o successivo, non {sys.version.split()[0]}'}), file=sys.stderr)
        raise SystemExit(2)
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
