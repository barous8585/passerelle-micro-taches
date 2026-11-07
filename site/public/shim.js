/* Notre Château sur Netlify : la porte d'entrée (comptes) et le pont entre le musée et son serveur.
   Le musée a été écrit pour une page Claude ; ce fichier lui offre exactement les mêmes outils
   (base partagée, fichiers, comptes, présence en direct, téléchargements), servis par Netlify. */
(() => {
  const api = async (path, opt = {}) => {
    const o = Object.assign({ credentials: 'same-origin' }, opt);
    if (opt.json !== undefined) { o.method = o.method || 'POST'; o.headers = { 'content-type': 'application/json' }; o.body = JSON.stringify(opt.json); delete o.json; }
    const r = await fetch('/api/' + path, o); let d = null; try { d = await r.json(); } catch (e) {}
    if (!r.ok) { const e = new Error((d && d.message) || 'Erreur ' + r.status); e.code = (d && d.code) || 'http_' + r.status; e.status = r.status; throw e; }
    return d;
  };
  const clone = (x) => (x == null ? x : JSON.parse(JSON.stringify(x)));
  let ME = null;

  /* ───────── base partagée (cache local + synchronisation régulière) ───────── */
  const cache = new Map(); const colSubs = new Map(); const docSubs = new Map();
  let since = 0, ready = null, syncing = false, timer = null;
  const parentOf = (p) => p.split('/').slice(0, -1).join('/');
  const snapDoc = (path) => ({ id: path.split('/').pop(), ref: { path }, exists: cache.has(path), data: () => clone(cache.get(path)), metadata: { fromCache: false, hasPendingWrites: false } });
  const snapCol = (col) => { const depth = col.split('/').length + 1; const docs = [...cache.keys()].filter((k) => k.startsWith(col + '/') && k.split('/').length === depth).sort().map(snapDoc); return { docs, size: docs.length, empty: !docs.length, docChanges: () => [], metadata: {} }; };
  function notify(paths) {
    const cols = new Set(), ds = new Set();
    paths.forEach((p) => { cols.add(parentOf(p)); ds.add(p); });
    cols.forEach((c) => (colSubs.get(c) || []).forEach((fn) => { try { fn(snapCol(c)); } catch (e) { console.error(e); } }));
    ds.forEach((p) => (docSubs.get(p) || []).forEach((fn) => { try { fn(snapDoc(p)); } catch (e) { console.error(e); } }));
  }
  async function sync() {
    if (syncing) return; syncing = true;
    try {
      const r = await api('sync?since=' + since);
      const changed = [];
      if (r.full) { const old = [...cache.keys()]; cache.clear(); old.forEach((k) => changed.push(k)); }
      r.changes.forEach((c) => { if (c.data === null) cache.delete(c.path); else cache.set(c.path, c.data); changed.push(c.path); });
      since = r.now;
      if (r.full) { colSubs.forEach((_, c) => changed.push(c + '/__')); }
      if (changed.length) notify(changed);
    } catch (e) { if (e.status === 401) location.reload(); }
    syncing = false;
  }
  function loop() { clearTimeout(timer); timer = setTimeout(async () => { await sync(); loop(); }, document.hidden ? 30000 : 4000); }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && ready) { sync(); loop(); } });
  const autoId = () => { const a = 'abcdefghijklmnopqrstuvwxyz0123456789'; let s = ''; for (let i = 0; i < 20; i++) s += a[Math.floor(Math.random() * a.length)]; return s; };
  const docRef = (path) => ({
    id: path.split('/').pop(), path,
    async get() { await ready; if (cache.has(path)) return snapDoc(path); const r = await api('doc?path=' + encodeURIComponent(path)); if (r.exists) cache.set(path, r.data); return snapDoc(path); },
    async set(data) { cache.set(path, clone(data)); notify([path]); await api('doc?path=' + encodeURIComponent(path), { method: 'PUT', json: { data } }); },
    async update(data) {
      await ready;
      if (!cache.has(path)) { const r = await api('doc?path=' + encodeURIComponent(path)); if (!r.exists) { const e = new Error('Document introuvable'); e.code = 'invalid_argument'; throw e; } cache.set(path, r.data); }
      cache.set(path, Object.assign(clone(cache.get(path)), clone(data))); notify([path]);
      await api('doc?path=' + encodeURIComponent(path), { method: 'PATCH', json: { data } });
    },
    async delete() { cache.delete(path); notify([path]); await api('doc?path=' + encodeURIComponent(path), { method: 'DELETE' }); },
    onSnapshot(next) { const a = docSubs.get(path) || []; a.push(next); docSubs.set(path, a); ready.then(() => next(snapDoc(path))); return () => { const b = docSubs.get(path) || []; docSubs.set(path, b.filter((f) => f !== next)); }; },
  });
  const collection = (path) => ({
    path, doc: (id) => docRef(path + '/' + (id || autoId())),
    onSnapshot(next) { const a = colSubs.get(path) || []; a.push(next); colSubs.set(path, a); ready.then(() => next(snapCol(path))); return () => { const b = colSubs.get(path) || []; colSubs.set(path, b.filter((f) => f !== next)); }; },
  });
  const db = { doc: docRef, collection };

  /* ───────── fichiers ───────── */
  const PART = 4 * 1024 * 1024;
  const assets = {
    async upload(blob, opt = {}) {
      const type = opt.type || blob.type || 'application/octet-stream';
      const { id } = await api('asset/start', { json: { type, size: blob.size } });
      const n = Math.max(1, Math.ceil(blob.size / PART));
      for (let i = 0; i < n; i++) await api('asset/part?id=' + id + '&n=' + i, { method: 'PUT', body: blob.slice(i * PART, (i + 1) * PART) });
      return await api('asset/finish', { json: { id, parts: n } });
    },
    async delete(id) { await api('asset?id=' + encodeURIComponent(id), { method: 'DELETE' }); },
    url: (id) => '/_blob/' + id,
  };

  /* ───────── comptes ───────── */
  const profilesCache = {};
  const user = {
    me: async () => ({ id: ME.id, isOwner: ME.isOwner, name: ME.name }),
    can: async () => true, isOwner: async () => ME.isOwner,
    async profiles(ids) { const want = [].concat(ids).filter((i) => !(i in profilesCache)); if (want.length) Object.assign(profilesCache, await api('profiles', { json: { ids: want } })); const o = {}; [].concat(ids).forEach((i) => { if (profilesCache[i]) o[i] = profilesCache[i]; }); return o; },
  };

  /* ───────── présence en direct ───────── */
  const R = { pres: null, sentAt: 0, peerFn: null, topics: {}, peers: new Map(), queue: [], evSince: Date.now(), t: null };
  async function roomTick() {
    clearTimeout(R.t);
    if (!document.hidden) {
      try {
        const body = { since: R.evSince };
        if (R.pres && Date.now() - R.sentAt > 1500) { body.presence = R.pres; R.sentAt = Date.now(); }
        if (R.queue.length) body.emit = R.queue.shift().m;
        const r = await api('room', { json: body });
        const now = new Map(); r.peers.forEach((p) => now.set(p.by, { peer: p.by, by: p.by, isMe: p.by === r.me, kind: 'viewer', presence: p.presence }));
        const joined = [], updated = [], left = [];
        now.forEach((p, k) => (R.peers.has(k) ? updated : joined).push(p)); R.peers.forEach((p, k) => { if (!now.has(k)) left.push(p); });
        R.peers = now; if (R.peerFn && (joined.length || updated.length || left.length)) R.peerFn({ peers: [...now.values()], joined, updated, left });
        r.events.forEach((e) => { R.evSince = Math.max(R.evSince, e.at); const fn = R.topics[e.topic]; if (fn) fn({ topic: e.topic, data: e.data, by: e.by, peer: e.by, isMe: e.by === r.me }); });
      } catch (e) {}
    }
    R.t = setTimeout(roomTick, R.peers.size > 1 ? 2000 : 6000);
  }
  const room = {
    async presence(st) { R.pres = st; },
    onPeers(fn) { R.peerFn = fn; return () => { R.peerFn = null; }; },
    on(t, fn) { R.topics[t] = fn; return () => { delete R.topics[t]; }; },
    emit(topic, data) { return new Promise((res) => { R.queue.push({ m: { topic, data } }); res({ ok: true }); roomTick(); }); },
  };

  /* ───────── téléchargements ───────── */
  const downloads = { async save({ filename, data }) { const b = data instanceof Blob ? data : new Blob([data]); const u = URL.createObjectURL(b); const a = document.createElement('a'); a.href = u; a.download = filename; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(u), 60000); return { status: 'saved' }; } };

  /* ───────── outils propres à la version Netlify (inviter, mot de passe, déconnexion, import) ───────── */
  window.__chateauNetlify = {
    async invite() { const { code } = await api('invite', { json: {} }); return location.origin + '/?invite=' + code; },
    async password(current, next) { await api('password', { json: { current, next } }); },
    async logout() { await api('logout', { json: {} }); location.href = '/'; },
    async importFile(file, progress) {
      if (progress) progress('Envoi du fichier…');
      const up = await assets.upload(file, { type: 'application/x-chateau-import' });
      if (progress) progress('Installation de vos souvenirs…');
      const r = await fetch('/import-chateau', { method: 'POST', credentials: 'same-origin', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ asset: up.id }) });
      const d = await r.json().catch(() => ({})); if (!r.ok) throw Object.assign(new Error(d.message || 'Import impossible'), { code: d.code });
      since = 0; await sync(); return d;
    },
    get me() { return ME; },
  };

  /* ───────── la porte : connexion / création de compte ───────── */
  let resolveReady; window.__claudeReady = new Promise((r) => (resolveReady = r));
  const css = `
  #door{position:fixed;inset:0;z-index:100000;display:flex;align-items:center;justify-content:center;padding:20px;background:radial-gradient(120% 90% at 50% 0%,#1c2350 0%,#0b0f24 60%,#05060f 100%);font-family:"Jost",system-ui,sans-serif;color:#f6f1e7}
  #door .box{width:min(400px,100%);text-align:center}
  #door .k{font-size:11px;letter-spacing:.32em;text-transform:uppercase;color:#d6b26e}
  #door h1{font-family:"Cormorant Garamond",Georgia,serif;font-style:italic;font-weight:500;font-size:44px;margin:10px 0 6px;line-height:1.05}
  #door p{color:rgba(246,241,231,.7);font-size:14.5px;line-height:1.55;margin:0 0 18px}
  #door input{width:100%;box-sizing:border-box;margin:6px 0;padding:13px 15px;border-radius:12px;border:1px solid rgba(214,178,110,.35);background:rgba(255,255,255,.06);color:#fff;font:inherit;font-size:16px}
  #door button{width:100%;margin-top:12px;padding:14px;border-radius:999px;border:0;background:linear-gradient(135deg,#f3c66b,#d6a24e);color:#2a1a10;font:inherit;font-weight:600;letter-spacing:.08em;text-transform:uppercase;font-size:13px;cursor:pointer}
  #door button:disabled{opacity:.6}
  #door .alt{margin-top:14px;font-size:13px;color:rgba(246,241,231,.65)} #door .alt a{color:#f3c66b;cursor:pointer;text-decoration:underline}
  #door .err{min-height:20px;margin-top:10px;color:#ff9db0;font-size:13.5px}
  #door .heart{font-size:30px;margin-bottom:4px}`;
  // l'invitation du QR code offert ne sert qu'une fois : ensuite, on propose simplement de se connecter
  function inviteOf(st) { const i = new URLSearchParams(location.search).get('invite'); return i && /^cadeau-/.test(i) && st && st.giftOpen === false ? null : i; }
  function door(mode, st) {
    let el = document.getElementById('door'); if (!el) { const s = document.createElement('style'); s.textContent = css; document.head.appendChild(s); el = document.createElement('div'); el.id = 'door'; document.body.appendChild(el); }
    const invite = inviteOf(st);
    const first = !st.hasOwner;
    if (mode === 'register' && !first && !invite) mode = 'login';
    const reg = mode === 'register';
    el.innerHTML = `<div class="box"><div class="heart">♡</div><div class="k">Notre château</div>
      <h1>${first ? 'Bâtir le château' : reg ? 'Bienvenue chez nous' : 'Te revoilà'}</h1>
      <p>${first ? 'Crée ton compte : tu seras le gardien du château. Tu pourras ensuite inviter la personne que tu aimes.' : reg ? 'On t\'a invitée à entrer. Crée ton compte, il te servira à revenir quand tu veux.' : 'Connecte-toi pour entrer dans notre château.'}</p>
      <form>${reg ? '<input name="name" placeholder="Ton prénom" autocomplete="given-name" maxlength="40">' : ''}
      <input name="email" type="email" placeholder="Adresse e-mail" autocomplete="email" required>
      <input name="password" type="password" placeholder="Mot de passe${reg ? ' (8 caractères minimum)' : ''}" autocomplete="${reg ? 'new-password' : 'current-password'}" required minlength="${reg ? 8 : 1}">
      <button type="submit">${reg ? 'Créer mon compte' : 'Entrer'}</button><div class="err"></div></form>
      ${first ? '' : reg ? '<div class="alt">Déjà un compte ? <a data-m="login">Se connecter</a></div>' : invite ? '<div class="alt">Pas encore de compte ? <a data-m="register">Créer mon compte</a></div>' : ''}</div>`;
    el.querySelectorAll('a[data-m]').forEach((a) => a.addEventListener('click', () => door(a.dataset.m, st)));
    const f = el.querySelector('form'), btn = f.querySelector('button'), out = f.querySelector('.err');
    f.addEventListener('submit', async (e) => {
      e.preventDefault(); btn.disabled = true; out.textContent = '';
      const v = Object.fromEntries(new FormData(f));
      try { const r = await api(reg ? 'register' : 'login', { json: Object.assign(v, reg ? { invite } : {}) }); if (invite) history.replaceState(null, '', '/'); start(r.user); }
      catch (err) { out.textContent = err.message; btn.disabled = false; }
    });
  }
  async function start(u) {
    ME = u; const el = document.getElementById('door'); if (el) el.remove();
    ready = sync().then(() => { loop(); });
    await ready; roomTick();
    window.claude = { use: async (nm) => ({ db, assets, user, room, downloads }[nm] || null) };
    resolveReady(window.claude);
  }
  async function boot() {
    try { const { user: u } = await api('me'); start(u); }
    catch (e) {
      let st = { hasOwner: true }; try { st = await api('status'); } catch (e2) {}
      door(!st.hasOwner || inviteOf(st) ? 'register' : 'login', st);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
