#!/usr/bin/env node
// Extract the (HttpOnly, session-only) SVPNCOOKIE for vpn.hampshire.edu out of
// floorp's sessionstore, so the VPN toggle can reuse the portal login without a
// browser extension or debug port.
//   node svpncookie.js            -> prints the cookie value (nothing if not logged in)
//   node svpncookie.js container  -> prints the Firefox container name holding it
// The cookie is container-scoped (originAttributes.userContextId); the toggle
// names that container when telling you to sign out, since xdg-open lands in the
// default container, not yours.
const fs = require('fs'), os = require('os'), path = require('path');

// mozlz4 = "mozLz40\0" + uint32 LE size + raw LZ4 block
function lz4dec(src, size) {
  const dst = Buffer.allocUnsafe(size);
  let s = 0, d = 0;
  while (s < src.length) {
    const tok = src[s++];
    let ll = tok >> 4;
    if (ll === 15) { let b; do { b = src[s++]; ll += b; } while (b === 255); }
    src.copy(dst, d, s, s + ll); d += ll; s += ll;
    if (s >= src.length) break;
    const off = src[s++] | (src[s++] << 8);
    let ml = tok & 15;
    if (ml === 15) { let b; do { b = src[s++]; ml += b; } while (b === 255); }
    ml += 4;
    let m = d - off;
    for (let i = 0; i < ml; i++) dst[d++] = dst[m++];
  }
  return dst.subarray(0, d);
}

const base = path.join(os.homedir(), '.floorp');
let file = null, newest = 0, profiles = [];
try { profiles = fs.readdirSync(base); } catch { process.exit(0); }
for (const p of profiles) {
  const f = path.join(base, p, 'sessionstore-backups', 'recovery.jsonlz4');
  try { const m = fs.statSync(f).mtimeMs; if (m > newest) { newest = m; file = f; } } catch {}
}
if (!file) process.exit(0);

let json;
try {
  const buf = fs.readFileSync(file);
  if (buf.subarray(0, 8).toString('latin1') !== 'mozLz40\0') process.exit(0);
  json = JSON.parse(lz4dec(buf.subarray(12), buf.readUInt32LE(8)).toString('utf8'));
} catch { process.exit(0); }

const isMatch = c => !!(c && typeof c === 'object' && !Array.isArray(c)
  && c.name === 'SVPNCOOKIE' && /(^|\.)vpn\.hampshire\.edu$/i.test(c.host || '') && c.value);

// Live session cookies sit in the top-level (or per-window) `cookies` arrays;
// prefer those so a stale copy buried in closed-tab/window state can't shadow
// the current login. The deep walk stays as a fallback for older layouts.
let hit = [].concat(json.cookies || [], ...(json.windows || []).map(w => w.cookies || [])).find(isMatch);
if (!hit) (function walk(o) {
  if (hit || !o || typeof o !== 'object') return;
  if (Array.isArray(o)) { for (const x of o) walk(x); return; }
  if (isMatch(o)) { hit = o; return; }
  for (const k in o) walk(o[k]);
})(json);
const value = hit ? hit.value : '';
const ctxId = (hit && hit.originAttributes && hit.originAttributes.userContextId) || 0;

if (process.argv[2] === 'container') {
  if (value && ctxId) {
    try {
      const cj = JSON.parse(fs.readFileSync(path.join(path.dirname(path.dirname(file)), 'containers.json')));
      const id = (cj.identities || []).find(i => i.userContextId === ctxId);
      if (id && id.name) process.stdout.write(id.name);
    } catch {}
  }
} else if (value) {
  process.stdout.write(value);
}
