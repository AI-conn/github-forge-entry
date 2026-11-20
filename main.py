#!/usr/bin/env python3
"""Forge entry: a small, deterministic GitHub-core clone.

Implements the four surfaces of the forge-01 trial:
  - git smart HTTP (protocol v0 + v2) at /{owner}/{repo}.git
  - REST v3 under /api/v3
  - GraphQL at /api/graphql
  - MCP (JSON-RPC over streamable HTTP) at /mcp

Pure Python standard library, one process, in-memory store. No network calls
except the trial lifecycle (register / done polling against MAKEFUN_ENV_URL).
All timestamps derive from MAKEFUN_TRIAL_NOW plus a per-object sequence
counter, so a fixed request sequence always produces the same bytes.
"""

import base64
import hashlib
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

# --------------------------------------------------------------------------
# Constants and trial environment

VIEWER_LOGIN = "adaiversen"
VIEWER_EMAIL = "adaiversen@example.invalid"
ZERO_OID = "0" * 40
DEFAULT_BRANCH = "main"
SERVER_VERSION_STRING = "forge-1.0.0"

TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
TRIAL_NOW_RAW = os.environ.get("MAKEFUN_TRIAL_NOW", "")


def _parse_trial_now(raw):
    if raw:
        text = raw.strip()
        try:
            if text.endswith("Z"):
                dt = datetime.fromisoformat(text[:-1] + "+00:00")
            else:
                dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            pass
        try:
            return int(float(text))
        except ValueError:
            pass
    return 1735689600  # 2025-01-01T00:00:00Z, the scenario epoch


BASE_EPOCH = _parse_trial_now(TRIAL_NOW_RAW)


def iso_utc(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Git object layer

OBJ_TYPES = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
TYPE_NAMES = {1: "commit", 2: "tree", 3: "blob", 4: "tag", 6: "ofs-delta", 7: "ref-delta"}


def hash_object(type_name, data):
    header = ("%s %d" % (type_name, len(data))).encode("ascii")
    return hashlib.sha1(header + b"\x00" + data).hexdigest()


def parse_commit(data):
    """Return {'tree': hex, 'parents': [hex], 'message': str}."""
    tree = None
    parents = []
    message = ""
    head, _, body = data.partition(b"\n\n")
    message = body.decode("utf-8", "replace")
    for line in head.split(b"\n"):
        if line.startswith(b"tree "):
            tree = line[5:].decode("ascii")
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode("ascii"))
    return {"tree": tree, "parents": parents, "message": message}


def parse_tree_entries(data):
    """Return [(mode_str, name, hex_sha)] in stored order."""
    entries = []
    pos = 0
    while pos < len(data):
        sp = data.index(b" ", pos)
        mode = data[pos:sp].decode("ascii")
        nul = data.index(b"\x00", sp)
        name = data[sp + 1:nul].decode("utf-8", "surrogateescape")
        sha = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, sha))
        pos = nul + 21
    return entries


def build_tree_data(entries):
    """entries: iterable of (mode_str, name, hex_sha); sorts the git way."""
    def sort_key(item):
        mode, name, _ = item
        return name + ("/" if mode == "40000" else "")
    out = bytearray()
    for mode, name, sha in sorted(entries, key=sort_key):
        out += mode.encode("ascii") + b" " + name.encode("utf-8", "surrogateescape") + b"\x00" + bytes.fromhex(sha)
    return bytes(out)


class Repo:
    def __init__(self, repo_id, owner, name, private, description):
        self.id = repo_id
        self.owner = owner
        self.name = name
        self.private = bool(private)
        self.description = description or ""
        self.default_branch = DEFAULT_BRANCH
        self.objects = {}          # hex sha -> (type_name, data bytes)
        self.refs = {}             # 'refs/heads/x' -> hex sha
        self.labels = {}           # name -> {'name':..., 'color':...}
        self.issues = {}           # number -> issue dict
        self.pulls = {}            # number -> pull dict
        self.next_number = 1       # issues and pulls share one sequence
        self.next_comment_id = 1

    # -- object store ------------------------------------------------------
    def store_object(self, type_name, data):
        sha = hash_object(type_name, data)
        if sha not in self.objects:
            self.objects[sha] = (type_name, data)
        return sha

    def get_object(self, sha):
        return self.objects.get(sha)

    def store_blob(self, content_bytes):
        return self.store_object("blob", content_bytes)

    # -- trees -------------------------------------------------------------
    def tree_to_map(self, tree_sha, prefix=""):
        """Flatten a tree object into {path: blob_sha}."""
        result = {}
        if tree_sha is None:
            return result
        obj = self.get_object(tree_sha)
        if obj is None or obj[0] != "tree":
            return result
        for mode, name, sha in parse_tree_entries(obj[1]):
            path = prefix + name
            if mode == "40000":
                result.update(self.tree_to_map(sha, path + "/"))
            else:
                result[path] = sha
        return result

    def map_to_tree(self, flat):
        """Store nested tree objects for {path: blob_sha}; return root tree sha."""
        root = {}
        for path, sha in flat.items():
            parts = path.split("/")
            node = root
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = sha

        def emit(node):
            entries = []
            for name, value in node.items():
                if isinstance(value, dict):
                    entries.append(("40000", name, emit(value)))
                else:
                    entries.append(("100644", name, value))
            return self.store_object("tree", build_tree_data(entries))

        return emit(root)

    def tree_at(self, commit_sha):
        obj = self.get_object(commit_sha)
        if obj is None or obj[0] != "commit":
            return None
        return parse_commit(obj[1])["tree"]

    # -- commits -----------------------------------------------------------
    def make_commit(self, tree_sha, parents, message, epoch):
        lines = ["tree %s" % tree_sha]
        for parent in parents:
            lines.append("parent %s" % parent)
        ident = "%s <%s> %d +0000" % (VIEWER_LOGIN, VIEWER_EMAIL, epoch)
        lines.append("author " + ident)
        lines.append("committer " + ident)
        data = ("\n".join(lines) + "\n\n" + message).encode("utf-8")
        if not data.endswith(b"\n"):
            data += b"\n"
        return self.store_object("commit", data)

    def commit_parents(self, sha):
        obj = self.get_object(sha)
        if obj is None or obj[0] != "commit":
            return []
        return parse_commit(obj[1])["parents"]

    def is_ancestor(self, anc, desc):
        """True when anc is desc itself or reachable from desc via parents."""
        if anc == desc:
            return True
        seen = set()
        stack = [desc]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if cur == anc:
                return True
            stack.extend(self.commit_parents(cur))
        return False

    def merge_base(self, a, b):
        """Best common ancestor of two commits (BFS from both sides)."""
        if a is None or b is None:
            return None
        anc_a = {}
        queue = [(a, 0)]
        seen = {a}
        while queue:
            cur, dist = queue.pop(0)
            anc_a[cur] = dist
            for parent in self.commit_parents(cur):
                if parent not in seen:
                    seen.add(parent)
                    queue.append((parent, dist + 1))
        queue = [(b, 0)]
        seen = {b}
        while queue:
            cur, dist = queue.pop(0)
            if cur in anc_a:
                return cur
            for parent in self.commit_parents(cur):
                if parent not in seen:
                    seen.add(parent)
                    queue.append((parent, dist + 1))
        return None

    def reachable_commits(self, tips):
        """All commit shas reachable from the given tips."""
        seen = set()
        stack = [t for t in tips if t]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            obj = self.get_object(cur)
            if obj is None or obj[0] != "commit":
                continue
            seen.add(cur)
            stack.extend(parse_commit(obj[1])["parents"])
        return seen


# --------------------------------------------------------------------------
# Packfiles

def pack_object_header(type_num, size):
    first = (type_num << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    if size:
        first |= 0x80
    out.append(first)
    while size:
        byte = size & 0x7F
        size >>= 7
        if size:
            byte |= 0x80
        out.append(byte)
    return bytes(out)


def build_pack(objects):
    """objects: list of (type_name, data). Returns a complete pack (v2).

    HARDEN: zlib level 1 — pack readers accept any level; level 6 CPU was
    pure latency on every fetch/clone. Output stays deterministic (same
    input -> same bytes), it is just a different valid encoding.
    """
    out = bytearray()
    out += b"PACK" + struct.pack(">II", 2, len(objects))
    for type_name, data in objects:
        out += pack_object_header(OBJ_TYPES[type_name], len(data))
        out += zlib.compress(data, 1)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


# HARDEN: pack cache. Objects are immutable and content-addressed, so the
# pack for a fixed ordered object list is a pure function of that list.
# Concurrent clones/fetches of the same history re-serve identical packs;
# building each from scratch (hash + deflate per object) wasted the CPU
# the clone workload measures. Bounded to the last few packs.
_PACK_CACHE = {}
_PACK_CACHE_LOCK = threading.Lock()
_PACK_CACHE_MAX = 8


def build_pack_cached(ordered):
    """ordered: list of (sha, (type_name, data)) in pack order."""
    key = tuple(sha for sha, _obj in ordered)
    with _PACK_CACHE_LOCK:
        hit = _PACK_CACHE.get(key)
    if hit is not None:
        return hit
    pack = build_pack([(type_name, data) for _sha, (type_name, data) in ordered])
    with _PACK_CACHE_LOCK:
        if len(_PACK_CACHE) >= _PACK_CACHE_MAX:
            _PACK_CACHE.clear()
        _PACK_CACHE[key] = pack
    return pack


def apply_delta(base, delta):
    pos = 0

    def read_varint():
        nonlocal pos
        value = 0
        shift = 0
        while True:
            byte = delta[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        return value

    src_size = read_varint()
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    read_varint()  # target size
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            offset = 0
            length = 0
            for i in range(4):
                if op & (1 << i):
                    offset |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    length |= delta[pos] << (8 * i)
                    pos += 1
            if length == 0:
                length = 0x10000
            out += base[offset:offset + length]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError("delta opcode 0")
    return bytes(out)


def read_pack(data, repo):
    """Parse pack bytes; store every object into repo. Returns object count."""
    if len(data) < 12 + 20 or data[:4] != b"PACK":
        raise ValueError("not a pack")
    count = struct.unpack(">I", data[8:12])[0]
    pos = 12
    entries = []  # {start, kind: 'plain'|'ofs'|'ref', type_name, base, raw}
    for _ in range(count):
        start = pos
        byte = data[pos]
        pos += 1
        type_num = (byte >> 4) & 0x07
        size = byte & 0x0F
        shift = 4
        while byte & 0x80:
            byte = data[pos]
            pos += 1
            size |= (byte & 0x7F) << shift
            shift += 7
        base = None
        kind = "plain"
        if type_num == 6:  # ofs-delta: base addressed by negative pack offset
            byte = data[pos]
            pos += 1
            offset = byte & 0x7F
            while byte & 0x80:
                byte = data[pos]
                pos += 1
                offset = ((offset + 1) << 7) | (byte & 0x7F)
            base = start - offset
            kind = "ofs"
        elif type_num == 7:  # ref-delta: base addressed by sha
            base = data[pos:pos + 20].hex()
            pos += 20
            kind = "ref"
        decomp = zlib.decompressobj()
        raw = decomp.decompress(data[pos:])
        pos += len(data[pos:]) - len(decomp.unused_data)
        if len(raw) != size:
            raise ValueError("pack object size mismatch")
        entries.append({
            "start": start,
            "kind": kind,
            "type_name": TYPE_NAMES[type_num] if kind == "plain" else None,
            "base": base,
            "raw": raw,
        })
    by_start = {entry["start"]: index for index, entry in enumerate(entries)}
    resolved = {}

    def resolve(index):
        if index in resolved:
            return resolved[index]
        entry = entries[index]
        if entry["kind"] == "plain":
            result = (entry["type_name"], entry["raw"])
        else:
            if entry["kind"] == "ref":
                base_sha = entry["base"]
                base_obj = repo.get_object(base_sha)
                if base_obj is None:
                    for other_index, other in enumerate(entries):
                        if other["kind"] == "plain" and hash_object(other["type_name"], other["raw"]) == base_sha:
                            base_obj = resolve(other_index)
                            break
                if base_obj is None:
                    raise ValueError("ref-delta base missing")
            else:
                base_obj = resolve(by_start[entry["base"]])
            result = (base_obj[0], apply_delta(base_obj[1], entry["raw"]))
        resolved[index] = result
        return result

    for index in range(len(entries)):
        type_name, raw = resolve(index)
        repo.store_object(type_name, raw)
    return len(entries)


# --------------------------------------------------------------------------
# Global store

class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.repos = {}            # name -> Repo (all owned by the viewer)
        self.next_repo_id = 5000
        self.next_global_id = 100000
        self.commit_seq = 0
        self.request_seq = 0

    def create_repo(self, name, private, description):
        repo = Repo(self.next_repo_id, VIEWER_LOGIN, name, private, description)
        self.next_repo_id += 1
        self.repos[name] = repo
        return repo

    def global_id(self):
        self.next_global_id += 1
        return self.next_global_id

    def next_epoch(self):
        self.commit_seq += 1
        return BASE_EPOCH + self.commit_seq

    def request_id(self):
        self.request_seq += 1
        return "%016x" % self.request_seq


STORE = Store()


def get_repo(owner, name):
    if owner != VIEWER_LOGIN:
        return None
    return STORE.repos.get(name)


# --------------------------------------------------------------------------
# pkt-line framing

def pkt_line(payload):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return ("%04x" % (len(payload) + 4)).encode("ascii") + payload


PKT_FLUSH = b"0000"
PKT_DELIM = b"0001"


def parse_pkt_lines(body):
    """Parse a pkt-line stream into (payloads, rest_after_flush, ok).

    payloads excludes the terminating flush; rest is whatever followed it
    (the raw pack in a receive-pack request)."""
    payloads = []
    pos = 0
    while pos + 4 <= len(body):
        try:
            length = int(body[pos:pos + 4], 16)
        except ValueError:
            return payloads, b"", False
        if length == 0:
            return payloads, body[pos + 4:], True
        if length == 1:
            payloads.append(b"\x01")  # delim marker
            pos += 4
            continue
        if length < 4 or pos + length > len(body):
            return payloads, b"", False
        payloads.append(body[pos + 4:pos + length])
        pos += length
    return payloads, b"", pos == len(body)


def parse_pkt_stream(body):
    """Parse an entire pkt-line request body: data payloads in order, with
    b"\\x01" markers for delims and None for flushes."""
    out = []
    pos = 0
    while pos + 4 <= len(body):
        try:
            length = int(body[pos:pos + 4], 16)
        except ValueError:
            break
        if length == 0:
            out.append(None)
            pos += 4
            continue
        if length == 1:
            out.append(b"\x01")
            pos += 4
            continue
        if length < 4 or pos + length > len(body):
            break
        out.append(body[pos + 4:pos + length])
        pos += length
    return out


# --------------------------------------------------------------------------
# Fetch computation

def commit_closure_with_trees(repo, tips, stop_commits):
    """Objects (commits + trees + blobs) reachable from tips, without
    descending into stop_commits (commits the client already fully holds)."""
    objects = {}
    stack = [t for t in tips if t]
    seen_commits = set()
    while stack:
        sha = stack.pop()
        if sha in seen_commits or sha in stop_commits:
            continue
        obj = repo.get_object(sha)
        if obj is None or obj[0] != "commit":
            continue
        seen_commits.add(sha)
        objects[sha] = obj
        parsed = parse_commit(obj[1])
        if parsed["tree"]:
            collect_tree(repo, parsed["tree"], objects)
        stack.extend(parsed["parents"])
    return objects


def collect_tree(repo, tree_sha, objects):
    if tree_sha in objects:
        return
    obj = repo.get_object(tree_sha)
    if obj is None or obj[0] != "tree":
        return
    objects[tree_sha] = obj
    for mode, _name, sha in parse_tree_entries(obj[1]):
        if mode == "40000":
            collect_tree(repo, sha, objects)
        else:
            blob = repo.get_object(sha)
            if blob is not None:
                objects[sha] = blob


def tag_chain(repo, sha):
    """Follow an annotated-tag chain; return (tag_shas, final_target_sha)."""
    chain = []
    seen = set()
    cur = sha
    while cur not in seen:
        seen.add(cur)
        obj = repo.get_object(cur)
        if obj is None or obj[0] != "tag":
            break
        chain.append(cur)
        target = None
        for line in obj[1].split(b"\n"):
            if line.startswith(b"object "):
                target = line[len(b"object "):].decode("ascii", "replace")
                break
            if line == b"":
                break
        if target is None:
            break
        cur = target
    return chain, cur


def compute_fetch(repo, wants, haves, shallow_lines, depth):
    """Return (objects_to_send, shallow_list, unshallow_list).

    Mirrors the client's view: `shallow_lines` are commits the client holds
    without their parents; `haves` are commits whose history the client
    holds, except that traversal stops at shallow boundary commits.
    """
    shallow_set = set(shallow_lines)
    # Expand tag wants: tag objects are delivered, and the walk starts from
    # the commit they ultimately point at.
    extra_tags = set()
    commit_wants = []
    for want in wants:
        chain, target = tag_chain(repo, want)
        extra_tags.update(chain)
        obj = repo.get_object(target)
        if obj is not None and obj[0] == "commit":
            commit_wants.append(target)
    wants = commit_wants
    # What the client already holds: walk haves, but do not descend past a
    # shallow boundary commit (the client does not have its parents).
    held_commits = set()
    stack = [h for h in haves if h]
    while stack:
        cur = stack.pop()
        if cur in held_commits:
            continue
        obj = repo.get_object(cur)
        if obj is None or obj[0] != "commit":
            continue
        held_commits.add(cur)
        if cur in shallow_set:
            continue
        stack.extend(parse_commit(obj[1])["parents"])
    held_objects = set()
    for sha in held_commits:
        held_objects.add(sha)
        tree = repo.tree_at(sha)
        if tree:
            tree_objs = {}
            collect_tree(repo, tree, tree_objs)
            held_objects.update(tree_objs.keys())

    if depth is None:
        send = commit_closure_with_trees(repo, wants, held_commits - shallow_set)
        # Commits on the client's shallow boundary that this fetch completes
        # (their parents are now sent or held) stop being shallow.
        unshallow = []
        for boundary in shallow_set:
            parents = repo.commit_parents(boundary)
            if all(p in send or p in held_commits for p in parents):
                if boundary in held_commits:
                    unshallow.append(boundary)
        for sha in list(send.keys()):
            if sha in held_objects:
                del send[sha]
        add_fetch_tags(repo, send, extra_tags, held_objects)
        return send, [], unshallow

    # Depth-limited: BFS by generation from the wants.
    generations = {}
    queue = []
    for want in wants:
        if want and repo.get_object(want) is not None:
            generations[want] = 0
            queue.append(want)
    index = 0
    while index < len(queue):
        cur = queue[index]
        index += 1
        gen = generations[cur]
        if gen + 1 >= depth:
            continue
        for parent in repo.commit_parents(cur):
            if parent not in generations and repo.get_object(parent) is not None:
                generations[parent] = gen + 1
                queue.append(parent)
    included = {sha for sha, gen in generations.items() if gen < depth}
    send = {}
    for sha in included:
        obj = repo.get_object(sha)
        if obj is None:
            continue
        if sha not in held_objects:
            send[sha] = obj
        tree = repo.tree_at(sha)
        if tree:
            tree_objs = {}
            collect_tree(repo, tree, tree_objs)
            for t_sha, t_obj in tree_objs.items():
                if t_sha not in held_objects:
                    send[t_sha] = t_obj
    shallow_list = []
    for sha in included:
        parents = repo.commit_parents(sha)
        if any(p not in included for p in parents):
            shallow_list.append(sha)
    unshallow = []
    for boundary in shallow_set:
        if boundary in included:
            parents = repo.commit_parents(boundary)
            if all(p in included for p in parents):
                unshallow.append(boundary)
    add_fetch_tags(repo, send, extra_tags, held_objects)
    return send, sorted(shallow_list), unshallow


def add_fetch_tags(repo, send, extra_tags, held_objects):
    """Add tag objects to a fetch result: tags named in the wants, plus any
    ref's tag chain whose target commit is being sent."""
    sent_commits = {sha for sha, obj in send.items() if obj[0] == "commit"}
    tags = set(extra_tags)
    for _ref, ref_sha in repo.refs.items():
        chain, target = tag_chain(repo, ref_sha)
        if chain and target in sent_commits:
            tags.update(chain)
    for sha in tags:
        if sha in held_objects or sha in send:
            continue
        obj = repo.get_object(sha)
        if obj is not None:
            send[sha] = obj


# --------------------------------------------------------------------------
# Git smart HTTP

UPLOAD_CAPS_V0 = (
    "multi_ack_detailed side-band-64k thin-pack ofs-delta shallow "
    "symref=HEAD:refs/heads/%s agent=forge/%s" % (DEFAULT_BRANCH, SERVER_VERSION_STRING)
)
RECEIVE_CAPS_V0 = (
    "report-status side-band-64k delete-refs ofs-delta "
    "symref=HEAD:refs/heads/%s agent=forge/%s" % (DEFAULT_BRANCH, SERVER_VERSION_STRING)
)
V2_CAPS = [
    "version 2",
    "agent=forge/%s" % SERVER_VERSION_STRING,
    "ls-refs=unborn",
    "fetch=shallow wait-for-done",
    "object-format=sha1",
]


def sorted_refs(repo):
    return sorted(repo.refs.items())


def peeled_oid(repo, sha):
    """Follow a tag chain to the commit it names; None when not a tag."""
    obj = repo.get_object(sha)
    if obj is None or obj[0] != "tag":
        return None
    for line in obj[1].split(b"\n"):
        if line.startswith(b"object "):
            target = line[7:].decode("ascii")
            deeper = peeled_oid(repo, target)
            return deeper if deeper is not None else target
    return None


def advertisement_v0(repo, service):
    caps = UPLOAD_CAPS_V0 if service == "git-upload-pack" else RECEIVE_CAPS_V0
    out = bytearray()
    out += pkt_line("# service=%s\n" % service)
    out += PKT_FLUSH
    head_target = "refs/heads/%s" % repo.default_branch
    head_sha = repo.refs.get(head_target)
    lines = []
    if head_sha:
        lines.append("%s HEAD" % head_sha)
    for ref, sha in sorted_refs(repo):
        lines.append("%s %s" % (sha, ref))
        peeled = peeled_oid(repo, sha)
        if peeled is not None:
            lines.append("%s %s^{}" % (peeled, ref))
    if not lines:
        out += pkt_line(("%s capabilities^{}" % ZERO_OID).encode("ascii") + b"\x00" + caps.encode("ascii") + b"\n")
    else:
        for i, line in enumerate(lines):
            if i == 0:
                out += pkt_line(line.encode("ascii") + b"\x00" + caps.encode("ascii") + b"\n")
            else:
                out += pkt_line(line + "\n")
    out += PKT_FLUSH
    return bytes(out)


def advertisement_v2():
    out = bytearray()
    for line in V2_CAPS:
        out += pkt_line(line + "\n")
    out += PKT_FLUSH
    return bytes(out)


def handle_ls_refs(repo, body):
    payloads = [p for p in parse_pkt_stream(body) if p is not None]
    args = []
    prefixes = []
    for payload in payloads:
        if payload == b"\x01":
            continue
        text = payload.decode("utf-8", "replace").rstrip("\n")
        if text.startswith("ref-prefix "):
            prefixes.append(text[len("ref-prefix "):])
        elif text in ("peel", "symrefs", "unborn"):
            args.append(text)
    want_peel = "peel" in args
    want_symrefs = "symrefs" in args
    want_unborn = "unborn" in args

    def keep(name):
        return not prefixes or any(name.startswith(p) for p in prefixes)

    out = bytearray()
    head_target = "refs/heads/%s" % repo.default_branch
    head_sha = repo.refs.get(head_target)
    if keep("HEAD"):
        if head_sha:
            line = "%s HEAD" % head_sha
            if want_symrefs:
                line += " symref-target:%s" % head_target
            out += pkt_line(line + "\n")
        elif want_unborn:
            line = "unborn HEAD"
            if want_symrefs:
                line += " symref-target:%s" % head_target
            out += pkt_line(line + "\n")
    for ref, sha in sorted_refs(repo):
        if not keep(ref):
            continue
        line = "%s %s" % (sha, ref)
        if want_peel:
            peeled = peeled_oid(repo, sha)
            if peeled is not None:
                line += " peeled:%s" % peeled
        out += pkt_line(line + "\n")
    out += PKT_FLUSH
    return bytes(out)


def sideband_wrap(pack, chunk=60000):
    out = bytearray()
    for pos in range(0, len(pack), chunk):
        piece = pack[pos:pos + chunk]
        out += pkt_line(b"\x01" + piece)
    return bytes(out)


def handle_fetch(repo, body, version):
    """Shared v0/v2 fetch: parse negotiation lines, answer with a pack."""
    payloads = [p for p in parse_pkt_stream(body) if p is not None]
    wants = []
    haves = []
    shallow_lines = []
    depth = None
    done = False
    section = 0  # v0: wants section first, then negotiation after the flush
    for payload in payloads:
        if payload == b"\x01":
            continue
        text = payload.decode("utf-8", "replace").rstrip("\n")
        if version == 2:
            if text.startswith("want "):
                wants.append(text[5:].strip())
            elif text.startswith("have "):
                haves.append(text[5:].strip())
            elif text.startswith("shallow "):
                shallow_lines.append(text[8:].strip())
            elif text.startswith("deepen "):
                try:
                    depth = int(text[7:].strip())
                except ValueError:
                    pass
            elif text == "done":
                done = True
        else:
            if text.startswith("want "):
                wants.append(text[5:].split(" ")[0].strip())
            elif text.startswith("have "):
                haves.append(text[5:].strip())
            elif text.startswith("shallow "):
                shallow_lines.append(text[8:].strip())
            elif text.startswith("deepen "):
                try:
                    depth = int(text[7:].strip())
                except ValueError:
                    pass
            elif text == "done":
                done = True
            section += 0  # v0 sections are positional; line prefixes suffice
    send, shallow_list, unshallow_list = compute_fetch(repo, wants, haves, shallow_lines, depth)
    ordered = sorted(send.items(), key=lambda kv: ({"commit": 0, "tree": 1, "blob": 2, "tag": 3}[kv[1][0]], kv[0]))
    pack = build_pack_cached(ordered)
    if version == 2:
        out = bytearray()
        if not done:
            out += pkt_line("acknowledgments\n")
            out += pkt_line("NAK\n")
            out += PKT_FLUSH
        if shallow_list or unshallow_list:
            out += pkt_line("shallow-info\n")
            for sha in shallow_list:
                out += pkt_line("shallow %s\n" % sha)
            for sha in unshallow_list:
                out += pkt_line("unshallow %s\n" % sha)
            out += PKT_FLUSH
        out += pkt_line("packfile\n")
        out += sideband_wrap(pack)
        out += PKT_FLUSH
        return bytes(out)
    out = bytearray()
    for sha in shallow_list:
        out += pkt_line("shallow %s\n" % sha)
    for sha in unshallow_list:
        out += pkt_line("unshallow %s\n" % sha)
    out += pkt_line("NAK\n")
    out += sideband_wrap(pack)
    out += PKT_FLUSH
    return bytes(out)


def handle_receive_pack(repo, body):
    payloads, rest, ok = parse_pkt_lines(body)
    if not ok:
        return b"", False
    commands = []
    client_caps = ""
    for i, payload in enumerate(payloads):
        if payload == b"\x01":
            continue
        text = payload.decode("utf-8", "replace").rstrip("\n")
        if i == 0 and "\x00" in text:
            text, client_caps = text.split("\x00", 1)
        parts = text.split(" ")
        if len(parts) == 3:
            commands.append((parts[0], parts[1], parts[2]))
    use_sideband = "side-band-64k" in client_caps
    unpack_ok = True
    unpack_detail = "ok"
    if rest:
        try:
            read_pack(rest, repo)
        except Exception:
            unpack_ok = False
            unpack_detail = "unpacker error"
    results = []
    with STORE.lock:
        for old, new, ref in commands:
            if not ref.startswith("refs/"):
                results.append((ref, False, "bad ref"))
                continue
            current = repo.refs.get(ref, ZERO_OID)
            if current != old:
                results.append((ref, False, "stale ref"))
                continue
            if new == ZERO_OID:
                repo.refs.pop(ref, None)
                results.append((ref, True, ""))
                continue
            obj = repo.get_object(new)
            if obj is None:
                results.append((ref, False, "missing object"))
                continue
            if old != ZERO_OID and obj[0] == "commit":
                if not repo.is_ancestor(old, new):
                    results.append((ref, False, "non-fast-forward"))
                    continue
            repo.refs[ref] = new
            results.append((ref, True, ""))
    report = bytearray()
    report += pkt_line("unpack %s\n" % unpack_detail)
    for ref, ok_ref, reason in results:
        if ok_ref:
            report += pkt_line("ok %s\n" % ref)
        else:
            report += pkt_line("ng %s %s\n" % (ref, reason))
    report += PKT_FLUSH
    if use_sideband:
        wrapped = bytearray()
        # The report pkt-line stream (which itself ends in a flush) rides
        # inside band-1 packets, AND a flush at the outer pkt-line level
        # ends the sideband stream. RECONCILE PATCH: the original omitted
        # the outer flush, so a strict demultiplexer (real git) never saw
        # the sideband stream end and reported "unexpected disconnect
        # while reading sideband packet" even though the ref was updated
        # and the report had parsed. Both terminators are required.
        raw = bytes(report)
        pos = 0
        while pos < len(raw):
            piece = raw[pos:pos + 60000]
            wrapped += pkt_line(b"\x01" + piece)
            pos += len(piece)
        wrapped += PKT_FLUSH
        return bytes(wrapped), unpack_ok
    return bytes(report), unpack_ok


# --------------------------------------------------------------------------
# REST v3

class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def repo_json(repo):
    return {
        "id": repo.id,
        "name": repo.name,
        "full_name": "%s/%s" % (repo.owner, repo.name),
        "private": repo.private,
        "description": repo.description,
        "owner": {"login": repo.owner},
        "default_branch": repo.default_branch,
    }


def issue_json(repo, issue):
    return {
        "id": issue["id"],
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "state": issue["state"],
        "labels": [{"name": name} for name in issue["labels"]],
        "created_at": issue["created_at"],
        "updated_at": issue["updated_at"],
    }


def ref_tip(repo, branch):
    if branch is None:
        return None
    return repo.refs.get("refs/heads/" + branch)


def pull_json(repo, pull):
    head_sha = ref_tip(repo, pull["head"]) or ZERO_OID
    base_sha = ref_tip(repo, pull["base"]) or ZERO_OID
    return {
        "id": pull["id"],
        "number": pull["number"],
        "state": pull["state"],
        "title": pull["title"],
        "body": pull["body"],
        "head": {"ref": pull["head"], "sha": head_sha},
        "base": {"ref": pull["base"], "sha": base_sha},
        "merged": pull["merged"],
        "merged_at": pull["merged_at"],
        "merge_commit_sha": pull["merge_commit_sha"],
        "created_at": pull["created_at"],
        "updated_at": pull["updated_at"],
    }


def resolve_ref(repo, ref):
    """Resolve a branch name or sha to a commit sha (or None)."""
    if ref is None:
        return repo.refs.get("refs/heads/" + repo.default_branch)
    if ref in repo.refs:
        return repo.refs[ref]
    candidate = repo.refs.get("refs/heads/" + ref)
    if candidate:
        return candidate
    obj = repo.get_object(ref)
    if obj is not None and obj[0] == "commit":
        return ref
    return None


def paginate(items, query):
    try:
        per_page = int(query.get("per_page", ["30"])[0])
    except ValueError:
        per_page = 30
    per_page = max(1, min(per_page, 100))
    try:
        page = int(query.get("page", ["1"])[0])
    except ValueError:
        page = 1
    page = max(1, page)
    start = (page - 1) * per_page
    return items[start:start + per_page], per_page, page


def tree_map_at(repo, ref):
    sha = resolve_ref(repo, ref)
    if sha is None:
        return {}
    tree = repo.tree_at(sha)
    return repo.tree_to_map(tree) if tree else {}


def commit_tree_change(repo, branch, flat_map, message):
    """Create a commit holding flat_map on branch; return its sha."""
    tree_sha = repo.map_to_tree(flat_map)
    parent = ref_tip(repo, branch)
    parents = [parent] if parent else []
    commit_sha = repo.make_commit(tree_sha, parents, message, STORE.next_epoch())
    repo.refs["refs/heads/" + branch] = commit_sha
    return commit_sha


def rest_create_repo(body):
    name = body.get("name")
    if not isinstance(name, str) or not name:
        raise HttpError(422, "Validation Failed: name is required")
    with STORE.lock:
        if name in STORE.repos:
            raise HttpError(422, "Validation Failed: name already exists on this account")
        repo = STORE.create_repo(name, body.get("private", False), body.get("description") or "")
    return 201, repo_json(repo)


def rest_list_user_repos(query, host):
    with STORE.lock:
        repos = sorted(STORE.repos.values(), key=lambda r: r.id)
    total = len(repos)
    page_items, per_page, page = paginate(repos, query)
    headers = {}
    if (page) * per_page < total:
        next_url = "http://%s/api/v3/user/repos?per_page=%d&page=%d" % (host, per_page, page + 1)
        last_page = (total + per_page - 1) // per_page
        last_url = "http://%s/api/v3/user/repos?per_page=%d&page=%d" % (host, per_page, last_page)
        headers["Link"] = '<%s>; rel="next", <%s>; rel="last"' % (next_url, last_url)
    return 200, [repo_json(r) for r in page_items], headers


def rest_get_repo(owner, name):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    return 200, repo_json(repo)


def rest_patch_repo(owner, name, body):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    with STORE.lock:
        if "description" in body:
            repo.description = body["description"] or ""
        if "private" in body:
            repo.private = bool(body["private"])
        if "name" in body and isinstance(body["name"], str) and body["name"] != repo.name:
            new_name = body["name"]
            if new_name in STORE.repos:
                raise HttpError(422, "Validation Failed: name already exists on this account")
            del STORE.repos[repo.name]
            repo.name = new_name
            STORE.repos[new_name] = repo
    return 200, repo_json(repo)


def rest_delete_repo(owner, name):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    with STORE.lock:
        del STORE.repos[name]
    return 204, None


def rest_list_branches(owner, name):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    branches = []
    for ref, sha in sorted_refs(repo):
        if ref.startswith("refs/heads/"):
            branches.append({"name": ref[len("refs/heads/"):], "commit": {"sha": sha}})
    return 200, branches


def rest_list_commits(owner, name, query):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    ref = query.get("sha", [None])[0]
    tip = resolve_ref(repo, ref)
    commits = []
    seen = set()
    stack = [tip] if tip else []
    ordered = []
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        ordered.append(cur)
        stack.extend(repo.commit_parents(cur))
    for sha in ordered:
        parsed = parse_commit(repo.get_object(sha)[1])
        commits.append({
            "sha": sha,
            "commit": {"message": parsed["message"].rstrip("\n")},
            "parents": [{"sha": p} for p in parsed["parents"]],
        })
    return 200, commits


def rest_create_ref(owner, name, body):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    ref = body.get("ref")
    sha = body.get("sha")
    if not isinstance(ref, str) or not ref.startswith("refs/"):
        raise HttpError(422, "Validation Failed: ref is required")
    obj = repo.get_object(sha) if isinstance(sha, str) else None
    if obj is None:
        raise HttpError(422, "Validation Failed: sha does not name an object")
    with STORE.lock:
        if ref in repo.refs:
            raise HttpError(422, "Validation Failed: Reference already exists")
        repo.refs[ref] = sha
    return 201, {"ref": ref, "object": {"sha": sha, "type": obj[0]}}


def rest_get_ref(owner, name, refpath):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    full = "refs/" + refpath
    sha = repo.refs.get(full)
    if sha is None:
        raise HttpError(404, "Not Found")
    obj = repo.get_object(sha)
    return 200, {"ref": full, "object": {"sha": sha, "type": obj[0] if obj else "commit"}}


def rest_get_contents(owner, name, path, query):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    ref = query.get("ref", [None])[0]
    flat = tree_map_at(repo, ref)
    if path in flat:
        blob_sha = flat[path]
        data = repo.get_object(blob_sha)[1]
        return 200, {
            "type": "file",
            "name": path.split("/")[-1],
            "path": path,
            "sha": blob_sha,
            "size": len(data),
            "encoding": "base64",
            "content": base64.b64encode(data).decode("ascii"),
        }
    prefix = (path + "/") if path else ""
    entries = {}
    for entry_path, blob_sha in flat.items():
        if not entry_path.startswith(prefix):
            continue
        rest = entry_path[len(prefix):]
        head, _, _tail = rest.partition("/")
        if head not in entries:
            if "/" in rest or _tail:
                entries[head] = ("dir", None)
            else:
                entries[head] = ("file", blob_sha)
    if entries or path == "":
        # Directory listing: dir shas are the subtree tree shas.
        sha = resolve_ref(repo, ref)
        tree_sha = repo.tree_at(sha) if sha else None
        dir_shas = subtree_shas(repo, tree_sha, path)
        listing = []
        for entry_name in sorted(entries):
            kind, blob_sha = entries[entry_name]
            listing.append({
                "type": kind,
                "name": entry_name,
                "path": prefix + entry_name,
                "sha": blob_sha if kind == "file" else dir_shas.get(entry_name, ZERO_OID),
            })
        return 200, listing
    raise HttpError(404, "Not Found")


def subtree_shas(repo, root_tree_sha, dir_path):
    """Map of immediate child name -> tree sha for the directory at dir_path."""
    if root_tree_sha is None:
        return {}
    current = root_tree_sha
    if dir_path:
        for part in dir_path.split("/"):
            obj = repo.get_object(current)
            if obj is None or obj[0] != "tree":
                return {}
            nxt = None
            for mode, entry_name, sha in parse_tree_entries(obj[1]):
                if entry_name == part and mode == "40000":
                    nxt = sha
                    break
            if nxt is None:
                return {}
            current = nxt
    obj = repo.get_object(current)
    result = {}
    if obj is not None and obj[0] == "tree":
        for mode, entry_name, sha in parse_tree_entries(obj[1]):
            if mode == "40000":
                result[entry_name] = sha
    return result


def rest_put_contents(owner, name, path, body):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    content_b64 = body.get("content")
    if not isinstance(content_b64, str):
        raise HttpError(422, "Validation Failed: content is required")
    try:
        data = base64.b64decode(content_b64, validate=False)
    except Exception:
        raise HttpError(422, "Validation Failed: content is not base64")
    message = body.get("message") or "Update %s" % path
    branch = body.get("branch") or repo.default_branch
    with STORE.lock:
        if branch != repo.default_branch and ref_tip(repo, branch) is None:
            raise HttpError(404, "Not Found: branch %s" % branch)
        flat = tree_map_at(repo, branch if ref_tip(repo, branch) else None) if ref_tip(repo, branch) else {}
        existing = flat.get(path)
        provided_sha = body.get("sha")
        if existing is not None:
            if provided_sha != existing:
                raise HttpError(409, "Conflict: %s does not match the current blob sha" % path)
            status = 200
        else:
            if provided_sha is not None:
                raise HttpError(404, "Not Found: %s" % path)
            status = 201
        blob_sha = repo.store_blob(data)
        flat[path] = blob_sha
        commit_sha = commit_tree_change(repo, branch, flat, message)
    payload = {
        "content": {
            "name": path.split("/")[-1],
            "path": path,
            "sha": blob_sha,
            "size": len(data),
            "encoding": "base64",
        },
        "commit": {"sha": commit_sha},
    }
    return status, payload


def rest_delete_contents(owner, name, path, body):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    branch = body.get("branch") or repo.default_branch
    with STORE.lock:
        flat = tree_map_at(repo, branch)
        existing = flat.get(path)
        if existing is None:
            raise HttpError(404, "Not Found")
        if body.get("sha") != existing:
            raise HttpError(409, "Conflict: sha does not match the current blob sha")
        del flat[path]
        commit_sha = commit_tree_change(repo, branch, flat, body.get("message") or "Delete %s" % path)
    return 200, {"content": None, "commit": {"sha": commit_sha}}


def rest_create_label(owner, name, body):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    label_name = body.get("name")
    if not isinstance(label_name, str) or not label_name:
        raise HttpError(422, "Validation Failed: name is required")
    with STORE.lock:
        if label_name in repo.labels:
            raise HttpError(422, "Validation Failed: Label already exists")
        label = {"name": label_name, "color": body.get("color") or "ededed"}
        repo.labels[label_name] = label
    return 201, dict(label)


def rest_create_issue(owner, name, body):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    title = body.get("title")
    if not isinstance(title, str) or not title:
        raise HttpError(422, "Validation Failed: title is required")
    labels = body.get("labels") or []
    with STORE.lock:
        for label_name in labels:
            if label_name not in repo.labels:
                raise HttpError(422, "Validation Failed: Label '%s' does not exist" % label_name)
        number = repo.next_number
        repo.next_number += 1
        now = iso_utc(STORE.next_epoch())
        issue = {
            "id": STORE.global_id(),
            "number": number,
            "title": title,
            "body": body.get("body") or "",
            "state": "open",
            "labels": list(labels),
            "comments": [],
            "created_at": now,
            "updated_at": now,
        }
        repo.issues[number] = issue
    return 201, issue_json(repo, issue)


def rest_list_issues(owner, name, query):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    state = query.get("state", ["open"])[0]
    items = [i for i in repo.issues.values() if state == "all" or i["state"] == state]
    direction = query.get("direction", ["desc"])[0]
    items.sort(key=lambda i: i["number"], reverse=(direction != "asc"))
    page_items, _pp, _pg = paginate(items, query)
    return 200, [issue_json(repo, i) for i in page_items]


def rest_get_issue(owner, name, number):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    issue = repo.issues.get(number)
    if issue is None:
        raise HttpError(404, "Not Found")
    return 200, issue_json(repo, issue)


def rest_patch_issue(owner, name, number, body):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    issue = repo.issues.get(number)
    if issue is None:
        raise HttpError(404, "Not Found")
    with STORE.lock:
        if "state" in body and body["state"] in ("open", "closed"):
            issue["state"] = body["state"]
        if "title" in body:
            issue["title"] = body["title"]
        if "body" in body:
            issue["body"] = body["body"] or ""
        issue["updated_at"] = iso_utc(STORE.next_epoch())
    return 200, issue_json(repo, issue)


def rest_create_comment(owner, name, number, body):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    issue = repo.issues.get(number)
    if issue is None:
        raise HttpError(404, "Not Found")
    with STORE.lock:
        comment = {"id": repo.next_comment_id, "body": body.get("body") or ""}
        repo.next_comment_id += 1
        issue["comments"].append(comment)
    return 201, dict(comment)


def rest_list_comments(owner, name, number):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    issue = repo.issues.get(number)
    if issue is None:
        raise HttpError(404, "Not Found")
    return 200, [dict(c) for c in issue["comments"]]


def validate_branch(repo, branch):
    return isinstance(branch, str) and ref_tip(repo, branch) is not None


def rest_create_pull(owner, name, body):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    head = body.get("head")
    base = body.get("base")
    title = body.get("title")
    if not validate_branch(repo, head):
        raise HttpError(422, "Validation Failed: head branch does not exist")
    if not validate_branch(repo, base):
        raise HttpError(422, "Validation Failed: base branch does not exist")
    with STORE.lock:
        for pull in repo.pulls.values():
            if pull["head"] == head and pull["base"] == base and pull["state"] == "open":
                raise HttpError(422, "Validation Failed: A pull request already exists for %s -> %s" % (head, base))
        number = repo.next_number
        repo.next_number += 1
        now = iso_utc(STORE.next_epoch())
        pull = {
            "id": STORE.global_id(),
            "number": number,
            "title": title or "",
            "body": body.get("body") or "",
            "state": "open",
            "head": head,
            "base": base,
            "merged": False,
            "merged_at": None,
            "merge_commit_sha": None,
            "created_at": now,
            "updated_at": now,
        }
        repo.pulls[number] = pull
    return 201, pull_json(repo, pull)


def rest_list_pulls(owner, name, query):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    state = query.get("state", ["open"])[0]
    items = [p for p in repo.pulls.values() if state == "all" or p["state"] == state]
    items.sort(key=lambda p: p["number"])
    page_items, _pp, _pg = paginate(items, query)
    return 200, [pull_json(repo, p) for p in page_items]


def rest_get_pull(owner, name, number):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    pull = repo.pulls.get(number)
    if pull is None:
        raise HttpError(404, "Not Found")
    return 200, pull_json(repo, pull)


def diff_counts(repo, old_bytes, new_bytes):
    import difflib
    old_lines = old_bytes.decode("utf-8", "replace").splitlines() if old_bytes else []
    new_lines = new_bytes.decode("utf-8", "replace").splitlines() if new_bytes else []
    additions = 0
    deletions = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            additions += j2 - j1
        if tag in ("replace", "delete"):
            deletions += i2 - i1
    return additions, deletions


def pull_files(repo, pull):
    base_tip = ref_tip(repo, pull["base"])
    head_tip = ref_tip(repo, pull["head"])
    base_sha = repo.merge_base(base_tip, head_tip) if base_tip and head_tip else base_tip
    base_map = {}
    head_map = {}
    if base_sha:
        tree = repo.tree_at(base_sha)
        base_map = repo.tree_to_map(tree) if tree else {}
    if head_tip:
        tree = repo.tree_at(head_tip)
        head_map = repo.tree_to_map(tree) if tree else {}
    files = []
    for path in sorted(set(base_map) | set(head_map)):
        old = base_map.get(path)
        new = head_map.get(path)
        if old == new:
            continue
        old_bytes = repo.get_object(old)[1] if old else b""
        new_bytes = repo.get_object(new)[1] if new else b""
        additions, deletions = diff_counts(repo, old_bytes, new_bytes)
        if old is None:
            status = "added"
        elif new is None:
            status = "removed"
        else:
            status = "modified"
        files.append({
            "filename": path,
            "status": status,
            "additions": additions,
            "deletions": deletions,
            "changes": additions + deletions,
        })
    return files


def three_way_merge(repo, base_map, our_map, their_map):
    """Return merged map, or None on conflict."""
    merged = {}
    for path in set(base_map) | set(our_map) | set(their_map):
        base_v = base_map.get(path)
        our_v = our_map.get(path)
        their_v = their_map.get(path)
        if our_v == their_v:
            if our_v is not None:
                merged[path] = our_v
        elif base_v == our_v:
            if their_v is not None:
                merged[path] = their_v
        elif base_v == their_v:
            if our_v is not None:
                merged[path] = our_v
        else:
            return None
    return merged


def merge_pull(repo, pull, method):
    """Perform the merge; returns (sha, None) or (None, HttpError)."""
    if pull["state"] != "open":
        return None, HttpError(405, "Pull Request is not open")
    base_tip = ref_tip(repo, pull["base"])
    head_tip = ref_tip(repo, pull["head"])
    if base_tip is None or head_tip is None:
        return None, HttpError(405, "Pull Request is not mergeable")
    base_sha = repo.merge_base(base_tip, head_tip)
    base_map = repo.tree_to_map(repo.tree_at(base_sha)) if base_sha else {}
    our_map = repo.tree_to_map(repo.tree_at(base_tip))
    their_map = repo.tree_to_map(repo.tree_at(head_tip))
    merged_map = three_way_merge(repo, base_map, our_map, their_map)
    if merged_map is None:
        return None, HttpError(405, "Pull Request is not mergeable: merge conflict")
    tree_sha = repo.map_to_tree(merged_map)
    epoch = STORE.next_epoch()
    if method == "merge":
        parents = [base_tip, head_tip]
        message = "Merge pull request #%d" % pull["number"]
    else:
        parents = [base_tip]
        message = pull["title"] or ("Squash merge #%d" % pull["number"])
    commit_sha = repo.make_commit(tree_sha, parents, message, epoch)
    repo.refs["refs/heads/" + pull["base"]] = commit_sha
    pull["state"] = "closed"
    pull["merged"] = True
    pull["merged_at"] = iso_utc(epoch)
    pull["merge_commit_sha"] = commit_sha
    pull["updated_at"] = iso_utc(epoch)
    return commit_sha, None


def rest_merge_pull(owner, name, number, body):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    pull = repo.pulls.get(number)
    if pull is None:
        raise HttpError(404, "Not Found")
    method = (body.get("merge_method") or "merge").lower()
    with STORE.lock:
        sha, error = merge_pull(repo, pull, method)
    if error is not None:
        raise error
    return 200, {"sha": sha, "merged": True, "message": "Pull Request successfully merged"}


def rest_pull_files(owner, name, number):
    repo = get_repo(owner, name)
    if repo is None:
        raise HttpError(404, "Not Found")
    pull = repo.pulls.get(number)
    if pull is None:
        raise HttpError(404, "Not Found")
    return 200, pull_files(repo, pull)


# --------------------------------------------------------------------------
# GraphQL

class GqlError(Exception):
    pass


_GQL_TOKEN_RE = re.compile(r"""
    (?P<comment>\#[^\n]*)
  | (?P<spread>\.\.\.)
  | (?P<name>[_A-Za-z][_0-9A-Za-z]*)
  | (?P<string>"(?:\\.|[^"\\])*")
  | (?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<punct>[!$():=@\[\]{}|&])
  | (?P<ws>[\s,]+)
""", re.VERBOSE)


def gql_tokenize(text):
    tokens = []
    pos = 0
    while pos < len(text):
        match = _GQL_TOKEN_RE.match(text, pos)
        if match is None:
            raise GqlError("Syntax error: unexpected character %r" % text[pos])
        pos = match.end()
        kind = match.lastgroup
        if kind in ("comment", "ws"):
            continue
        tokens.append((kind, match.group()))
    tokens.append(("eof", ""))
    return tokens


class GqlParser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect_punct(self, ch):
        kind, value = self.next()
        if kind != "punct" or value != ch:
            raise GqlError("Syntax error: expected %r, got %r" % (ch, value))

    def expect_name(self):
        kind, value = self.next()
        if kind != "name":
            raise GqlError("Syntax error: expected a name, got %r" % value)
        return value

    def parse_document(self):
        operations = []
        fragments = {}
        while self.peek()[0] != "eof":
            kind, value = self.peek()
            if kind == "punct" and value == "{":
                operations.append(("query", None, {}, self.parse_selection_set()))
            elif kind == "name" and value in ("query", "mutation", "subscription"):
                self.next()
                op_name = None
                if self.peek()[0] == "name":
                    op_name = self.next()[1]
                if self.peek() == ("punct", "("):
                    self.parse_variable_definitions()
                operations.append((value, op_name, {}, self.parse_selection_set()))
            elif kind == "name" and value == "fragment":
                self.next()
                frag_name = self.expect_name()
                if self.expect_name() != "on":
                    raise GqlError("Syntax error: fragment without type condition")
                type_cond = self.expect_name()
                fragments[frag_name] = (type_cond, self.parse_selection_set())
            else:
                raise GqlError("Syntax error: unexpected token %r" % value)
        return operations, fragments

    def parse_variable_definitions(self):
        self.expect_punct("(")
        while self.peek() != ("punct", ")"):
            self.expect_punct("$")
            self.expect_name()
            self.expect_punct(":")
            self.parse_type()
            if self.peek() == ("punct", "="):
                self.next()
                self.parse_value()
        self.expect_punct(")")

    def parse_type(self):
        kind, value = self.peek()
        if kind == "punct" and value == "[":
            self.next()
            self.parse_type()
            self.expect_punct("]")
        else:
            self.expect_name()
        if self.peek() == ("punct", "!"):
            self.next()

    def parse_selection_set(self):
        self.expect_punct("{")
        selections = []
        while self.peek() != ("punct", "}"):
            selections.append(self.parse_selection())
        self.expect_punct("}")
        return selections

    def parse_selection(self):
        kind, value = self.peek()
        if kind == "spread":
            self.next()
            if self.peek()[0] == "name" and self.peek()[1] == "on":
                self.next()
                type_cond = self.expect_name()
                return ("inline", type_cond, self.parse_selection_set())
            frag_name = self.expect_name()
            return ("spread", frag_name, None)
        first = self.expect_name()
        alias = None
        name = first
        if self.peek() == ("punct", ":"):
            self.next()
            alias = first
            name = self.expect_name()
        args = {}
        if self.peek() == ("punct", "("):
            args = self.parse_arguments()
        selection_set = None
        if self.peek() == ("punct", "{"):
            selection_set = self.parse_selection_set()
        return ("field", name, alias, args, selection_set)

    def parse_arguments(self):
        self.expect_punct("(")
        args = {}
        while self.peek() != ("punct", ")"):
            arg_name = self.expect_name()
            self.expect_punct(":")
            args[arg_name] = self.parse_value()
        self.expect_punct(")")
        return args

    def parse_value(self):
        kind, value = self.next()
        if kind == "punct" and value == "$":
            return ("var", self.expect_name())
        if kind == "string":
            return ("value", json.loads(value))
        if kind == "number":
            return ("value", float(value) if ("." in value or "e" in value or "E" in value) else int(value))
        if kind == "name":
            if value == "true":
                return ("value", True)
            if value == "false":
                return ("value", False)
            if value == "null":
                return ("value", None)
            return ("value", ("enum", value))
        if kind == "punct" and value == "[":
            items = []
            while self.peek() != ("punct", "]"):
                items.append(self.parse_value())
            self.expect_punct("]")
            return ("list", items)
        if kind == "punct" and value == "{":
            obj = {}
            while self.peek() != ("punct", "}"):
                key = self.expect_name()
                self.expect_punct(":")
                obj[key] = self.parse_value()
            self.expect_punct("}")
            return ("object", obj)
        raise GqlError("Syntax error: unexpected value token %r" % value)


def gql_eval_value(node, variables):
    kind = node[0]
    if kind == "var":
        return variables.get(node[1])
    if kind == "value":
        value = node[1]
        if isinstance(value, tuple) and value and value[0] == "enum":
            return value[1]
        return value
    if kind == "list":
        return [gql_eval_value(item, variables) for item in node[1]]
    if kind == "object":
        return {key: gql_eval_value(val, variables) for key, val in node[1].items()}
    return None


def gql_id(kind, *parts):
    raw = kind + ":" + ":".join(str(p) for p in parts)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def gql_decode_id(value):
    if not isinstance(value, str):
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        parts = raw.split(":")
        return parts[0], parts[1:]
    except Exception:
        return None


GQL_TYPE_FIELDS = {
    "Query": ["viewer", "repository", "node", "__type", "__schema"],
    "Mutation": ["createIssue", "closeIssue", "createPullRequest", "mergePullRequest", "addComment"],
    "User": ["id", "login", "name"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner", "defaultBranchRef",
                   "viewerPermission", "hasIssuesEnabled", "issue", "issues", "pullRequest", "pullRequests"],
    "Issue": ["id", "number", "title", "state", "body", "createdAt", "updatedAt"],
    "PullRequest": ["id", "number", "title", "body", "state", "merged", "headRefName", "baseRefName",
                    "mergeable", "files", "createdAt", "updatedAt"],
    "Ref": ["name"],
    "IssueConnection": ["nodes", "pageInfo", "totalCount"],
    "PullRequestConnection": ["nodes", "pageInfo", "totalCount"],
    "PageInfo": ["hasNextPage", "hasPreviousPage", "startCursor", "endCursor"],
    "PullRequestChangedFile": ["path", "additions", "deletions", "changeType"],
    "Comment": ["id", "body", "createdAt"],
    "CreateIssuePayload": ["issue", "clientMutationId"],
    "CloseIssuePayload": ["issue", "clientMutationId"],
    "CreatePullRequestPayload": ["pullRequest", "clientMutationId"],
    "MergePullRequestPayload": ["pullRequest", "clientMutationId"],
    "AddCommentPayload": ["commentEdge", "clientMutationId"],
    "CommentEdge": ["node", "cursor"],
    "__Type": ["name", "kind", "fields", "enumValues", "description"],
    "__Field": ["name", "description"],
    "__EnumValue": ["name", "description"],
}

GQL_ENUMS = {
    "SearchType": ["ISSUE", "REPOSITORY", "USER", "DISCUSSION"],
    "IssueState": ["OPEN", "CLOSED"],
    "PullRequestState": ["OPEN", "CLOSED", "MERGED"],
    "MergeableState": ["MERGEABLE", "CONFLICTING", "UNKNOWN"],
    "PullRequestMergeMethod": ["MERGE", "SQUASH", "REBASE"],
    "RepositoryPermission": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"],
    "IssueOrderField": ["CREATED_AT", "UPDATED_AT", "COMMENTS"],
    "OrderDirection": ["ASC", "DESC"],
}


def gql_type_object(name):
    if name in GQL_ENUMS:
        return ("__Type", {"name": name, "kind": "ENUM", "fields": None, "enumValues": GQL_ENUMS[name]})
    if name in GQL_TYPE_FIELDS:
        return ("__Type", {"name": name, "kind": "OBJECT", "fields": GQL_TYPE_FIELDS[name], "enumValues": None})
    return None


def find_repo_by_id(db_id):
    for repo in STORE.repos.values():
        if repo.id == db_id:
            return repo
    return None


def issue_cursor(number):
    return base64.urlsafe_b64encode(("cursor:%d" % number).encode("ascii")).decode("ascii").rstrip("=")


def decode_issue_cursor(cursor):
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
        if raw.startswith("cursor:"):
            return int(raw[len("cursor:"):])
    except Exception:
        pass
    return None


def gql_resolve(typename, payload, field, args):
    """Return a raw value: scalar, or a (typename, payload) object tuple,
    or a list of those."""
    if typename == "Query":
        if field == "viewer":
            return ("User", {"login": VIEWER_LOGIN})
        if field == "repository":
            repo = get_repo(args.get("owner"), args.get("name"))
            return ("Repository", repo) if repo is not None else None
        if field == "node":
            decoded = gql_decode_id(args.get("id"))
            if decoded is None:
                return None
            kind, parts = decoded
            if kind == "repo" and parts:
                repo = find_repo_by_id(int(parts[0]))
                return ("Repository", repo) if repo is not None else None
            if kind == "issue" and len(parts) >= 2:
                repo = find_repo_by_id(int(parts[0]))
                issue = repo.issues.get(int(parts[1])) if repo else None
                return ("Issue", (repo, issue)) if issue is not None else None
            if kind == "pr" and len(parts) >= 2:
                repo = find_repo_by_id(int(parts[0]))
                pull = repo.pulls.get(int(parts[1])) if repo else None
                return ("PullRequest", (repo, pull)) if pull is not None else None
            return None
        if field == "__type":
            return gql_type_object(args.get("name"))
        if field == "__schema":
            return None
    if typename == "Mutation":
        return gql_mutation(field, args)
    if typename == "User":
        if field == "login":
            return payload["login"]
        if field == "id":
            return gql_id("user", payload["login"])
        if field == "name":
            return payload["login"]
    if typename == "Repository":
        repo = payload
        if field == "id":
            return gql_id("repo", repo.id)
        if field == "databaseId":
            return repo.id
        if field == "name":
            return repo.name
        if field == "nameWithOwner":
            return "%s/%s" % (repo.owner, repo.name)
        if field == "owner":
            return ("User", {"login": repo.owner})
        if field == "defaultBranchRef":
            return ("Ref", {"name": repo.default_branch})
        if field == "viewerPermission":
            return "ADMIN"
        if field == "hasIssuesEnabled":
            return True
        if field == "issue":
            issue = repo.issues.get(args.get("number"))
            return ("Issue", (repo, issue)) if issue is not None else None
        if field == "issues":
            return gql_repo_issues(repo, args)
        if field == "pullRequest":
            pull = repo.pulls.get(args.get("number"))
            return ("PullRequest", (repo, pull)) if pull is not None else None
        if field == "pullRequests":
            pulls = sorted((p for p in repo.pulls.values() if p["state"] == "open"), key=lambda p: p["number"])
            first = args.get("first")
            if isinstance(first, int):
                pulls = pulls[:first]
            return ("PullRequestConnection", {
                "nodes": [("PullRequest", (repo, p)) for p in pulls],
                "pageInfo": {"hasNextPage": False, "hasPreviousPage": False, "startCursor": None, "endCursor": None},
                "totalCount": len(pulls),
            })
    if typename == "Issue":
        repo, issue = payload
        if field == "id":
            return gql_id("issue", repo.id, issue["number"])
        if field == "number":
            return issue["number"]
        if field == "title":
            return issue["title"]
        if field == "state":
            return issue["state"].upper()
        if field == "body":
            return issue["body"]
        if field == "createdAt":
            return issue["created_at"]
        if field == "updatedAt":
            return issue["updated_at"]
    if typename == "PullRequest":
        repo, pull = payload
        if field == "id":
            return gql_id("pr", repo.id, pull["number"])
        if field == "number":
            return pull["number"]
        if field == "title":
            return pull["title"]
        if field == "body":
            return pull["body"]
        if field == "state":
            if pull["merged"]:
                return "MERGED"
            return pull["state"].upper()
        if field == "merged":
            return pull["merged"]
        if field == "headRefName":
            return pull["head"]
        if field == "baseRefName":
            return pull["base"]
        if field == "mergeable":
            if pull["state"] != "open":
                return "UNKNOWN"
            base_tip = ref_tip(repo, pull["base"])
            head_tip = ref_tip(repo, pull["head"])
            if base_tip is None or head_tip is None:
                return "UNKNOWN"
            base_sha = repo.merge_base(base_tip, head_tip)
            base_map = repo.tree_to_map(repo.tree_at(base_sha)) if base_sha else {}
            merged = three_way_merge(repo, base_map,
                                      repo.tree_to_map(repo.tree_at(base_tip)),
                                      repo.tree_to_map(repo.tree_at(head_tip)))
            return "MERGEABLE" if merged is not None else "CONFLICTING"
        if field == "files":
            files = pull_files(repo, pull)
            first = args.get("first")
            if isinstance(first, int):
                files = files[:first]
            return ("PullRequestChangedFileConnection", {
                "nodes": [("PullRequestChangedFile", {
                    "path": f["filename"], "additions": f["additions"],
                    "deletions": f["deletions"], "changeType": f["status"].upper(),
                }) for f in files],
            })
        if field == "createdAt":
            return pull["created_at"]
        if field == "updatedAt":
            return pull["updated_at"]
    if typename == "Ref":
        if field == "name":
            return payload["name"]
    if typename in ("IssueConnection", "PullRequestConnection"):
        if field == "nodes":
            return payload["nodes"]
        if field == "pageInfo":
            return ("PageInfo", payload["pageInfo"])
        if field == "totalCount":
            return payload["totalCount"]
    if typename == "PullRequestChangedFileConnection":
        if field == "nodes":
            return payload["nodes"]
        if field == "pageInfo":
            return ("PageInfo", {"hasNextPage": False, "hasPreviousPage": False, "startCursor": None, "endCursor": None})
    if typename == "PageInfo":
        return payload.get(field)
    if typename == "PullRequestChangedFile":
        return payload.get(field)
    if typename == "Comment":
        repo, issue, comment = payload
        if field == "id":
            return gql_id("comment", repo.id, issue["number"], comment["id"])
        if field == "body":
            return comment["body"]
        if field == "createdAt":
            return issue["created_at"]
    if typename == "CreateIssuePayload" or typename == "CloseIssuePayload":
        if field == "issue":
            return payload["issue"]
        if field == "clientMutationId":
            return None
    if typename == "CreatePullRequestPayload" or typename == "MergePullRequestPayload":
        if field == "pullRequest":
            return payload["pullRequest"]
        if field == "clientMutationId":
            return None
    if typename == "AddCommentPayload":
        if field == "commentEdge":
            return payload["commentEdge"]
        if field == "clientMutationId":
            return None
    if typename == "CommentEdge":
        if field == "node":
            return payload["node"]
        if field == "cursor":
            return None
    if typename == "__Type":
        if field == "name":
            return payload["name"]
        if field == "kind":
            return payload["kind"]
        if field == "description":
            return None
        if field == "fields":
            if payload["fields"] is None:
                return None
            return [("__Field", {"name": n}) for n in payload["fields"]]
        if field == "enumValues":
            if payload["enumValues"] is None:
                return None
            return [("__EnumValue", {"name": n}) for n in payload["enumValues"]]
    if typename == "__Field":
        if field == "name":
            return payload["name"]
        if field == "description":
            return None
    if typename == "__EnumValue":
        if field == "name":
            return payload["name"]
        if field == "description":
            return None
    raise GqlError("Cannot query field '%s' on type '%s'" % (field, typename))


def gql_repo_issues(repo, args):
    items = sorted(repo.issues.values(), key=lambda i: i["number"])
    states = args.get("states")
    if states:
        wanted = {str(s).lower() for s in states}
        items = [i for i in items if i["state"] in wanted]
    order_by = args.get("orderBy") or {}
    direction = str(order_by.get("direction", "ASC")).upper() if isinstance(order_by, dict) else "ASC"
    if direction == "DESC":
        items = list(reversed(items))
    total = len(items)
    after = args.get("after")
    if after:
        after_number = decode_issue_cursor(after)
        if after_number is not None:
            numbers = [i["number"] for i in items]
            if after_number in numbers:
                items = items[numbers.index(after_number) + 1:]
    first = args.get("first")
    page = items
    has_next = False
    if isinstance(first, int):
        page = items[:first]
        has_next = len(items) > first
    end_cursor = issue_cursor(page[-1]["number"]) if page else after
    return ("IssueConnection", {
        "nodes": [("Issue", (repo, i)) for i in page],
        "pageInfo": {
            "hasNextPage": has_next,
            "hasPreviousPage": bool(after),
            "startCursor": issue_cursor(page[0]["number"]) if page else None,
            "endCursor": end_cursor,
        },
        "totalCount": total,
    })


def gql_mutation(field, args):
    input_obj = args.get("input") or {}
    if field == "createIssue":
        decoded = gql_decode_id(input_obj.get("repositoryId"))
        repo = find_repo_by_id(int(decoded[1][0])) if decoded and decoded[0] == "repo" else None
        if repo is None:
            raise GqlError("repository not found")
        with STORE.lock:
            number = repo.next_number
            repo.next_number += 1
            now = iso_utc(STORE.next_epoch())
            issue = {
                "id": STORE.global_id(), "number": number,
                "title": input_obj.get("title") or "", "body": input_obj.get("body") or "",
                "state": "open", "labels": [], "comments": [],
                "created_at": now, "updated_at": now,
            }
            repo.issues[number] = issue
        return ("CreateIssuePayload", {"issue": ("Issue", (repo, issue))})
    if field == "closeIssue":
        decoded = gql_decode_id(input_obj.get("issueId"))
        if decoded and decoded[0] == "issue" and len(decoded[1]) >= 2:
            repo = find_repo_by_id(int(decoded[1][0]))
            issue = repo.issues.get(int(decoded[1][1])) if repo else None
            if issue is not None:
                with STORE.lock:
                    issue["state"] = "closed"
                    issue["updated_at"] = iso_utc(STORE.next_epoch())
                return ("CloseIssuePayload", {"issue": ("Issue", (repo, issue))})
        raise GqlError("issue not found")
    if field == "createPullRequest":
        decoded = gql_decode_id(input_obj.get("repositoryId"))
        repo = find_repo_by_id(int(decoded[1][0])) if decoded and decoded[0] == "repo" else None
        if repo is None:
            raise GqlError("repository not found")
        status, payload = _create_pull_impl(repo, input_obj.get("headRefName"), input_obj.get("baseRefName"),
                                            input_obj.get("title"), input_obj.get("body"))
        if status != 201:
            raise GqlError(payload["message"])
        pull = repo.pulls[payload["number"]]
        return ("CreatePullRequestPayload", {"pullRequest": ("PullRequest", (repo, pull))})
    if field == "mergePullRequest":
        decoded = gql_decode_id(input_obj.get("pullRequestId"))
        if decoded and decoded[0] == "pr" and len(decoded[1]) >= 2:
            repo = find_repo_by_id(int(decoded[1][0]))
            pull = repo.pulls.get(int(decoded[1][1])) if repo else None
            if pull is not None:
                method = str(input_obj.get("mergeMethod") or "MERGE").lower()
                if method == "rebase":
                    method = "squash"
                with STORE.lock:
                    _sha, error = merge_pull(repo, pull, method)
                if error is not None:
                    raise GqlError(error.message)
                return ("MergePullRequestPayload", {"pullRequest": ("PullRequest", (repo, pull))})
        raise GqlError("pull request not found")
    if field == "addComment":
        decoded = gql_decode_id(input_obj.get("subjectId"))
        if decoded and decoded[0] == "issue" and len(decoded[1]) >= 2:
            repo = find_repo_by_id(int(decoded[1][0]))
            issue = repo.issues.get(int(decoded[1][1])) if repo else None
            if issue is not None:
                with STORE.lock:
                    comment = {"id": repo.next_comment_id, "body": input_obj.get("body") or ""}
                    repo.next_comment_id += 1
                    issue["comments"].append(comment)
                node = ("Comment", (repo, issue, comment))
                return ("AddCommentPayload", {"commentEdge": ("CommentEdge", {"node": node})})
        raise GqlError("subject not found")
    raise GqlError("unknown mutation '%s'" % field)


def _create_pull_impl(repo, head, base, title, body):
    """Shared by GraphQL createPullRequest; returns (status, payload)."""
    if not validate_branch(repo, head):
        return 422, {"message": "Validation Failed: head branch does not exist"}
    if not validate_branch(repo, base):
        return 422, {"message": "Validation Failed: base branch does not exist"}
    with STORE.lock:
        for pull in repo.pulls.values():
            if pull["head"] == head and pull["base"] == base and pull["state"] == "open":
                return 422, {"message": "Validation Failed: A pull request already exists for %s -> %s" % (head, base)}
        number = repo.next_number
        repo.next_number += 1
        now = iso_utc(STORE.next_epoch())
        pull = {
            "id": STORE.global_id(), "number": number, "title": title or "", "body": body or "",
            "state": "open", "head": head, "base": base, "merged": False,
            "merged_at": None, "merge_commit_sha": None, "created_at": now, "updated_at": now,
        }
        repo.pulls[number] = pull
    return 201, pull_json(repo, pull)


def gql_execute_selection(typename, payload, selections, variables, fragments, errors):
    out = {}
    for selection in selections:
        kind = selection[0]
        if kind == "inline":
            _s, type_cond, sub = selection
            if type_cond == typename:
                out.update(gql_execute_selection(typename, payload, sub, variables, fragments, errors))
            continue
        if kind == "spread":
            frag = fragments.get(selection[1])
            if frag is not None and frag[0] == typename:
                out.update(gql_execute_selection(typename, payload, frag[1], variables, fragments, errors))
            continue
        _s, name, alias, arg_nodes, sub = selection
        key = alias or name
        if name == "__typename":
            out[key] = typename
            continue
        args = {k: gql_eval_value(v, variables) for k, v in arg_nodes.items()}
        known_fields = GQL_TYPE_FIELDS.get(typename)
        if typename == "PullRequestChangedFileConnection":
            known_fields = ["nodes", "pageInfo"]
        if known_fields is not None and name not in known_fields and name != "__typename":
            errors.append({"message": "Cannot query field '%s' on type '%s'" % (name, typename)})
            out[key] = None
            continue
        try:
            value = gql_resolve(typename, payload, name, args)
        except GqlError as err:
            errors.append({"message": str(err)})
            out[key] = None
            continue
        out[key] = gql_complete(value, sub, variables, fragments, errors)
    return out


def gql_complete(value, sub, variables, fragments, errors):
    if value is None:
        return None
    if isinstance(value, tuple):
        typename, payload = value
        if sub is None:
            return {}
        return gql_execute_selection(typename, payload, sub, variables, fragments, errors)
    if isinstance(value, list):
        return [gql_complete(item, sub, variables, fragments, errors) for item in value]
    return value


def gql_run(document_text, variables, operation_name):
    tokens = gql_tokenize(document_text)
    operations, fragments = GqlParser(tokens).parse_document()
    chosen = None
    for op in operations:
        if operation_name is None or op[1] == operation_name:
            chosen = op
            break
    if chosen is None:
        raise GqlError("operation not found")
    op_type, _name, _vd, selections = chosen
    root_type = "Mutation" if op_type == "mutation" else "Query"
    errors = []
    data = gql_execute_selection(root_type, None, selections, variables or {}, fragments, errors)
    result = {"data": data}
    if errors:
        result["errors"] = errors
    return result


def handle_graphql(body):
    """body: parsed JSON (object or list). Returns (status, payload)."""
    if isinstance(body, list):
        results = []
        for item in body:
            results.append(handle_graphql(item)[1])
        return 200, results
    if not isinstance(body, dict):
        return 400, {"errors": [{"message": "request body must be an object"}]}
    query = body.get("query")
    if not isinstance(query, str):
        return 400, {"errors": [{"message": "query is required"}]}
    try:
        result = gql_run(query, body.get("variables") or {}, body.get("operationName"))
    except GqlError as err:
        return 200, {"errors": [{"message": str(err)}], "data": None}
    return 200, result


# --------------------------------------------------------------------------
# MCP

MCP_TOOLS = [
    {
        "name": "create_repo",
        "description": "Create a new repository for the authenticated user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Repository name."},
                "private": {"type": "boolean", "description": "Whether the repository is private."},
                "description": {"type": "string", "description": "Repository description."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_repo",
        "description": "Get a repository by owner and name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login."},
                "repo": {"type": "string", "description": "Repository name."},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_issue",
        "description": "Create an issue in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login."},
                "repo": {"type": "string", "description": "Repository name."},
                "title": {"type": "string", "description": "Issue title."},
                "body": {"type": "string", "description": "Issue body."},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "Label names."},
            },
            "required": ["owner", "repo", "title"],
        },
    },
    {
        "name": "list_issues",
        "description": "List issues in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login."},
                "repo": {"type": "string", "description": "Repository name."},
                "state": {"type": "string", "description": "Filter by state: open, closed, or all."},
            },
            "required": ["owner", "repo"],
        },
    },
    {
        "name": "create_pull_request",
        "description": "Create a pull request in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login."},
                "repo": {"type": "string", "description": "Repository name."},
                "title": {"type": "string", "description": "Pull request title."},
                "body": {"type": "string", "description": "Pull request body."},
                "head": {"type": "string", "description": "Head branch name."},
                "base": {"type": "string", "description": "Base branch name."},
            },
            "required": ["owner", "repo", "title", "head", "base"],
        },
    },
    {
        "name": "merge_pull_request",
        "description": "Merge a pull request in a repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner login."},
                "repo": {"type": "string", "description": "Repository name."},
                "pull_number": {"type": "integer", "description": "Pull request number."},
                "merge_method": {"type": "string", "description": "Merge method: merge or squash."},
            },
            "required": ["owner", "repo", "pull_number"],
        },
    },
]

MCP_TOOL_MAP = {tool["name"]: tool for tool in MCP_TOOLS}


def mcp_tool_result(payload):
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "structuredContent": payload,
        "isError": False,
    }


def mcp_tool_error(message):
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def mcp_call_tool(name, args):
    try:
        if name == "create_repo":
            status, payload = rest_create_repo({
                "name": args.get("name"),
                "private": args.get("private", False),
                "description": args.get("description") or "",
            })
            return mcp_tool_result(payload)
        if name == "get_repo":
            status, payload = rest_get_repo(args.get("owner"), args.get("repo"))
            return mcp_tool_result(payload)
        if name == "create_issue":
            body = {"title": args.get("title"), "body": args.get("body") or ""}
            if args.get("labels") is not None:
                body["labels"] = args.get("labels")
            status, payload = rest_create_issue(args.get("owner"), args.get("repo"), body)
            return mcp_tool_result(payload)
        if name == "list_issues":
            repo = get_repo(args.get("owner"), args.get("repo"))
            if repo is None:
                return mcp_tool_error("repository not found")
            state = args.get("state") or "all"
            items = [i for i in sorted(repo.issues.values(), key=lambda i: i["number"])
                     if state == "all" or i["state"] == state]
            return mcp_tool_result({"issues": [issue_json(repo, i) for i in items]})
        if name == "create_pull_request":
            status, payload = rest_create_pull(args.get("owner"), args.get("repo"), {
                "title": args.get("title"), "body": args.get("body") or "",
                "head": args.get("head"), "base": args.get("base"),
            })
            return mcp_tool_result(payload)
        if name == "merge_pull_request":
            number = args.get("pull_number")
            if not isinstance(number, int):
                return mcp_tool_error("pull_number must be an integer")
            status, payload = rest_merge_pull(args.get("owner"), args.get("repo"), number,
                                              {"merge_method": args.get("merge_method") or "merge"})
            return mcp_tool_result(payload)
    except HttpError as err:
        return mcp_tool_error(err.message)
    return mcp_tool_error("unknown tool")


def mcp_handle_one(message):
    """Returns a response dict, or None for notifications."""
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if method is None:
        if "id" not in message:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in message:
        return None  # notification: no response body
    if method == "server/discover":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "supportedVersions": ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "forge-mcp", "version": "1.0.0"},
        }}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": params.get("protocolVersion") or "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "forge-mcp", "version": "1.0.0"},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": MCP_TOOLS}}
    if method == "tools/call":
        tool_name = params.get("name")
        tool = MCP_TOOL_MAP.get(tool_name)
        if tool is None:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32602, "message": "unknown tool: %s" % tool_name}}
        tool_args = params.get("arguments") or {}
        missing = [key for key in tool["inputSchema"].get("required", []) if tool_args.get(key) is None]
        if missing:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32602, "message": "missing required arguments: %s" % ", ".join(missing)}}
        result = mcp_call_tool(tool_name, tool_args)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": "Method not found: %s" % method}}


def handle_mcp(raw_body):
    """Returns (status, payload_obj_or_None). None payload means: 202 empty."""
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    if isinstance(parsed, list):
        responses = []
        for item in parsed:
            response = mcp_handle_one(item)
            if response is not None:
                responses.append(response)
        if not responses:
            return 202, None
        return 200, responses
    response = mcp_handle_one(parsed)
    if response is None:
        return 202, None
    return 200, response


# --------------------------------------------------------------------------
# HTTP plumbing

GIT_PATH_RE = re.compile(r"^/([^/]+)/([^/]+?)\.git/(info/refs|git-upload-pack|git-receive-pack)$")
REPO_PATH_RE = re.compile(r"^/api/v3/repos/([^/]+)/([^/]+)(/.*)?$")
ISSUE_NUM_RE = re.compile(r"^/issues/(\d+)(/comments)?$")
PULL_NUM_RE = re.compile(r"^/pulls/(\d+)(/files|/merge)?$")


class ForgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "forge"

    def log_message(self, format, *args):
        pass

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    # -- response helpers --------------------------------------------------
    def send_bytes(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-GitHub-Request-Id", STORE.request_id())
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, payload, extra_headers=None):
        if status == 204 or payload is None:
            self.send_response(status)
            self.send_header("X-GitHub-Request-Id", STORE.request_id())
            self.end_headers()
            return
        # HARDEN: compact separators — every client parses JSON; the
        # default ", "/": " padding was ~10-20% of small-response bytes
        # and dumps time on the read/write hot paths. Byte output stays
        # deterministic and stable across repeated calls.
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8", extra_headers)

    def send_error_json(self, status, message):
        self.send_json(status, {"message": message})

    # -- request helpers ---------------------------------------------------
    def read_body(self):
        return getattr(self, "_body", b"")

    def _read_body_raw(self):
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        try:
            count = int(length)
        except ValueError:
            return b""
        if count <= 0:
            return b""
        return self.rfile.read(count)

    def authorized(self):
        return self.headers.get("Authorization") == "Bearer " + TOKEN

    def json_body(self):
        raw = self.read_body()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            raise HttpError(400, "Problems parsing JSON")
        if not isinstance(parsed, dict):
            raise HttpError(400, "Problems parsing JSON")
        return parsed

    # -- verbs ---------------------------------------------------------------
    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    # -- routing -------------------------------------------------------------
    def dispatch(self, method):
        # Consume the request body exactly once, up front: error paths that
        # answer without reading it would otherwise poison the keep-alive
        # connection for the next request.
        self._body = b""
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            try:
                self._body = self._read_body_raw()
            except (BrokenPipeError, ConnectionResetError):
                return
        try:
            self.route(method)
        except HttpError as err:
            self.send_error_json(err.status, err.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_error_json(500, "Internal Server Error")
            except Exception:
                pass

    def route(self, method):
        parsed_url = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed_url.path)
        query = urllib.parse.parse_qs(parsed_url.query)

        if path == "/healthz":
            self.send_json(200, {"ok": True})
            return

        git_match = GIT_PATH_RE.match(path)
        if git_match:
            self.route_git(method, git_match.group(1), git_match.group(2), git_match.group(3), query)
            return

        if path == "/api/graphql" and method == "POST":
            if not self.authorized():
                self.send_error_json(401, "Bad credentials")
                return
            raw = self.read_body()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                self.send_json(400, {"errors": [{"message": "Problems parsing JSON"}]})
                return
            status, result = handle_graphql(payload)
            self.send_json(status, result)
            return

        if path == "/mcp":
            self.route_mcp(method)
            return

        if path == "/api/v3" or path.startswith("/api/v3/"):
            self.route_rest(method, path, query)
            return

        self.send_error_json(404, "Not Found")

    def route_git(self, method, owner, name, service, query):
        repo = get_repo(owner, name)
        if repo is None:
            self.send_error_json(404, "Not Found")
            return
        if method == "GET" and service == "info/refs":
            service_name = query.get("service", [""])[0]
            if service_name == "git-upload-pack":
                if self.headers.get("Git-Protocol") == "version=2":
                    self.send_bytes(200, advertisement_v2(),
                                    "application/x-git-upload-pack-advertisement")
                    return
                self.send_bytes(200, advertisement_v0(repo, "git-upload-pack"),
                                "application/x-git-upload-pack-advertisement")
                return
            if service_name == "git-receive-pack":
                self.send_bytes(200, advertisement_v0(repo, "git-receive-pack"),
                                "application/x-git-receive-pack-advertisement")
                return
            self.send_error_json(400, "Missing service parameter")
            return
        if method == "POST" and service == "git-upload-pack":
            body = self.read_body()
            version = 2 if self.headers.get("Git-Protocol") == "version=2" else 0
            if version == 2:
                first_line = b""
                for line in parse_pkt_stream(body):
                    if line:
                        first_line = line
                        break
                if first_line.startswith(b"command=ls-refs"):
                    self.send_bytes(200, handle_ls_refs(repo, body),
                                    "application/x-git-upload-pack-result")
                    return
            self.send_bytes(200, handle_fetch(repo, body, version),
                            "application/x-git-upload-pack-result")
            return
        if method == "POST" and service == "git-receive-pack":
            body = self.read_body()
            response, _ok = handle_receive_pack(repo, body)
            self.send_bytes(200, response, "application/x-git-receive-pack-result")
            return
        self.send_error_json(404, "Not Found")

    def route_mcp(self, method):
        if method == "DELETE":
            self.send_json(204, None)
            return
        if method != "POST":
            self.send_error_json(405, "Method Not Allowed")
            return
        if not self.authorized():
            self.send_error_json(401, "Unauthorized")
            return
        status, payload = handle_mcp(self.read_body())
        if payload is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_json(status, payload)

    def route_rest(self, method, path, query):
        host = self.headers.get("Host", "127.0.0.1")
        public = (method == "GET" and path in ("/api/v3", "/api/v3/", "/api/v3/meta"))
        if not public and not self.authorized():
            self.send_error_json(401, "Bad credentials")
            return

        if path in ("/api/v3", "/api/v3/"):
            if method == "GET":
                self.send_json(200, {
                    "current_user_url": "http://%s/api/v3/user" % host,
                    "repository_url": "http://%s/api/v3/repos/{owner}/{repo}" % host,
                }, {"X-OAuth-Scopes": "repo, read:org"})
                return
            self.send_error_json(404, "Not Found")
            return

        if path == "/api/v3/meta":
            if method == "GET":
                self.send_json(200, {"installed_version": SERVER_VERSION_STRING, "verifiable_password_authentication": False})
                return
            self.send_error_json(404, "Not Found")
            return

        if path == "/api/v3/user":
            if method == "GET":
                self.send_json(200, {"login": VIEWER_LOGIN, "id": 1000, "type": "User"})
                return
            self.send_error_json(404, "Not Found")
            return

        if path == "/api/v3/user/repos":
            if method == "GET":
                status, payload, headers = rest_list_user_repos(query, host)
                self.send_json(status, payload, headers)
                return
            if method == "POST":
                status, payload = rest_create_repo(self.json_body())
                self.send_json(status, payload)
                return
            self.send_error_json(404, "Not Found")
            return

        repo_match = REPO_PATH_RE.match(path)
        if repo_match:
            owner, name, rest = repo_match.group(1), repo_match.group(2), repo_match.group(3) or ""
            self.route_repo(method, owner, name, rest, query)
            return

        self.send_error_json(404, "Not Found")

    def route_repo(self, method, owner, name, rest, query):
        if rest == "":
            if method == "GET":
                status, payload = rest_get_repo(owner, name)
            elif method == "PATCH":
                status, payload = rest_patch_repo(owner, name, self.json_body())
            elif method == "DELETE":
                status, payload = rest_delete_repo(owner, name)
            else:
                self.send_error_json(404, "Not Found")
                return
            self.send_json(status, payload)
            return
        if rest == "/branches" and method == "GET":
            status, payload = rest_list_branches(owner, name)
            self.send_json(status, payload)
            return
        if rest == "/commits" and method == "GET":
            status, payload = rest_list_commits(owner, name, query)
            self.send_json(status, payload)
            return
        if rest.startswith("/contents/") or rest == "/contents":
            content_path = rest[len("/contents/"):] if rest.startswith("/contents/") else ""
            if method == "GET":
                status, payload = rest_get_contents(owner, name, content_path, query)
            elif method == "PUT":
                status, payload = rest_put_contents(owner, name, content_path, self.json_body())
            elif method == "DELETE":
                status, payload = rest_delete_contents(owner, name, content_path, self.json_body())
            else:
                self.send_error_json(404, "Not Found")
                return
            self.send_json(status, payload)
            return
        if rest == "/git/refs" and method == "POST":
            status, payload = rest_create_ref(owner, name, self.json_body())
            self.send_json(status, payload)
            return
        if rest.startswith("/git/refs/") and method == "GET":
            status, payload = rest_get_ref(owner, name, rest[len("/git/refs/"):])
            self.send_json(status, payload)
            return
        if rest == "/labels" and method == "POST":
            status, payload = rest_create_label(owner, name, self.json_body())
            self.send_json(status, payload)
            return
        if rest == "/issues":
            if method == "GET":
                status, payload = rest_list_issues(owner, name, query)
            elif method == "POST":
                status, payload = rest_create_issue(owner, name, self.json_body())
            else:
                self.send_error_json(404, "Not Found")
                return
            self.send_json(status, payload)
            return
        issue_match = ISSUE_NUM_RE.match(rest)
        if issue_match:
            number = int(issue_match.group(1))
            comments = issue_match.group(2)
            if comments:
                if method == "GET":
                    status, payload = rest_list_comments(owner, name, number)
                elif method == "POST":
                    status, payload = rest_create_comment(owner, name, number, self.json_body())
                else:
                    self.send_error_json(404, "Not Found")
                    return
            elif method == "GET":
                status, payload = rest_get_issue(owner, name, number)
            elif method == "PATCH":
                status, payload = rest_patch_issue(owner, name, number, self.json_body())
            else:
                self.send_error_json(404, "Not Found")
                return
            self.send_json(status, payload)
            return
        if rest == "/pulls":
            if method == "GET":
                status, payload = rest_list_pulls(owner, name, query)
            elif method == "POST":
                status, payload = rest_create_pull(owner, name, self.json_body())
            else:
                self.send_error_json(404, "Not Found")
                return
            self.send_json(status, payload)
            return
        pull_match = PULL_NUM_RE.match(rest)
        if pull_match:
            number = int(pull_match.group(1))
            suffix = pull_match.group(2)
            if suffix == "/files" and method == "GET":
                status, payload = rest_pull_files(owner, name, number)
            elif suffix == "/merge" and method == "PUT":
                status, payload = rest_merge_pull(owner, name, number, self.json_body())
            elif suffix is None and method == "GET":
                status, payload = rest_get_pull(owner, name, number)
            else:
                self.send_error_json(404, "Not Found")
                return
            self.send_json(status, payload)
            return
        self.send_error_json(404, "Not Found")


# --------------------------------------------------------------------------
# Lifecycle

def env_request(env_url, method, path, payload=None, timeout=5.0):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(env_url.rstrip("/") + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


class ForgeServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL")
    if not env_url:
        print("MAKEFUN_ENV_URL is not set", file=sys.stderr)
        return 1
    server = ForgeServer(("0.0.0.0", 0), ForgeHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    registered = False
    for _attempt in range(50):
        try:
            env_request(env_url, "POST", "/register", {"port": port})
            registered = True
            break
        except Exception:
            time.sleep(0.2)
    if not registered:
        print("could not register with the trial environment", file=sys.stderr)
    while True:
        try:
            _status, body = env_request(env_url, "GET", "/done", timeout=10.0)
            info = json.loads(body.decode("utf-8"))
            if info.get("done"):
                break
        except Exception:
            pass
        time.sleep(0.5)
    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
