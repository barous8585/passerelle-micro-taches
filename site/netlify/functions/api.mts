/* Notre Château — le serveur du musée sur Netlify.
   Comptes (deux personnes), base partagée (souvenirs, lettres…), photos et sons, présence en direct.
   Tout est privé : chaque requête doit venir d'une personne connectée. */
import type { Config, Context } from "@netlify/functions";
import { getStore } from "@netlify/blobs";
import { scryptSync, randomBytes, createHash, createDecipheriv } from "node:crypto";
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

/* la lettre d'accueil : chiffrée ici (le code est public), sa clé n'existe que dans les réglages secrets du site */
const LETTRE = "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";
async function lettre(): Promise<string> {
  const k = Netlify.env.get("LETTRE_KEY") || ((await auth().get("meta/lettre_key")) as string | null); if (!k) return "";
  try { const b = Buffer.from(LETTRE, "base64"), iv = b.subarray(0, 12), tag = b.subarray(b.length - 16), ct = b.subarray(12, b.length - 16);
    const d = createDecipheriv("aes-256-gcm", Buffer.from(k, "base64"), iv); d.setAuthTag(tag); return Buffer.concat([d.update(ct), d.final()]).toString("utf8"); } catch { return ""; }
}

/* la vidéo de l'entrée : chiffrée avec la même clé que la lettre, déchiffrée ici pour les personnes connectées seulement */
/* ses ouvertures d'avant le journal automatique (chiffrées, comme la lettre) */
const JOURNAL_PASSE_ENC = "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";
async function journalPasse(): Promise<any[]> {
  const k = Netlify.env.get("LETTRE_KEY") || ((await auth().get("meta/lettre_key")) as string | null); if (!k) return [];
  try { const b = Buffer.from(JOURNAL_PASSE_ENC, "base64"); const d = createDecipheriv("aes-256-gcm", Buffer.from(k, "base64"), b.subarray(0, 12)); d.setAuthTag(b.subarray(b.length - 16));
    return JSON.parse(Buffer.concat([d.update(b.subarray(12, b.length - 16)), d.final()]).toString("utf8")); } catch { return []; }
}
/* l'énigme du château (chiffrée, comme la lettre) */
const ENIGME_ENC = "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";
async function enigme(): Promise<any | null> {
  const k = Netlify.env.get("LETTRE_KEY") || ((await auth().get("meta/lettre_key")) as string | null); if (!k) return null;
  try { const b = Buffer.from(ENIGME_ENC, "base64"); const d = createDecipheriv("aes-256-gcm", Buffer.from(k, "base64"), b.subarray(0, 12)); d.setAuthTag(b.subarray(b.length - 16));
    return JSON.parse(Buffer.concat([d.update(b.subarray(12, b.length - 16)), d.final()]).toString("utf8")); } catch { return null; }
}
const normAns = (x: string) => String(x || "").toLowerCase().replace(/œ/g, "oe").normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
const MEDIA_FILES: Record<string, string> = { intro: "video/mp4", son1: "audio/mp4", son2: "audio/mp4", son3: "audio/mp4", son4: "audio/mp4" };
const MEDIA_CACHE: Record<string, Buffer> = {};
async function privateMedia(origin: string, name: string): Promise<Buffer | null> {
  if (MEDIA_CACHE[name]) return MEDIA_CACHE[name];
  const k = Netlify.env.get("LETTRE_KEY") || ((await auth().get("meta/lettre_key")) as string | null); if (!k) return null;
  try { const r = await fetch(origin + "/" + name + ".enc"); if (!r.ok) return null; const b = Buffer.from(await r.arrayBuffer());
    const d = createDecipheriv("aes-256-gcm", Buffer.from(k, "base64"), b.subarray(0, 12)); d.setAuthTag(b.subarray(b.length - 16));
    return (MEDIA_CACHE[name] = Buffer.concat([d.update(b.subarray(12, b.length - 16)), d.final()])); } catch { return null; }
}

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
    /* ───── l'énigme : visible pour elle seulement quand le gardien l'a allumée ───── */
    if (route === "enigme" && m === "GET") {
      if (!me) return err("auth", 401);
      const E = await enigme(); if (!E) return json({ active: false });
      const on = (await auth().get("meta/enigme_on")) === E.id, total = E.steps.length;
      const from = me.isOwner ? 0 : Number((await auth().get(`enigme/progress/${E.id}/${me.id}`)) || 0);
      const steps = (E.steps as any[]).map((st, i) => ({ i, type: st.type || "choice", target: st.target, q: st.q, hints: st.hints || [], choices: st.choices || [] })).slice(from);
      return json({ active: me.isOwner ? true : on && from < total, on, total, from, steps });
    }
    if (route === "enigme" && m === "POST") {
      if (!me) return err("auth", 401);
      const E = await enigme(); if (!E) return err("not_found", 404);
      const b = (await req.json().catch(() => ({}))) as any, i = Number(b.i), st = (E.steps as any[])[i]; if (!st) return err("bad_step", 400);
      const total = E.steps.length, pk = `enigme/progress/${E.id}/${me.id}`;
      if (!me.isOwner && Number((await auth().get(pk)) || 0) !== i) return err("bad_step", 409);
      const ok = st.type === "frame" ? !!b.found : (st.answers as string[]).some((x) => normAns(x) === normAns(b.answer));
      const last = i === total - 1;
      if (!me.isOwner) {
        const ak = `enigme/tries/${E.id}/${me.id}/${i}`, tries = Number((await auth().get(ak)) || 0) + 1; await auth().set(ak, String(tries));
        if (ok) {
          await auth().set(pk, String(i + 1));
          const ua = req.headers.get("user-agent") || "", g = (context as any)?.geo || {}, t = Date.now();
          const what = st.type === "frame" ? `cadre trouvé : « ${String(b.title || "").slice(0, 40)} »` : `« ${String(b.answer).slice(0, 40)} »`;
          await store("chateau-scans").setJSON(`scan/${t}-${Math.random().toString(36).slice(2, 7)}`, { t, appareil: /iPhone/.test(ua) ? "iPhone" : /Android/.test(ua) ? "téléphone Android" : "appareil", ville: String(g.city || "").slice(0, 60), pays: String((g.country && g.country.name) || "").slice(0, 60), moment: `${last ? "🏆" : "🗝️"} énigme ${i + 1}/${total} résolue (${what}, ${tries} essai${tries > 1 ? "s" : ""})${last ? " · toutes les énigmes résolues !" : ""}` }).catch(() => {});
        }
      }
      return json(ok ? { ok: true, last, reward: last ? E.reward : null } : { ok: false });
    }
    if (route === "enigme/etat" && m === "POST") {
      if (!me || !me.isOwner) return err("forbidden", 403);
      const E = await enigme(); if (!E) return err("not_found", 404);
      const b = (await req.json().catch(() => ({}))) as any;
      if (b.on) await auth().set("meta/enigme_on", E.id); else await auth().delete("meta/enigme_on");
      return json({ ok: true, on: !!b.on });
    }
    if (route === "enigme/notifier" && m === "POST") {
      if (!me || !me.isOwner) return err("forbidden", 403);
      const E = await enigme(); if (!E) return err("not_found", 404);
      await auth().set("meta/enigme_on", E.id);
      const others = (await allUsers()).filter((u) => !u.isOwner).map((u) => u.id);
      const sent = await pushTo(others, { title: "Notre château", body: "🗝️ Psst… le château a caché 5 énigmes rien que pour toi. Prête à jouer ? Envoie la capture qui montre que tu as gagné, et ton cadeau t’est envoyé 🎁", tag: "enigme", url: "/" });
      return json({ ok: true, sent });
    }
    /* elle entre dans le château (QR code, app installée ou site) : une ligne dans le journal */
    if (route === "entree" && m === "POST") {
      if (!me) return err("auth", 401);
      if (me.isOwner) return json({ ok: true, skipped: true });
      const b = (await req.json().catch(() => ({}))) as any, ua = req.headers.get("user-agent") || "";
      const appareil = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "téléphone Android" : /Macintosh/.test(ua) ? "Mac" : /Windows/.test(ua) ? "PC Windows" : "appareil";
      const via = ({ qr: "QR code", app: "app installée", web: "site web" } as Record<string, string>)[String(b.via)] || "site web";
      const g = (context as any)?.geo || {}, t = Date.now();
      await store("chateau-scans").setJSON(`scan/${t}-${Math.random().toString(36).slice(2, 7)}`, { t, appareil, ville: String(g.city || "").slice(0, 60), pays: String((g.country && g.country.name) || "").slice(0, 60), moment: "entrée dans le château · " + via });
      return json({ ok: true });
    }
    if (route === "journal" && m === "GET") {
      if (!me || !me.isOwner) return new Response(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><body style="font-family:system-ui;background:#140c1c;color:#f6efe2;padding:24px"><h2>Journal des scans</h2><p>Connecte-toi d'abord avec ton compte de gardien sur <a style="color:#f3d79a" href="/?apercu=1">hadiatoukaba.fr/?apercu=1</a>, puis reviens sur cette page.</p>`, { status: 401, headers: { "content-type": "text/html; charset=utf-8", "x-robots-tag": "noindex, nofollow, noarchive, nosnippet, noimageindex" } });
      const st = store("chateau-scans"), { blobs } = await st.list({ prefix: "scan/" });
      const rows = [...(await journalPasse()), ...(await Promise.all(blobs.map((b) => st.get(b.key, { type: "json" })))).filter(Boolean)].sort((a: any, b: any) => b.t - a.t) as any[];
      const fmt = (t: number) => new Date(t).toLocaleString("fr-FR", { timeZone: "Europe/Paris", weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
      const esc = (v: string) => v.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" } as any)[c]);
      const lignes = rows.map((r) => `<tr><td>${fmt(r.t)}</td><td>${esc(r.appareil)}</td><td>${esc([r.ville, r.pays].filter(Boolean).join(", ") || "—")}</td><td>${esc(r.moment)}</td></tr>`).join("");
      return new Response(`<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>Journal des ouvertures</title>
<style>body{margin:0;font-family:-apple-system,system-ui,sans-serif;background:#140c1c;color:#f6efe2;padding:22px 16px}h1{font-family:Georgia,serif;font-style:italic;font-weight:500;font-size:28px;margin:0 0 4px}p{color:rgba(246,239,226,.65);margin:0 0 18px;font-size:14px}
table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:#d6ad62;font-weight:500;font-size:11px;letter-spacing:.12em;text-transform:uppercase;padding:8px 6px;border-bottom:1px solid rgba(214,173,98,.4)}td{padding:10px 6px;border-bottom:1px solid rgba(255,255,255,.08);vertical-align:top}td:first-child{white-space:nowrap;color:#f3d79a}
.vide{padding:30px 0;text-align:center;color:rgba(246,239,226,.5)}</style></head><body>
<h1>Journal des ouvertures</h1><p>${rows.length} ouverture${rows.length > 1 ? "s" : ""} · QR code, app et site · heure de Paris · la page se met à jour toute seule</p>
<table><thead><tr><th>Quand</th><th>Appareil</th><th>Où</th><th>Page</th></tr></thead><tbody>${lignes || `<tr><td colspan="4" class="vide">Personne n'a encore scanné le QR code.</td></tr>`}</tbody></table>
<script>setTimeout(()=>location.reload(),30000)</script></body></html>`, { headers: { "content-type": "text/html; charset=utf-8", "x-robots-tag": "noindex, nofollow, noarchive, nosnippet, noimageindex", "cache-control": "no-store" } });
    }
    if (!me) return err("unauthenticated", 401, "Connexion requise");

    if (route === "me") return json({ user: publicUser(me) });
    if (route === "lettre" && m === "GET") return json({ text: await lettre() });
    const mr = route.match(/^(?:intro-video|media\/(intro|son[1-4]))$/);
    if (mr && m === "GET") {
      const name = mr[1] || "intro", v = await privateMedia(url.origin, name); if (!v) return err("not_found", 404);
      const size = v.length, range = req.headers.get("range"), base = { "content-type": MEDIA_FILES[name], "accept-ranges": "bytes", "cache-control": "private, max-age=86400", "x-robots-tag": "noindex" };
      const mm = range && range.match(/bytes=(\d*)-(\d*)/);
      if (mm) { let a = mm[1] === "" ? Math.max(0, size - Number(mm[2])) : Number(mm[1]), e = mm[1] !== "" && mm[2] !== "" ? Math.min(size - 1, Number(mm[2])) : size - 1;
        e = Math.min(e, a + 4 * 1024 * 1024 - 1);
        if (a >= size || a > e) return new Response(null, { status: 416, headers: { "content-range": `bytes */${size}` } });
        return new Response(v.subarray(a, e + 1), { status: 206, headers: { ...base, "content-range": `bytes ${a}-${e}/${size}`, "content-length": String(e - a + 1) } }); }
      return new Response(v, { status: 200, headers: { ...base, "content-length": String(size) } });
    }
    if (route === "lettre/key" && m === "POST") {
      if (!me.isOwner) return err("not_permitted", 403);
      const { key } = await req.json(); if (typeof key !== "string" || key.length < 40 || key.length > 60) return err("bad_request", 400);
      await auth().set("meta/lettre_key", key); const t = await lettre();
      if (!t) { await auth().delete("meta/lettre_key"); return err("bad_key", 400, "Clé incorrecte"); }
      return json({ ok: true, chars: t.length });
    }
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
    if (route === "push/test" && m === "POST") { if (me && !me.isOwner) { const ua = req.headers.get("user-agent") || "", g = (context as any)?.geo || {}, t = Date.now(); await store("chateau-scans").setJSON(`scan/${t}-${Math.random().toString(36).slice(2, 7)}`, { t, appareil: /iPhone/.test(ua) ? "iPhone" : /Android/.test(ua) ? "téléphone Android" : "appareil", ville: String(g.city || "").slice(0, 60), pays: String((g.country && g.country.name) || "").slice(0, 60), moment: "notifications du château activées" + (/iPhone|iPad/.test(ua) ? " · app installée" : "") }).catch(() => {}); } const n = await pushTo([me.id], { title: "Notre château", body: "Les notifications sont activées ✨ Tu seras prévenu·e quand quelque chose de nouveau arrive.", tag: "test", url: "/" }); return json({ sent: n }); }

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
        if (!me.isOwner && (!prev || now - prev.at > 30 * 60e3)) { const p = notifyOthers(me, "presence", `${firstName(me)} vient d'entrer dans le château 🏰`, 30 * 60e3).catch((e) => console.warn("notif", e)); if ((context as any).waitUntil) (context as any).waitUntil(p); else await p; }
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
