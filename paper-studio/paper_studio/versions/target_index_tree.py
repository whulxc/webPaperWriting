"""Content-addressed, structurally shared numeric and named selection tries.

Numeric lookup verifies four branches and one leaf. Named lookup uses the full
SHA256 key in a compressed trie with at most64 branches. Absent edges prove absence without
an ancestry walk. A change stores only its leaf and ancestor branches; existing
nodes are never rewritten.
"""
from __future__ import annotations

import json
import re

from paper_studio.security.policy import encoded, fail, sha

FORMAT = 'paper-studio.target-selections.radix-v1'
NAMED_FORMAT = 'paper-studio.target-selections.named-patricia-v1'
FOREST_FORMAT = 'paper-studio.target-selections.forest-v1'
ROLES = ('manuscript_pdf', 'manuscript_tex', 'figure_pdf', 'figure_source', 'review', 'provenance')
LANGUAGES = ('en', 'zh', 'shared')
HASH = re.compile(r'[0-9a-f]{64}\Z')
MAX_NODE_BYTES = 16 * 1024


def selector_path(selector):
    try:
        role, language, figure = selector.split('|')
        number = int(figure)
        if str(number) != figure or not 0 <= number <= 999:
            raise ValueError
        if (role.startswith('figure_') and (not number or language == 'shared')
                or not role.startswith('figure_') and number
                or role.startswith('manuscript_') and language == 'shared'):
            raise ValueError
        # 3 role bits, 2 language bits and 10 figure bits cannot collide.
        return f'{(ROLES.index(role) << 12) | (LANGUAGES.index(language) << 10) | number:04x}'
    except (ValueError, TypeError, AttributeError):
        fail('INDEX_TAMPERED', 'Invalid persisted selection key')


class SelectionTree:
    def __init__(self, connection, root=None, count=0):
        if type(count) is not int or count < 0 or (root is None) != (count == 0) or (
                root is not None and (not isinstance(root, str) or not HASH.fullmatch(root))):
            fail('INDEX_TAMPERED', 'Invalid selection root identity')
        self.connection = connection
        self.format = FORMAT
        self.depth = 4
        self.root = root
        self.count = count
        self._cache = {}
        self._pending = {}
        self._persisted = set()

    def __len__(self):
        return self.count

    def _read(self, digest, depth):
        if digest is None:
            return None
        if digest not in self._cache:
            row = self.connection.execute('SELECT document FROM selection_nodes WHERE digest=?', (digest,)).fetchone()
            if row is None or len(row['document'].encode()) > MAX_NODE_BYTES or sha(row['document'].encode()) != digest:
                fail('INDEX_TAMPERED', 'Selection node differs from its immutable root')
            try:
                node = json.loads(row['document'])
            except (ValueError, RecursionError):
                fail('INDEX_TAMPERED', 'Invalid selection node JSON')
            self._cache[digest] = node
            self._persisted.add(digest)
        node = self._cache[digest]
        if not isinstance(node, dict) or node.get('format') != self.format or node.get('depth') != depth:
            fail('INDEX_TAMPERED', 'Selection node depth or format changed')
        if depth == self.depth:
            if (set(node) != {'format', 'depth', 'selector', 'entry', 'count'}
                    or type(node['count']) is not int or node['count'] != 1
                    or not isinstance(node['entry'], dict)):
                fail('INDEX_TAMPERED', 'Invalid selection leaf')
        elif (set(node) != {'format', 'depth', 'children', 'count'}
              or type(node['count']) is not int or node['count'] < 1
              or not isinstance(node['children'], dict) or not 1 <= len(node['children']) <= 16
              or any(not isinstance(k, str) or not re.fullmatch('[0-9a-f]', k)
                     or not isinstance(v, str) or not HASH.fullmatch(v)
                     for k, v in node['children'].items())):
            fail('INDEX_TAMPERED', 'Invalid selection branch')
        return node

    def _save(self, node):
        raw = encoded(node).decode()
        if len(raw.encode()) > MAX_NODE_BYTES:
            fail('INDEX_LIMIT', 'A single selection node exceeds16KiB')
        digest = sha(raw.encode())
        self._cache[digest] = json.loads(raw)
        if digest not in self._persisted:
            self._pending[digest] = raw
        return digest

    def flush(self):
        """Persist only new nodes reachable from the final transaction root.

        Unchanged persisted subtrees terminate the walk immediately. Intermediate
        transaction roots are discarded in memory, without deleting any history.
        """
        visited = set()
        rows = []

        def visit(digest):
            if digest not in self._pending or digest in visited:
                return
            visited.add(digest)
            node = self._cache[digest]
            if node['depth'] < self.depth:
                for child in node['children'].values():
                    visit(child)
            rows.append((digest, self._pending[digest]))

        visit(self.root)
        self.connection.executemany('INSERT OR IGNORE INTO selection_nodes VALUES(?,?)', rows)
        self._persisted.update(visited)
        self._pending.clear()

    def get(self, key, default=None):
        path = selector_path(key)
        digest = self.root
        parent_count = self.count
        for depth in range(self.depth + 1):
            node = self._read(digest, depth)
            if node is None:
                return default
            if node['count'] > parent_count or depth == 0 and node['count'] != self.count:
                fail('INDEX_TAMPERED', 'Selection count differs from its root')
            parent_count = node['count']
            if depth == self.depth:
                if node['selector'] != key:
                    fail('INDEX_TAMPERED', 'Selection leaf differs from the exact selector')
                # Return detached metadata; callers cannot mutate cached nodes.
                return json.loads(json.dumps(node['entry']))
            digest = node['children'].get(path[depth])
        fail('INDEX_TAMPERED', 'Selection path exceeds fixed depth')

    def set(self, key, value):
        path = selector_path(key)

        def update(digest, depth):
            node = self._read(digest, depth)
            old_count = node['count'] if node else 0
            if depth == self.depth:
                if node and node['selector'] != key:
                    fail('INDEX_TAMPERED', 'Selection leaf key collision')
                if value is None:
                    return None, -old_count
                leaf = {'format': self.format, 'depth': self.depth, 'selector': key, 'entry': value, 'count': 1}
                return self._save(leaf), 1 - old_count
            children = dict(node['children']) if node else {}
            child, delta = update(children.get(path[depth]), depth + 1)
            if child is None:
                children.pop(path[depth], None)
            else:
                children[path[depth]] = child
            if not children:
                return None, -old_count
            branch = {'format': self.format, 'depth': depth, 'children': children, 'count': old_count + delta}
            return self._save(branch), delta

        if self.root is not None:
            root = self._read(self.root, 0)
            if root['count'] != self.count:
                fail('INDEX_TAMPERED', 'Selection root count changed')
        self.root, delta = update(self.root, 0)
        self.count += delta

    def delete(self, key):
        self.set(key, None)

    def items(self):
        """Explicit full-current-scope validation only; never used by get."""
        def visit(digest, depth, prefix):
            node = self._read(digest, depth)
            if node is None:
                return
            if depth == self.depth:
                key = node['selector']
                path = selector_path(key)
                if path != prefix:
                    fail('INDEX_TAMPERED', 'Selection leaf is stored under another key path')
                yield key, json.loads(json.dumps(node['entry']))
                return
            for edge, child in sorted(node['children'].items()):
                yield from visit(child, depth + 1, prefix + edge)
        yield from visit(self.root, 0, '')


class NamedSelectionTree:
    """Persistent SHA256 Patricia tree, with no stored single-child chains."""
    def __init__(self, connection, root=None, count=0):
        if (type(count) is not int or count < 0 or (root is None) != (count == 0)
                or root is not None and (not isinstance(root,str) or not HASH.fullmatch(root))):
            fail('INDEX_TAMPERED', 'Invalid named selection root')
        self.connection = connection
        self.root = root
        self.count = count
        self._cache = {}
        self._pending = {}
        self._persisted = set()

    def __len__(self):
        return self.count

    def _load(self, digest):
        if digest not in self._cache:
            row = self.connection.execute('SELECT document FROM selection_nodes WHERE digest=?',(digest,)).fetchone()
            if row is None or len(row['document'].encode()) > MAX_NODE_BYTES or sha(row['document'].encode()) != digest:
                fail('INDEX_TAMPERED', 'Named selection node differs from its immutable root')
            try:
                self._cache[digest] = json.loads(row['document'])
            except (ValueError,RecursionError):
                fail('INDEX_TAMPERED', 'Invalid named selection JSON')
            self._persisted.add(digest)
        node = self._cache[digest]
        if not isinstance(node,dict) or node.get('format') != NAMED_FORMAT or type(node.get('count')) is not int:
            fail('INDEX_TAMPERED', 'Invalid named selection format')
        if node.get('kind') == 'leaf':
            if (set(node) != {'format','kind','count','path','selector','entry'} or node['count'] != 1
                    or not isinstance(node['selector'],str) or node['path'] != sha(node['selector'].encode())
                    or not isinstance(node['entry'],dict)):
                fail('INDEX_TAMPERED', 'Invalid named selection leaf')
        elif node.get('kind') == 'branch':
            if (set(node) != {'format','kind','count','prefix','children'}
                    or not isinstance(node['prefix'],str) or not re.fullmatch('[0-9a-f]{0,63}',node['prefix'])
                    or not isinstance(node['children'],dict) or not 2 <= len(node['children']) <= 16
                    or node['count'] < len(node['children'])
                    or any(not isinstance(key,str) or not re.fullmatch('[0-9a-f]',key)
                           or not isinstance(value,str) or not HASH.fullmatch(value)
                           for key,value in node['children'].items())):
                fail('INDEX_TAMPERED', 'Invalid named selection branch')
        else:
            fail('INDEX_TAMPERED', 'Unknown named selection node')
        return node

    def _save(self, node):
        raw = encoded(node).decode()
        if len(raw.encode()) > MAX_NODE_BYTES:
            fail('INDEX_LIMIT', 'A named selection node exceeds16KiB')
        digest = sha(raw.encode())
        self._cache[digest] = json.loads(raw)
        if digest not in self._persisted:
            self._pending[digest] = raw
        return digest

    def get(self, key, default=None):
        path = sha(key.encode())
        digest = self.root
        required_prefix = ''
        parent_count = self.count
        for _ in range(65):
            if digest is None:
                return default
            node = self._load(digest)
            prefix = node['path'] if node['kind'] == 'leaf' else node['prefix']
            if not prefix.startswith(required_prefix) or node['count'] > parent_count:
                fail('INDEX_TAMPERED', 'Named selection node is outside its parent prefix')
            if digest == self.root and node['count'] != self.count:
                fail('INDEX_TAMPERED', 'Named selection root count changed')
            parent_count = node['count']
            if node['kind'] == 'leaf':
                if prefix != path:
                    return default
                if node['selector'] != key:
                    fail('INDEX_TAMPERED', 'Named selection digest collision')
                return json.loads(json.dumps(node['entry']))
            if not path.startswith(prefix):
                return default
            required_prefix = prefix + path[len(prefix)]
            digest = node['children'].get(path[len(prefix)])
        fail('INDEX_TAMPERED', 'Named selection exceeds its bounded key depth')

    def set(self, key, value):
        path = sha(key.encode())

        def leaf():
            return self._save({'format':NAMED_FORMAT,'kind':'leaf','count':1,'path':path,'selector':key,'entry':value})

        def update(digest):
            if digest is None:
                if value is None:
                    return None,0
                return leaf(),1
            node = self._load(digest)
            prefix = node['path'] if node['kind'] == 'leaf' else node['prefix']
            shared = 0
            while shared < len(prefix) and path[shared] == prefix[shared]:
                shared += 1
            if shared < len(prefix):
                if value is None:
                    return digest,0
                branch = {'format':NAMED_FORMAT,'kind':'branch','count':node['count']+1,
                          'prefix':prefix[:shared], 'children':{prefix[shared]:digest,path[shared]:leaf()}}
                return self._save(branch),1
            if node['kind'] == 'leaf':
                if node['selector'] != key:
                    fail('INDEX_TAMPERED', 'Named selection digest collision')
                if value is None:
                    return None,-1
                return leaf(),0
            children = dict(node['children'])
            edge = path[len(prefix)]
            child,delta = update(children.get(edge))
            if child is None:
                children.pop(edge,None)
            else:
                children[edge] = child
            if len(children) == 1:
                return next(iter(children.values())),delta
            if not children:
                return None,-node['count']
            branch = {'format':NAMED_FORMAT,'kind':'branch','count':node['count']+delta,
                      'prefix':prefix,'children':children}
            return self._save(branch),delta

        if self.root is not None and self._load(self.root)['count'] != self.count:
            fail('INDEX_TAMPERED', 'Named selection root count changed')
        self.root,delta = update(self.root)
        self.count += delta

    def delete(self, key):
        self.set(key,None)

    def flush(self):
        visited = set()
        rows = []

        def visit(digest):
            if digest not in self._pending or digest in visited:
                return
            visited.add(digest)
            node = self._cache[digest]
            if node['kind'] == 'branch':
                for child in node['children'].values():
                    visit(child)
            rows.append((digest,self._pending[digest]))

        visit(self.root)
        self.connection.executemany('INSERT OR IGNORE INTO selection_nodes VALUES(?,?)',rows)
        self._persisted.update(visited)
        self._pending.clear()

    def items(self):
        def visit(digest,prefix):
            if digest is None:
                return
            node = self._load(digest)
            actual = node['path'] if node['kind'] == 'leaf' else node['prefix']
            if not actual.startswith(prefix):
                fail('INDEX_TAMPERED', 'Named selection is outside its parent prefix')
            if node['kind'] == 'leaf':
                yield node['selector'],json.loads(json.dumps(node['entry']))
                return
            for edge,child in sorted(node['children'].items()):
                yield from visit(child,actual+edge)

        yield from visit(self.root,'')


class SelectionForest:
    """Preserve old numeric roots; named selectors have a separate hash trie."""
    def __init__(self, connection, roots=None):
        roots = roots or {'numeric': {'root': None, 'count': 0}, 'named': {'root': None, 'count': 0}}
        if not isinstance(roots, dict) or set(roots) != {'numeric', 'named'}:
            fail('INDEX_TAMPERED', 'Invalid selection forest')
        for branch in roots.values():
            if not isinstance(branch, dict) or set(branch) != {'root', 'count'}:
                fail('INDEX_TAMPERED', 'Invalid selection forest branch')
        self.numeric = SelectionTree(connection, **roots['numeric'])
        self.named = NamedSelectionTree(connection, **roots['named'])

    @property
    def _cache(self):
        return {**self.numeric._cache, **self.named._cache}

    @property
    def roots(self):
        return {'numeric': {'root': self.numeric.root, 'count': len(self.numeric)},
                'named': {'root': self.named.root, 'count': len(self.named)}}

    def __len__(self):
        return len(self.numeric) + len(self.named)

    def _tree(self, key):
        return self.named if key.rsplit('|', 1)[-1].startswith('@') else self.numeric

    def get(self, key, default=None):
        return self._tree(key).get(key, default)

    def set(self, key, value):
        self._tree(key).set(key, value)

    def delete(self, key):
        self._tree(key).delete(key)

    def flush(self):
        self.numeric.flush()
        self.named.flush()

    def items(self):
        yield from self.numeric.items()
        yield from self.named.items()
