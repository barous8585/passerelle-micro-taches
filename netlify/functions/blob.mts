/* Sert les photos, sons et vidéos du musée — uniquement aux personnes connectées.
   Gère les requêtes partielles (Range), indispensables pour lire les vidéos sur iPhone. */
import type { Config, Context } from "@netlify/functions";
import { getStore } from "@netlify/blobs";
import { readSession } from "../lib/session.mts";

const files = () => getStore({ name: "chateau-files", consistency: "strong" });

export default async (req: Request, context: Context) => {
  if (!(await readSession(req))) return new Response("Connexion requise", { status: 401 });
  const id = context.params.id;
  const meta = (await files().get("meta/" + id, { type: "json" })) as any;
  if (!meta || !meta.done) return new Response("Introuvable", { status: 404 });
  const size: number = meta.size, ps: number = meta.partSize || size, n: number = meta.parts || 1;
  let start = 0, end = size - 1, status = 200;
  const range = req.headers.get("range");
  if (range) {
    const mm = range.match(/bytes=(\d*)-(\d*)/);
    if (mm) {
      if (mm[1] === "" && mm[2] !== "") { start = Math.max(0, size - Number(mm[2])); }
      else { start = Number(mm[1] || 0); if (mm[2] !== "") end = Math.min(size - 1, Number(mm[2])); }
      if (start > end || start >= size) return new Response(null, { status: 416, headers: { "content-range": `bytes */${size}` } });
      // jamais plus de 6 Mo par réponse partielle
      end = Math.min(end, start + 6 * 1024 * 1024 - 1);
      status = 206;
    }
  }
  let i = Math.floor(start / ps);
  const last = Math.min(n - 1, Math.floor(end / ps));
  const stream = new ReadableStream({
    async pull(ctrl) {
      if (i > last) { ctrl.close(); return; }
      const buf = (await files().get(`part/${id}/${String(i).padStart(4, "0")}`, { type: "arrayBuffer" })) as ArrayBuffer | null;
      if (buf) {
        const base = i * ps, a = Math.max(0, start - base), b = Math.min(buf.byteLength, end - base + 1);
        ctrl.enqueue(new Uint8Array(buf).subarray(a, b));
      }
      i++;
    },
  });
  const headers: Record<string, string> = {
    "content-type": meta.type, "accept-ranges": "bytes", "content-length": String(end - start + 1),
    "cache-control": "private, max-age=31536000, immutable",
  };
  if (status === 206) headers["content-range"] = `bytes ${start}-${end}/${size}`;
  return new Response(stream, { status, headers });
};

export const config: Config = { path: "/_blob/:id" };
