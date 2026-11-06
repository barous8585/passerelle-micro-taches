/* Une seule fois : recopie dans Netlify les souvenirs qui étaient dans la page Claude.
   Le fichier d'import est d'abord envoyé comme un fichier normal (par morceaux), puis lu ici. Aucune donnée privée n'est dans le code. */
import type { Config, Context } from "@netlify/functions";
import { getStore } from "@netlify/blobs";
import { randomBytes } from "node:crypto";
import { readSession } from "../../lib/session.mts";

const st = (name: string) => getStore({ name, consistency: "strong" });
const PART = 4 * 1024 * 1024;
const hourKey = (t: number) => new Date(t).toISOString().slice(0, 13).replace(/[-T:]/g, "");

export default async (req: Request, context: Context) => {
  if (req.method !== "POST") return new Response("POST", { status: 405 });
  const s = await readSession(req); if (!s) return Response.json({ code: "unauthenticated" }, { status: 401 });
  const auth = st("chateau-auth"), docs = st("chateau-docs"), files = st("chateau-files");
  const u = (await auth.get("user/" + s.email, { type: "json" })) as any;
  if (!u || !u.isOwner) return Response.json({ code: "not_permitted" }, { status: 403 });
  if (await auth.get("meta/imported", { type: "json" })) return Response.json({ ok: true, already: true });
  const { asset } = await req.json().catch(() => ({}));
  const meta = asset && ((await files.get("meta/" + asset, { type: "json" })) as any);
  if (!meta || !meta.done || meta.by !== u.id) return Response.json({ code: "not_found", message: "Fichier d'import introuvable" }, { status: 404 });
  const chunks: Buffer[] = [];
  for (let i = 0; i < meta.parts; i++) { const b = (await files.get(`part/${asset}/${String(i).padStart(4, "0")}`, { type: "arrayBuffer" })) as ArrayBuffer; chunks.push(Buffer.from(b)); }
  let bundle: any; try { bundle = JSON.parse(Buffer.concat(chunks).toString("utf8")); } catch { return Response.json({ code: "bad_file", message: "Ce fichier n'est pas un import du château" }, { status: 400 }); }
  if (!bundle || bundle.format !== "notre-chateau-import") return Response.json({ code: "bad_file", message: "Ce fichier n'est pas un import du château" }, { status: 400 });
  const DOCS = bundle.docs || [], FILES = bundle.files || [];
  const t = Date.now();
  for (const d of DOCS as any[]) {
    // si le document existe déjà (réglages faits juste avant), on garde ce qui a été rempli
    const cur = (await docs.get("d/" + d.path, { type: "json" })) as any;
    let data = d.data;
    if (cur && !cur.deleted && cur.data) { data = { ...d.data }; for (const [k, v] of Object.entries(cur.data)) if (v !== "" && v !== null && v !== undefined) (data as any)[k] = v; }
    await docs.set("d/" + d.path, JSON.stringify({ data, t }));
    await docs.setJSON(`log/${hourKey(t)}/${String(t).padStart(15, "0")}-${randomBytes(3).toString("hex")}`, { path: d.path, t });
  }
  for (const f of FILES as any[]) {
    const buf = Buffer.from(f.b64, "base64"), n = Math.max(1, Math.ceil(buf.length / PART));
    for (let i = 0; i < n; i++) await files.set(`part/${f.id}/${String(i).padStart(4, "0")}`, buf.subarray(i * PART, (i + 1) * PART));
    await files.setJSON("meta/" + f.id, { type: f.type, size: buf.length, partSize: PART, parts: n, by: u.id, done: true, createdAt: t });
  }
  for (let i = 0; i < meta.parts; i++) await files.delete(`part/${asset}/${String(i).padStart(4, "0")}`);
  await files.delete("meta/" + asset);
  await auth.setJSON("meta/imported", { at: t, docs: (DOCS as any[]).length, files: (FILES as any[]).length });
  return Response.json({ ok: true, docs: (DOCS as any[]).length, files: (FILES as any[]).length });
};

export const config: Config = { path: "/import-chateau" };
