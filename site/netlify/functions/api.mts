/* Notre Château — le serveur du musée sur Netlify.
   Comptes (deux personnes), base partagée (souvenirs, lettres…), photos et sons, présence en direct.
   Tout est privé : chaque requête doit venir d'une personne connectée. */
import type { Config, Context } from "@netlify/functions";
import { getStore } from "@netlify/blobs";
import { scryptSync, randomBytes, createHash } from "node:crypto";
import { sign, verify } from "../lib/session.mts";
import webpush from "web-push";

const store = (name: string) => getStore({ name, consistency: "strong" });
const auth = () => store("chateau-auth");
const docs = () => store("chateau-docs");
const files = () => store("chateau-files");
const live = () => store("chateau-live");

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "x-robots-tag": "noindex, nofollow, noarchive, nosnippet, noimageindex", ...headers } });
const err = (code: string, status = 400, message = "") => json({ code, message: message || code }, status);

/* ── sessions signées ── */
const COOKIE = "chateau_sid";
/* le compte du créateur garde l'identifiant qu'il avait dans la page Claude, pour que ses souvenirs restent signés de lui */
const OWNER_ID = "u_8efd3NXwrMxqfCwEZxkpiQ";
/* L'invitation imprimée dans le QR code offert avec les fleurs : valable une seule fois, pour le deuxième compte.
   Seule son empreinte est ici (le code lui-même n'existe que sur le papier)
   (empreinte salée : elle ne permet ni de retrouver le code, ni de déchiffrer quoi que ce soit) */
const GIFT_HASH = "dd294e9fbba22663ffed92d498f5a6215ba47ff3a320b90d91980f7dedc0574b";
const isGift = (code: unknown) => typeof code === "string" && createHash("sha256").update("chateau-cadeau|" + code).digest("hex") === GIFT_HASH;
const PART = 4 * 1024 * 1024;
function sessionCookie(token: string, maxAgeS: number) {
  return `${COOKIE}=${token}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${maxAgeS}`;
}
function readCookie(req: Request): string | undefined {
  const c = req.headers.get("cookie") || "";
  const m = c.match(new RegExp("(?:^|;\\s*)" + COOKIE + "=([^;]+)"));
  return m ? m[1] : undefined;
}

/* ── comptes ── */
type User = { id: string; email: string; name: string; isOwner: boolean; salt: string; hash: string; createdAt: number };
const hashPw = (pw: string, salt: string) => scryptSync(pw, salt, 64).toString("hex");
const newId = (p = "") => p + randomBytes(12).toString("base64url").replace(/[-_]/g, "x").slice(0, 16);
async function userByEmail(email: string): Promise<User | null> {
  return (await auth().get("user/" + email.toLowerCase().trim(), { type: "json" })) as User | null;
}
async function allUsers(): Promise<User[]> {
  const { blobs } = await auth().list({ prefix: "user/" });
  const out = await Promise.all(blobs.map((b) => auth().get(b.key, { type: "json" })));
  return out.filter(Boolean) as User[];
}
async function currentUser(req: Request): Promise<User | null> {
  const s = await verify(readCookie(req)); if (!s) return null;
  return userByEmail(s.email);
}
const publicUser = (u: User) => ({ id: u.id, name: u.name, isOwner: u.isOwner, email: u.email });

/* ── base de documents : clé = chemin du document ── */
const okPath = (p: string) => /^[A-Za-z0-9_\-.~:@+]{1,200}(\/[A-Za-z0-9_\-.~:@+]{1,200}){1,14}$/.test(p) && p.split("/").length % 2 === 0;
const isPrivatePath = (p: string) => p.startsWith("data/users/");
const canTouch = (p: string, u: User) => !isPrivatePath(p) || p.split("/")[2] === u.id;
const hourKey = (t: number) => new Date(t).toISOString().slice(0, 13).replace(/[-T:]/g, "");
async function logChange(path: string, t: number) {
  await docs().setJSON(`log/${hourKey(t)}/${String(t).padStart(15, "0")}-${randomBytes(3).toString("hex")}`, { path, t });
}
async function readDoc(path: string) { return (await docs().get("d/" + path, { type: "json" })) as { data: any; t: number } | null; }

/* ── notifications sur le téléphone (Web Push) ──
   Les clés de l'app sont créées une seule fois et gardées côté serveur. */
const pushStore = () => store("chateau-push");
async function vapid(): Promise<{ publicKey: string; privateKey: string }> {
  let v = (await auth().get("meta/vapid", { type: "json" })) as any;
  if (!v) { await auth().setJSON("meta/vapid", webpush.generateVAPIDKeys(), { onlyIfNew: true }); v = await auth().get("meta/vapid", { type: "json" }); }
  return v;
}
const subKey = (uid: string, endpoint: string) => `sub/${uid}/${createHash("sha256").update(endpoint).digest("hex").slice(0, 32)}`;
async function pushTo(uids: string[], payload: Record<string, unknown>) {
  if (!uids.length) return 0;
  const v = await vapid(); webpush.setVapidDetails("https://hadiatoukaba.fr", v.publicKey, v.privateKey);
  let sent = 0;
  for (const uid of uids) {
    const { blobs } = await pushStore().list({ prefix: `sub/${uid}/` });
    for (const b of blobs) {
      const rec = (await pushStore().get(b.key, { type: "json" })) as any; if (!rec || !rec.sub) continue;
      try { await webpush.sendNotification(rec.sub, JSON.stringify(payload), { TTL: 60 * 60 * 24, urgency: "high" }); sent++; }
      catch (e: any) { if (e && (e.statusCode === 404 || e.statusCode === 410)) await pushStore().delete(b.key); else console.warn("push", e && e.statusCode, e && e.body); }
    }
  }
  return sent;
}
/* prévenir l'autre ; une seule fois par moment (pas 200 notifications pour 200 photos) */
async function notifyOthers(me: User, kind: string, body: string, quietMs: number) {
  const tk = `thr/${me.id}/${kind}`, now = Date.now();
  const last = (await pushStore().get(tk, { type: "json" })) as any;
  if (last && now - last.t < quietMs) return;
  await pushStore().setJSON(tk, { t: now });
  const others = (await allUsers()).filter((u) => u.id !== me.id).map((u) => u.id);
  await pushTo(others, { title: "Notre château", body, tag: kind, url: "/" });
}
const firstName = (u: User) => (u.name || "").split(/\s+/)[0] || "Ton amour";
const NOTIF: Record<string, [string, number]> = {
  memories: ["{n} a accroché de nouveaux souvenirs dans le château 🖼️", 20 * 60e3],
  letters: ["{n} t'a écrit une lettre 💌", 2 * 60e3],
  notes: ["{n} t'a laissé un mot du jour 📝", 5 * 60e3],
  reasons: ["{n} a ajouté une raison de t'aimer ♥", 10 * 60e3],
  wishes: ["{n} a fait un vœu dans le jardin ✨", 10 * 60e3],
  capsules: ["{n} a enterré une capsule temporelle ⏳", 10 * 60e3],
  firsts: ["{n} a noté une nouvelle première fois 🌱", 10 * 60e3],
  treasures: ["{n} a déposé un trésor 💎", 10 * 60e3],
  projects: ["{n} a ajouté un projet pour vous deux 🗺️", 10 * 60e3],
  library: ["{n} a rangé quelque chose dans la bibliothèque 📚", 10 * 60e3],
  sounds: ["{n} a ajouté un son au phonographe 🎼", 10 * 60e3],
};

/* ── la fonction ── */
export default async (req: Request, context: Context) => {
  const url = new URL(req.url), route = url.pathname.replace(/^\/api\/?/, ""), m = req.method;
  try {
    /* comptes */
    if (route === "status" && m === "GET") {
      const users = await allUsers();
      const imp = await auth().get("meta/imported", { type: "json" });
      return json({ hasOwner: users.some((u) => u.isOwner), count: users.length, imported: !!imp, giftOpen: users.length < 2 });
    }
    if (route === "register" && m === "POST") {
      const { email, password, name, invite } = await req.json();
      if (!email || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return err("bad_email", 400, "Adresse e-mail invalide");
      if (!password || String(password).length < 8) return err("weak_password", 400, "Le mot de passe doit faire au moins 8 caractères");
      if (await userByEmail(email)) return err("exists", 409, "Un compte existe déjà avec cette adresse");
      const users = await allUsers(), first = !users.length;
      if (!first && isGift(invite)) {
        if (users.length >= 2) return err("invite_used", 403, "Ce cadeau a déjà été ouvert : connecte-toi avec ton compte");
      } else if (!first) {
        const inv = invite && (await auth().get("invite/" + invite, { type: "json" })) as any;
        if (!inv || inv.used) return err("invite_required", 403, "Il faut un lien d'invitation valide");
        await auth().setJSON("invite/" + invite, { ...inv, used: true, usedAt: Date.now() });
      }
      const salt = randomBytes(16).toString("hex");
      const id = first ? OWNER_ID : newId("u_");
      const u: User = { id, email: email.toLowerCase().trim(), name: String(name || "").slice(0, 40) || email.split("@")[0], isOwner: first, salt, hash: hashPw(String(password), salt), createdAt: Date.now() };
      await auth().setJSON("user/" + u.email, u, { onlyIfNew: true });
      const ttl = 60 * 60 * 24 * 90;
      return json({ user: publicUser(u) }, 200, { "set-cookie": sessionCookie(await sign({ email: u.email, exp: Date.now() + ttl * 1000 }), ttl) });
    }
    if (route === "login" && m === "POST") {
      const { email, password } = await req.json();
      /* contre ceux qui essaieraient de deviner un mot de passe : après 5 erreurs, la porte se ferme un moment */
      const fk = "fail/" + createHash("sha256").update(String(email || "").toLowerCase().trim()).digest("hex").slice(0, 32), now = Date.now();
      const fail = ((await auth().get(fk, { type: "json" })) as any) || { n: 0, until: 0 };
      if (fail.until > now) return err("too_many", 429, `Trop d'essais. Réessaie dans ${Math.ceil((fail.until - now) / 60000)} min.`);
      const u = email && (await userByEmail(email));
      if (!u || hashPw(String(password || ""), u.salt) !== u.hash) {
        const n = fail.n + 1; await auth().setJSON(fk, { n, until: n >= 5 ? now + Math.min(24 * 3600e3, 15 * 60e3 * 2 ** (n - 5)) : 0 });
        return err("bad_login", 401, n >= 5 ? "Trop d'essais : la porte se ferme 15 minutes" : "E-mail ou mot de passe incorrect");
      }
      if (fail.n) await auth().delete(fk);
      const ttl = 60 * 60 * 24 * 90;
      return json({ user: publicUser(u) }, 200, { "set-cookie": sessionCookie(await sign({ email: u.email, exp: Date.now() + ttl * 1000 }), ttl) });
    }
    if (route === "logout") return json({ ok: true }, 200, { "set-cookie": sessionCookie("", 0) });

    /* journal des scans du QR code : la page d'attente y inscrit chaque ouverture du lien */
    if (route === "scan" && m === "POST") {
      const b = (await req.json().catch(() => ({}))) as any, clip = (v: unknown) => String(v || "").slice(0, 60);
      const t = Date.now();
      await store("chateau-scans").setJSON(`scan/${t}-${Math.random().toString(36).slice(2, 7)}`, { t, appareil: clip(b.appareil), ville: clip(b.ville), pays: clip(b.pays), moment: clip(b.moment) });
      return json({ ok: true });
    }

    /* tout le reste demande une connexion */
    const me = await currentUser(req);
    if (route === "journal" && m === "GET") {
      if (!me || !me.isOwner) return new Response(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><body style="font-family:system-ui;background:#140c1c;color:#f6efe2;padding:24px"><h2>Journal des scans</h2><p>Connecte-toi d'abord avec ton compte de gardien sur <a style="color:#f3d79a" href="/?apercu=1">hadiatoukaba.fr/?apercu=1</a>, puis reviens sur cette page.</p>`, { status: 401, headers: { "content-type": "text/html; charset=utf-8", "x-robots-tag": "noindex, nofollow, noarchive, nosnippet, noimageindex" } });
      const st = store("chateau-scans"), { blobs } = await st.list({ prefix: "scan/" });
      const rows = (await Promise.all(blobs.map((b) => st.get(b.key, { type: "json" })))).filter(Boolean).sort((a: any, b: any) => b.t - a.t) as any[];
      const fmt = (t: number) => new Date(t).toLocaleString("fr-FR", { timeZone: "Europe/Paris", weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
      const esc = (v: string) => v.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" } as any)[c]);
      const lignes = rows.map((r) => `<tr><td>${fmt(r.t)}</td><td>${esc(r.appareil)}</td><td>${esc([r.ville, r.pays].filter(Boolean).join(", ") || "—")}</td><td>${esc(r.moment)}</td></tr>`).join("");
      return new Response(`<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>Journal des scans</title>
<style>body{margin:0;font-family:-apple-system,system-ui,sans-serif;background:#140c1c;color:#f6efe2;padding:22px 16px}h1{font-family:Georgia,serif;font-style:italic;font-weight:500;font-size:28px;margin:0 0 4px}p{color:rgba(246,239,226,.65);margin:0 0 18px;font-size:14px}
table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:#d6ad62;font-weight:500;font-size:11px;letter-spacing:.12em;text-transform:uppercase;padding:8px 6px;border-bottom:1px solid rgba(214,173,98,.4)}td{padding:10px 6px;border-bottom:1px solid rgba(255,255,255,.08);vertical-align:top}td:first-child{white-space:nowrap;color:#f3d79a}
.vide{padding:30px 0;text-align:center;color:rgba(246,239,226,.5)}</style></head><body>
<h1>Journal des scans</h1><p>${rows.length} ouverture${rows.length > 1 ? "s" : ""} du lien du QR code · heure de Paris · la page se met à jour toute seule</p>
<table><thead><tr><th>Quand</th><th>Appareil</th><th>Où</th><th>Page</th></tr></thead><tbody>${lignes || `<tr><td colspan="4" class="vide">Personne n'a encore scanné le QR code.</td></tr>`}</tbody></table>
<script>setTimeout(()=>location.reload(),30000)</script></body></html>`, { headers: { "content-type": "text/html; charset=utf-8", "x-robots-tag": "noindex, nofollow, noarchive, nosnippet, noimageindex", "cache-control": "no-store" } });
    }
    if (!me) return err("unauthenticated", 401, "Connexion requise");

    if (route === "me") return json({ user: publicUser(me) });
    if (route === "profiles" && m === "POST") {
      const { ids } = await req.json(); const users = await allUsers(); const out: Record<string, any> = {};
      for (const id of ids || []) { const u = users.find((x) => x.id === id); if (u) out[id] = { id, name: u.name }; }
      return json(out);
    }
    if (route === "invite" && m === "POST") {
      if (!me.isOwner) return err("not_permitted", 403, "Seul le créateur du château peut inviter");
      const code = randomBytes(12).toString("base64url");
      await auth().setJSON("invite/" + code, { by: me.id, createdAt: Date.now(), used: false });
      return json({ code });
    }
    if (route === "password" && m === "POST") {
      const { current, next } = await req.json();
      const pk = "failpw/" + me.id, pf = ((await auth().get(pk, { type: "json" })) as any) || { n: 0, until: 0 };
      if (pf.until > Date.now()) return err("too_many", 429, "Trop d'essais. Réessaie plus tard.");
      if (hashPw(String(current || ""), me.salt) !== me.hash) { const n = pf.n + 1; await auth().setJSON(pk, { n, until: n >= 5 ? Date.now() + 30 * 60e3 : 0 }); return err("bad_login", 401, "Mot de passe actuel incorrect"); }
      if (pf.n) await auth().delete(pk);
      if (!next || String(next).length < 8) return err("weak_password", 400, "Le mot de passe doit faire au moins 8 caractères");
      const salt = randomBytes(16).toString("hex");
      await auth().setJSON("user/" + me.email, { ...me, salt, hash: hashPw(String(next), salt) });
      return json({ ok: true });
    }

    /* notifications */
    if (route === "push/key" && m === "GET") return json({ key: (await vapid()).publicKey });
    if (route === "push/subscribe" && m === "POST") {
      const { sub } = await req.json();
      if (!sub || typeof sub.endpoint !== "string" || !/^https:\/\//.test(sub.endpoint) || !sub.keys) return err("bad_request", 400, "Abonnement invalide");
      await pushStore().setJSON(subKey(me.id, sub.endpoint), { sub, ua: (req.headers.get("user-agent") || "").slice(0, 160), at: Date.now() });
      return json({ ok: true });
    }
    if (route === "push/unsubscribe" && m === "POST") { const { endpoint } = await req.json(); if (endpoint) await pushStore().delete(subKey(me.id, String(endpoint))); return json({ ok: true }); }
    if (route === "push/test" && m === "POST") { const n = await pushTo([me.id], { title: "Notre château", body: "Les notifications sont activées ✨ Tu seras prévenu·e quand quelque chose de nouveau arrive.", tag: "test", url: "/" }); return json({ sent: n }); }

    /* documents */
    if (route === "doc") {
      const path = url.searchParams.get("path") || "";
      if (!okPath(path)) return err("bad_request", 400, "Chemin invalide");
      if (!canTouch(path, me)) return err("not_permitted", 403);
      if (m === "GET") { const d = await readDoc(path); return json({ exists: !!d && !d.deleted, data: d && !d.deleted ? d.data : null }); }
      if (m === "PUT" || m === "PATCH") {
        const body = await req.json(); const t = Date.now();
        let data = body.data;
        let isNew = false;
        if (m === "PATCH") { const cur = await readDoc(path); if (!cur || (cur as any).deleted) return err("not_found", 404, "Document introuvable"); data = { ...cur.data, ...data }; }
        else { const segs = path.split("/"), col = segs[segs.length - 2]; if (NOTIF[col] && !isPrivatePath(path)) { const cur = await readDoc(path); isNew = !cur || !!(cur as any).deleted; } }
        const raw = JSON.stringify({ data, t });
        if (raw.length > 900_000) return err("too_large", 413, "Document trop volumineux");
        await docs().set("d/" + path, raw); await logChange(path, t);
        if (isNew) { const segs = path.split("/"), [txt, quiet] = NOTIF[segs[segs.length - 2]]; const p = notifyOthers(me, segs[segs.length - 2], txt.replace("{n}", firstName(me)), quiet).catch((e) => console.warn("notif", e)); if ((context as any).waitUntil) (context as any).waitUntil(p); else await p; }
        return json({ ok: true, t });
      }
      if (m === "DELETE") { const t = Date.now(); await docs().setJSON("d/" + path, { deleted: true, t }); await logChange(path, t); return json({ ok: true }); }
    }
    /* synchronisation : tout, ou seulement ce qui a changé depuis `since` */
    if (route === "sync" && m === "GET") {
      const since = Number(url.searchParams.get("since") || 0), now = Date.now();
      let paths: string[];
      if (!since || now - since > 36 * 3600e3) {
        const { blobs } = await docs().list({ prefix: "d/" }); paths = blobs.map((b) => b.key.slice(2));
      } else {
        const set = new Set<string>();
        for (let t = since - 3600e3; t <= now + 3600e3; t += 3600e3) {
          const { blobs } = await docs().list({ prefix: `log/${hourKey(t)}/` });
          for (const b of blobs) { const ts = Number(b.key.split("/")[2].split("-")[0]); if (ts > since - 2000) { const e = (await docs().get(b.key, { type: "json" })) as any; if (e) set.add(e.path); } }
        }
        paths = [...set];
      }
      paths = paths.filter((p) => canTouch(p, me));
      const out: any[] = [];
      for (let i = 0; i < paths.length; i += 24) {
        const part = await Promise.all(paths.slice(i, i + 24).map(async (p) => { const d = await readDoc(p); return d ? { path: p, data: (d as any).deleted ? null : d.data, t: d.t } : null; }));
        out.push(...part.filter(Boolean));
      }
      return json({ now, full: !since || now - since > 36 * 3600e3, changes: out });
    }

    /* fichiers (photos, sons, vidéos) : envoi par morceaux de 4 Mo */
    if (route === "asset/start" && m === "POST") {
      const { type, size } = await req.json();
      if (size > 500 * 1024 * 1024) return err("too_large", 413, "Fichier trop lourd (500 Mo max)");
      const id = newId("f"); await files().setJSON("meta/" + id, { type: type || "application/octet-stream", size, partSize: PART, by: me.id, parts: 0, done: false, createdAt: Date.now() });
      return json({ id });
    }
    if (route === "asset/part" && m === "PUT") {
      const id = url.searchParams.get("id") || "", n = Number(url.searchParams.get("n") || 0);
      const meta = (await files().get("meta/" + id, { type: "json" })) as any; if (!meta || meta.by !== me.id || meta.done) return err("not_found", 404);
      if (n < 0 || n > 130) return err("bad_request", 400);
      await files().set(`part/${id}/${String(n).padStart(4, "0")}`, await req.arrayBuffer());
      return json({ ok: true });
    }
    if (route === "asset/finish" && m === "POST") {
      const { id, parts } = await req.json();
      const meta = (await files().get("meta/" + id, { type: "json" })) as any; if (!meta || meta.by !== me.id) return err("not_found", 404);
      await files().setJSON("meta/" + id, { ...meta, parts, done: true });
      return json({ id, url: "/_blob/" + id, sizeBytes: meta.size, contentType: meta.type });
    }
    if (route === "asset" && m === "DELETE") {
      const id = url.searchParams.get("id") || ""; const meta = (await files().get("meta/" + id, { type: "json" })) as any; if (!meta) return json({ ok: true });
      for (let i = 0; i < (meta.parts || 0); i++) await files().delete(`part/${id}/${String(i).padStart(4, "0")}`);
      await files().delete("meta/" + id); return json({ ok: true });
    }

    /* présence en direct : chacun dépose sa position, reçoit celle de l'autre et les cœurs envoyés */
    if (route === "room" && m === "POST") {
      const { presence, emit, since } = await req.json(); const now = Date.now();
      if (presence) {
        const prev = (await live().get("p/" + me.id, { type: "json" })) as any;
        await live().setJSON("p/" + me.id, { presence, at: now, by: me.id });
        if (!prev || now - prev.at > 30 * 60e3) { const p = notifyOthers(me, "presence", `${firstName(me)} vient d'entrer dans le château 🏰`, 30 * 60e3).catch((e) => console.warn("notif", e)); if ((context as any).waitUntil) (context as any).waitUntil(p); else await p; }
      }
      if (emit && emit.topic) await live().setJSON(`e/${String(now).padStart(15, "0")}-${randomBytes(3).toString("hex")}`, { topic: String(emit.topic).slice(0, 30), data: emit.data || {}, by: me.id, at: now });
      const { blobs } = await live().list({ prefix: "p/" });
      const peers = (await Promise.all(blobs.map((b) => live().get(b.key, { type: "json" })))).filter((p: any) => p && now - p.at < 15000);
      const ev = await live().list({ prefix: "e/" }); const events: any[] = [];
      for (const b of ev.blobs) { const ts = Number(b.key.slice(2).split("-")[0]); if (now - ts > 120000) { await live().delete(b.key); continue; } if (ts > (since || now - 5000)) { const e = await live().get(b.key, { type: "json" }); if (e) events.push(e); } }
      return json({ now, me: me.id, peers, events });
    }

    return err("not_found", 404, "Route inconnue");
  } catch (e: any) {
    console.error(e);
    return err("server_error", 500, (e && e.message) || "Erreur du serveur");
  }
};

export const config: Config = { path: "/api/*" };
