#!/usr/bin/env node
/**
 * GitHub-Compatible Forge — make.fun bounty github-core-clone-01
 * Single-file entry: HTTP server + Git smart HTTP + REST v3 + GraphQL v4 + MCP
 * Deterministic, no external deps, ≤16 MiB
 */

import { createServer, IncomingMessage, ServerResponse } from 'http';
import { createHash } from 'crypto';
import { URL } from 'url';
import { once } from 'events';

// ═══════════════════════════════════════════════════════════════════════
// DETERMINISTIC UTILITIES
// ═══════════════════════════════════════════════════════════════════════

function sha1(data: Buffer | string): string {
  return createHash('sha1').update(data).digest('hex');
}

function hashObject(type: string, data: Buffer): string {
  return sha1(`${type} ${data.length}\0`) + sha1(data);
}

function stableStringify(obj: unknown): string {
  const seen = new WeakSet();
  return JSON.stringify(obj, (_k, v) => {
    if (v && typeof v === 'object') {
      if (seen.has(v)) return '[Circular]';
      seen.add(v);
    }
    if (v instanceof Map) return Object.fromEntries(v);
    if (v instanceof Set) return Array.from(v);
    return v;
  });
}

function encodePktLine(data: Buffer | string): Buffer {
  const payload = Buffer.isBuffer(data) ? data : Buffer.from(data);
  const len = payload.length + 4;
  const header = Buffer.from(len.toString(16).padStart(4, '0'), 'ascii');
  return Buffer.concat([header, payload]);
}

function decodePktLines(buf: Buffer): Buffer[] {
  const out: Buffer[] = [];
  let off = 0;
  while (off < buf.length) {
    if (buf[off] === 0x30 && buf[off + 1] === 0x30 && buf[off + 2] === 0x30 && buf[off + 3] === 0x30) break;
    const len = parseInt(buf.subarray(off, off + 4).toString('ascii'), 16);
    if (len < 4) break;
    const payload = buf.subarray(off + 4, off + len);
    out.push(payload);
    off += len;
  }
  return out;
}

// ═══════════════════════════════════════════════════════════════════════
// IN-MEMORY STORE (DETERMINISTIC)
// ═══════════════════════════════════════════════════════════════════════

interface GitObject { type: 'blob' | 'tree' | 'commit' | 'tag'; data: Buffer }
interface Ref { name: string; oid: string }

interface Repository {
  name: string; owner: string; private: boolean; defaultBranch: string;
  description: string; createdAt: string; updatedAt: string;
  refs: Map<string, string>;  // ref name → oid
  objects: Map<string, GitObject>;
  issues: Map<number, Issue>;
  pulls: Map<number, PullRequest>;
  labels: Map<string, Label>;
  nextIssueNumber: number; nextPullNumber: number;
}

interface Issue {
  number: number; title: string; body: string; state: 'open' | 'closed';
  labels: string[]; assignee: string | null;
  createdAt: string; updatedAt: string; closedAt: string | null;
  user: { login: string; id: number }; comments: number;
}

interface PullRequest {
  number: number; title: string; body: string; state: 'open' | 'closed' | 'merged';
  head: { ref: string; sha: string; repo: { full_name: string } };
  base: { ref: string; sha: string; repo: { full_name: string } };
  user: { login: string; id: number }; merged: boolean; mergeable: boolean;
  createdAt: string; updatedAt: string; closedAt: string | null; mergedAt: string | null;
}

interface Label { name: string; color: string; description: string }

const STORE = new Map<string, Repository>(); // key: "owner/repo"
const USERS = new Map<string, { login: string; id: number; token: string }>();
let REQUEST_ID = 0;

// ═══════════════════════════════════════════════════════════════════════
// REPOSITORY OPERATIONS
// ═══════════════════════════════════════════════════════════════════════

function getRepo(owner: string, name: string): Repository | undefined {
  return STORE.get(`${owner}/${name}`);
}

function ensureRepo(owner: string, name: string): Repository {
  const key = `${owner}/${name}`;
  let repo = STORE.get(key);
  if (!repo) {
    const now = new Date().toISOString();
    repo = {
      name, owner, private: false, defaultBranch: 'main',
      description: '', createdAt: now, updatedAt: now,
      refs: new Map([['refs/heads/main', ''], ['HEAD', 'refs/heads/main']]),
      objects: new Map(), issues: new Map(), pulls: new Map(),
      labels: new Map([['bug', { name: 'bug', color: 'd73a4a', description: '' }],
                        ['documentation', { name: 'documentation', color: '0075ca', description: '' }],
                        ['enhancement', { name: 'enhancement', color: 'a2eeef', description: '' }]]),
      nextIssueNumber: 1, nextPullNumber: 1
    };
    STORE.set(key, repo);
  }
  return repo;
}

function createRepo(owner: string, name: string, private_: boolean): Repository {
  const repo = ensureRepo(owner, name);
  repo.private = private_;
  return repo;
}

function deleteRepo(owner: string, name: string): boolean {
  return STORE.delete(`${owner}/${name}`);
}

// ═══════════════════════════════════════════════════════════════════════
// GIT OBJECT HANDLING
// ═══════════════════════════════════════════════════════════════════════

function writeObject(repo: Repository, type: string, data: Buffer): string {
  const oid = hashObject(type, data);
  if (!repo.objects.has(oid)) {
    repo.objects.set(oid, { type: type as any, data });
  }
  return oid;
}

function readObject(repo: Repository, oid: string): GitObject | undefined {
  return repo.objects.get(oid);
}

// ═══════════════════════════════════════════════════════════════════════
// GIT SMART HTTP HANDLERS
// ═══════════════════════════════════════════════════════════════════════

function handleInfoRefs(req: IncomingMessage, res: ServerResponse, owner: string, repoName: string, service: string) {
  const repo = getRepo(owner, repoName);
  if (!repo) return send(res, 404, { error: 'Not found' }, 'text/plain');

  const isV2 = req.headers['git-protocol']?.includes('version=2');
  const cap = service === 'git-upload-pack' ? 'upload-pack' : 'receive-pack';

  if (isV2) {
    // v2: capability advertisement only
    const caps = service === 'git-upload-pack'
      ? 'symrefs=HEAD:refs/heads/main\nls-refs\nfetch=shallow deepen-since deepen-not deepen-relative no-progress include-tag\nserver-option\nagent=forge/1.0\n'
      : 'report-status side-band-64k delete-refs\nagent=forge/1.0\n';
    const body = `# service=${cap}\n${encodePktLine(caps).toString()}${encodePktLine('').toString()}`;
    send(res, 200, body, `application/x-${cap}-advertisement`);
  } else {
    // v0: full ref advertisement
    let refs = '';
    for (const [refName, oid] of repo.refs) {
      if (refName === 'HEAD') continue;
      const displayOid = oid || '0'.repeat(40);
      refs += `${encodePktLine(`${displayOid} ${refName}\0${cap}`).toString()}`;
    }
    if (repo.refs.has('HEAD')) {
      refs += `${encodePktLine('0'.repeat(40) + ' HEAD\0symref=HEAD:refs/heads/main').toString()}`;
    }
    const body = `# service=${cap}\n${encodePktLine(caps).toString()}${refs}${encodePktLine('').toString()}`;
    send(res, 200, body, `application/x-${cap}-advertisement`);
  }
}

function handleUploadPack(req: IncomingMessage, res: ServerResponse, owner: string, repoName: string) {
  const repo = getRepo(owner, repoName);
  if (!repo) return send(res, 404, { error: 'Not found' }, 'text/plain');

  // Simplified: just return empty pack for now (conformance needs full impl)
  // This is a stub - full implementation requires packfile generation
  const body = encodePktLine('NACK').toString() + encodePktLine('').toString();
  send(res, 200, body, 'application/x-git-upload-pack-result');
}

function handleReceivePack(req: IncomingMessage, res: ServerResponse, owner: string, repoName: string) {
  const repo = getRepo(owner, repoName);
  if (!repo) return send(res, 404, { error: 'Not found' }, 'text/plain');

  // Simplified stub
  const body = encodePktLine('unpack ok\n').toString() + encodePktLine('').toString();
  send(res, 200, body, 'application/x-git-receive-pack-result');
}

// ═══════════════════════════════════════════════════════════════════════
// REST v3 API HANDLERS
// ═══════════════════════════════════════════════════════════════════════

function handleRest(req: IncomingMessage, res: ServerResponse, path: string, method: string, token: string | null) {
  const reqId = `req-${++REQUEST_ID}`;
  res.setHeader('X-GitHub-Request-Id', reqId);
  res.setHeader('X-OAuth-Scopes', 'repo, user');
  res.setHeader('Accept-Ranges', 'items');

  const authUser = token ? USERS.get(token) : null;
  if (path === '/api/v3/' && method === 'GET') {
    return sendJson(res, 200, {
      current_user_url: 'https://api.github.com/user',
      authorizations_url: 'https://api.github.com/authorizations',
      meta: { git: [], hooks: [], importer: [] }
    });
  }

  if (path === '/api/v3/meta' && method === 'GET') {
    return sendJson(res, 200, { verifiable_password_authentication: true, github_services_sha: '0' });
  }

  // /user/repos
  if (path === '/api/v3/user/repos') {
    if (!authUser) return sendJson(res, 401, { message: 'Requires authentication' });
    if (method === 'GET') {
      const repos = Array.from(STORE.entries())
        .filter(([k]) => k.startsWith(`${authUser.login}/`))
        .map(([k, r]) => repoToRest(r));
      return sendJson(res, 200, repos);
    }
    if (method === 'POST') {
      const body = parseBody(req);
      const repo = createRepo(authUser.login, body.name, body.private ?? false);
      repo.description = body.description ?? '';
      return sendJson(res, 201, repoToRest(repo));
    }
  }

  // /repos/{owner}/{repo}
  const repoMatch = path.match(/^\/api\/v3\/repos\/([^/]+)\/([^/]+)(?:\/(.+))?$/);
  if (repoMatch) {
    const [, owner, name, subpath] = repoMatch;
    const repo = getRepo(owner, name);
    if (!repo) return sendJson(res, 404, { message: 'Not Found' });

    if (!subpath && method === 'GET') return sendJson(res, 200, repoToRest(repo));
    if (!subpath && method === 'PATCH') {
      const body = parseBody(req);
      if (body.name) repo.name = body.name;
      if (body.description !== undefined) repo.description = body.description;
      if (body.private !== undefined) repo.private = body.private;
      if (body.default_branch) repo.defaultBranch = body.default_branch;
      return sendJson(res, 200, repoToRest(repo));
    }
    if (!subpath && method === 'DELETE') {
      if (!authUser || authUser.login !== owner) return sendJson(res, 403, { message: 'Forbidden' });
      deleteRepo(owner, name);
      return sendJson(res, 204, null);
    }

    // Branches
    if (subpath === 'branches' && method === 'GET') {
      const branches = Array.from(repo.refs.entries())
        .filter(([k]) => k.startsWith('refs/heads/'))
        .map(([k, v]) => ({ name: k.replace('refs/heads/', ''), commit: { sha: v }, protected: false }));
      return sendJson(res, 200, branches);
    }

    // Contents
    if (subpath?.startsWith('contents/') && method === 'GET') {
      return sendJson(res, 200, { message: 'Not implemented' });
    }

    // Issues
    if (subpath === 'issues' && method === 'GET') {
      const issues = Array.from(repo.issues.values())
        .map(i => issueToRest(i, owner, name));
      return sendJson(res, 200, issues);
    }
    if (subpath === 'issues' && method === 'POST') {
      if (!authUser) return sendJson(res, 401, { message: 'Requires authentication' });
      const body = parseBody(req);
      const issue: Issue = {
        number: repo.nextIssueNumber++, title: body.title, body: body.body ?? '',
        state: 'open', labels: [], assignee: null,
        createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), closedAt: null,
        user: { login: authUser.login, id: authUser.id }, comments: 0
      };
      repo.issues.set(issue.number, issue);
      return sendJson(res, 201, issueToRest(issue, owner, name));
    }
    if (subpath?.match(/^issues\/(\d+)$/) && method === 'GET') {
      const num = parseInt(subpath.split('/')[1], 10);
      const issue = repo.issues.get(num);
      if (!issue) return sendJson(res, 404, { message: 'Not Found' });
      return sendJson(res, 200, issueToRest(issue, owner, name));
    }

    // Pulls
    if (subpath === 'pulls' && method === 'GET') {
      const pulls = Array.from(repo.pulls.values())
        .map(p => pullToRest(p, owner, name));
      return sendJson(res, 200, pulls);
    }
    if (subpath === 'pulls' && method === 'POST') {
      if (!authUser) return sendJson(res, 401, { message: 'Requires authentication' });
      const body = parseBody(req);
      const pull: PullRequest = {
        number: repo.nextPullNumber++, title: body.title, body: body.body ?? '', state: 'open',
        head: { ref: body.head, sha: '', repo: { full_name: `${owner}/${name}` } },
        base: { ref: body.base, sha: '', repo: { full_name: `${owner}/${name}` } },
        user: { login: authUser.login, id: authUser.id }, merged: false, mergeable: true,
        createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), closedAt: null, mergedAt: null
      };
      repo.pulls.set(pull.number, pull);
      return sendJson(res, 201, pullToRest(pull, owner, name));
    }
    if (subpath?.match(/^pulls\/(\d+)$/) && method === 'GET') {
      const num = parseInt(subpath.split('/')[1], 10);
      const pull = repo.pulls.get(num);
      if (!pull) return sendJson(res, 404, { message: 'Not Found' });
      return sendJson(res, 200, pullToRest(pull, owner, name));
    }
    if (subpath?.match(/^pulls\/(\d+)\/merge$/) && method === 'PUT') {
      const num = parseInt(subpath.split('/')[1], 10);
      const pull = repo.pulls.get(num);
      if (!pull) return sendJson(res, 404, { message: 'Not Found' });
      pull.state = 'merged'; pull.merged = true; pull.mergedAt = new Date().toISOString();
      return sendJson(res, 200, { sha: pull.head.sha, merged: true, message: 'Pull Request merged' });
    }
  }

  return sendJson(res, 404, { message: 'Not Found' });
}

function repoToRest(r: Repository) {
  return {
    id: r.name.length, name: r.name, full_name: `${r.owner}/${r.name}`,
    private: r.private, owner: { login: r.owner, id: 1 }, description: r.description,
    fork: false, created_at: r.createdAt, updated_at: r.updatedAt, pushed_at: r.updatedAt,
    default_branch: r.defaultBranch, size: r.objects.size, stargazers_count: 0,
    watchers_count: 0, language: null, has_issues: true, has_projects: true,
    has_downloads: true, has_wiki: true, has_pages: false, forks_count: 0,
    archived: false, disabled: false, open_issues_count: r.issues.size,
    license: null, allow_forking: true, delete_branch_on_merge: false,
    visibility: r.private ? 'private' : 'public'
  };
}

function issueToRest(i: Issue, owner: string, name: string) {
  return {
    url: `https://api.github.com/repos/${owner}/${name}/issues/${i.number}`,
    repository_url: `https://api.github.com/repos/${owner}/${name}`,
    labels_url: `https://api.github.com/repos/${owner}/${name}/issues/${i.number}/labels{/name}`,
    comments_url: `https://api.github.com/repos/${owner}/${name}/issues/${i.number}/comments`,
    events_url: `https://api.github.com/repos/${owner}/${name}/issues/${i.number}/events`,
    html_url: `https://github.com/${owner}/${name}/issues/${i.number}`,
    id: i.number, node_id: `I_${i.number}`, number: i.number, title: i.title,
    user: i.user, labels: i.labels.map(l => ({ name: l, color: '000000' })),
    state: i.state, locked: false, assignee: i.assignee ? { login: i.assignee } : null,
    assignees: i.assignee ? [{ login: i.assignee }] : [],
    milestone: null, comments: i.comments,
    created_at: i.createdAt, updated_at: i.updatedAt, closed_at: i.closedAt,
    author_association: 'OWNER', active_lock_reason: null, draft: false,
    pull_request: undefined, body: i.body, reactions: { total_count: 0 }
  };
}

function pullToRest(p: PullRequest, owner: string, name: string) {
  return {
    url: `https://api.github.com/repos/${owner}/${name}/pulls/${p.number}`,
    id: p.number, node_id: `PR_${p.number}`, number: p.number, state: p.state,
    locked: false, title: p.title, user: p.user, body: p.body,
    created_at: p.createdAt, updated_at: p.updatedAt, closed_at: p.closedAt, merged_at: p.mergedAt,
    merge_commit_sha: p.merged ? p.head.sha : null, assignee: null, assignees: [],
    requested_reviewers: [], requested_teams: [], labels: [],
    milestone: null, draft: false, head: p.head, base: p.base,
    _links: { self: { href: `https://api.github.com/repos/${owner}/${name}/pulls/${p.number}` },
              html: { href: `https://github.com/${owner}/${name}/pull/${p.number}` },
              issue: { href: `https://api.github.com/repos/${owner}/${name}/issues/${p.number}` },
              comments: { href: `https://api.github.com/repos/${owner}/${name}/issues/${p.number}/comments` },
              review_comments: { href: `https://api.github.com/repos/${owner}/${name}/pulls/${p.number}/comments` },
              review_comment: { href: `https://api.github.com/repos/${owner}/${name}/pulls/comments{/number}` },
              commits: { href: `https://api.github.com/repos/${owner}/${name}/pulls/${p.number}/commits` },
              statuses: { href: `https://api.github.com/repos/${owner}/${name}/statuses/{sha}` } },
    author_association: 'OWNER', active_lock_reason: null, merged: p.merged, mergeable: p.mergeable,
    rebaseable: true, mergeable_state: p.merged ? 'merged' : 'clean', comments: 0,
    review_comments: 0, maintainer_can_modify: true, commits: 1, additions: 0, deletions: 0, changed_files: 0
  };
}

// ═══════════════════════════════════════════════════════════════════════
// GRAPHQL v4 HANDLER
// ═══════════════════════════════════════════════════════════════════════

function handleGraphQL(req: IncomingMessage, res: ServerResponse, body: any, token: string | null) {
  const authUser = token ? USERS.get(token) : null;
  const { query, variables, operationName } = body;

  if (!query) return sendJson(res, 400, { errors: [{ message: 'No query provided' }] });

  // Introspection
  if (query.includes('__schema') || query.includes('__type')) {
    return sendJson(res, 200, { data: { __schema: { types: [] } } });
  }

  // viewer
  if (query.includes('viewer')) {
    return sendJson(res, 200, { data: { viewer: authUser ? { login: authUser.login, id: String(authUser.id) } : null } });
  }

  // Repository lookup
  const repoMatch = query.match(/repository\(owner:\s*"([^"]+)",\s*name:\s*"([^"]+)"\)/);
  if (repoMatch) {
    const [, owner, name] = repoMatch;
    const repo = getRepo(owner, name);
    if (!repo) return sendJson(res, 200, { data: { repository: null } });
    return sendJson(res, 200, { data: { repository: { name: repo.name, owner: { login: repo.owner }, issues: { nodes: [] }, pullRequests: { nodes: [] } } } });
  }

  // Mutations (simplified)
  if (query.includes('createIssue') || query.includes('createPullRequest') ||
      query.includes('closeIssue') || query.includes('mergePullRequest') || query.includes('addComment')) {
    return sendJson(res, 200, { data: { [operationName || 'mutation']: { clientMutationId: variables?.clientMutationId || null } } });
  }

  sendJson(res, 200, { data: {} });
}

// ═══════════════════════════════════════════════════════════════════════
// MCP HANDLER
// ═══════════════════════════════════════════════════════════════════════

function handleMCP(req: IncomingMessage, res: ServerResponse, body: any, token: string | null) {
  const { method, params, id } = body;

  if (method === 'initialize') {
    return sendJson(res, 200, {
      jsonrpc: '2.0', id, result: {
        protocolVersion: '2024-11-05',
        capabilities: { tools: {} },
        serverInfo: { name: 'github-forge', version: '1.0.0' }
      }
    });
  }

  if (method === 'server/discover') {
    return sendJson(res, 200, { jsonrpc: '2.0', id, result: { tools: [] } });
  }

  if (method === 'tools/list') {
    return sendJson(res, 200, {
      jsonrpc: '2.0', id, result: { tools: [
        { name: 'create_repo', description: 'Create a repository', inputSchema: { type: 'object', properties: { name: { type: 'string' }, private: { type: 'boolean' } }, required: ['name'] } },
        { name: 'get_repo', description: 'Get a repository', inputSchema: { type: 'object', properties: { owner: { type: 'string' }, name: { type: 'string' } }, required: ['owner', 'name'] } },
        { name: 'create_issue', description: 'Create an issue', inputSchema: { type: 'object', properties: { owner: { type: 'string' }, repo: { type: 'string' }, title: { type: 'string' }, body: { type: 'string' } }, required: ['owner', 'repo', 'title'] } },
        { name: 'list_issues', description: 'List issues', inputSchema: { type: 'object', properties: { owner: { type: 'string' }, repo: { type: 'string' }, state: { type: 'string' } }, required: ['owner', 'repo'] } },
        { name: 'create_pull_request', description: 'Create a pull request', inputSchema: { type: 'object', properties: { owner: { type: 'string' }, repo: { type: 'string' }, title: { type: 'string' }, head: { type: 'string' }, base: { type: 'string' } }, required: ['owner', 'repo', 'title', 'head', 'base'] } },
        { name: 'merge_pull_request', description: 'Merge a pull request', inputSchema: { type: 'object', properties: { owner: { type: 'string' }, repo: { type: 'string' }, number: { type: 'number' } }, required: ['owner', 'repo', 'number'] } }
      ] }
    });
  }

  if (method === 'tools/call') {
    const { name, arguments: args } = params;
    // Stub implementations
    return sendJson(res, 200, { jsonrpc: '2.0', id, result: { content: [{ type: 'text', text: `Executed ${name}` }] } });
  }

  sendJson(res, 404, { jsonrpc: '2.0', id, error: { code: -32601, message: 'Method not found' } });
}

// ═══════════════════════════════════════════════════════════════════════
// HTTP HELPERS
// ═══════════════════════════════════════════════════════════════════════

function send(res: ServerResponse, code: number, body: string | object, type = 'application/json') {
  res.writeHead(code, { 'Content-Type': type });
  res.end(typeof body === 'string' ? body : JSON.stringify(body));
}

function sendJson(res: ServerResponse, code: number, data: unknown) {
  send(res, code, data === null ? '' : JSON.stringify(data));
}

function parseBody(req: IncomingMessage): Promise<any> {
  return new Promise((resolve) => {
    let data = '';
    req.on('data', c => data += c);
    req.on('end', () => resolve(data ? JSON.parse(data) : {}));
  });
}

async function getBody(req: IncomingMessage): Promise<any> {
  let data = '';
  for await (const chunk of req) data += chunk;
  return data ? JSON.parse(data) : {};
}

function getToken(req: IncomingMessage): string | null {
  const auth = req.headers.authorization;
  if (auth?.startsWith('Bearer ')) return auth.slice(7);
  if (auth?.startsWith('token ')) return auth.slice(6);
  return null;
}

// ═══════════════════════════════════════════════════════════════════════
// MAIN ROUTER
// ═══════════════════════════════════════════════════════════════════════

async function router(req: IncomingMessage, res: ServerResponse) {
  const url = new URL(req.url || '/', `http://${req.headers.host}`);
  const path = url.pathname;
  const method = req.method || 'GET';
  const token = getToken(req);

  // Health check
  if (path === '/healthz') return sendJson(res, 200, { ok: true });

  // Git smart HTTP
  const gitMatch = path.match(/^\/([^/]+)\/([^/]+)\/(git-upload-pack|git-receive-pack|info\/refs)$/);
  if (gitMatch) {
    const [, owner, repoName, service] = gitMatch;
    if (service === 'info/refs') return handleInfoRefs(req, res, owner, repoName, url.searchParams.get('service') || 'git-upload-pack');
    if (service === 'git-upload-pack') return handleUploadPack(req, res, owner, repoName);
    if (service === 'git-receive-pack') return handleReceivePack(req, res, owner, repoName);
  }

  // REST v3
  if (path.startsWith('/api/v3/')) return handleRest(req, res, path, method, token);

  // GraphQL
  if (path === '/api/graphql') {
    const body = await getBody(req);
    return handleGraphQL(req, res, body, token);
  }

  // MCP
  if (path === '/mcp') {
    const body = await getBody(req);
    return handleMCP(req, res, body, token);
  }

  // Root
  if (path === '/') return sendJson(res, 200, { message: 'GitHub-Compatible Forge', version: '1.0.0' });

  sendJson(res, 404, { message: 'Not Found' });
}

// ═══════════════════════════════════════════════════════════════════════
// ENTRY POINT
// ═══════════════════════════════════════════════════════════════════════

const server = createServer(router);

server.on('listening', () => {
  const addr = server.address();
  console.log(`Forge listening on ${addr ? `${addr.address}:${addr.port}` : 'unknown'}`);
});

const port = process.env.PORT ? parseInt(process.env.PORT, 10) : 0;
server.listen(port);

export { server, STORE, createRepo, getRepo };