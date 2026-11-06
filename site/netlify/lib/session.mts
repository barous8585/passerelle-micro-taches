/* Clé secrète des sessions : variable SESSION_SECRET si elle existe, sinon une clé tirée au hasard
   une seule fois et gardée dans le stockage privé du site (aucun réglage à faire dans Netlify). */
import { getStore } from "@netlify/blobs";
import { randomBytes, createHmac, timingSafeEqual } from "node:crypto";

let cached: string | null = null;
export async function getSecret(): Promise<string> {
  if (cached) return cached;
  const env = Netlify.env.get("SESSION_SECRET");
  if (env && env.length >= 24) return (cached = env);
  const st = getStore({ name: "chateau-auth", consistency: "strong" });
  let s = await st.get("meta/secret");
  if (!s) { await st.set("meta/secret", randomBytes(36).toString("base64url"), { onlyIfNew: true }); s = await st.get("meta/secret"); }
  return (cached = s as string);
}
export async function sign(payload: object): Promise<string> {
  const p = Buffer.from(JSON.stringify(payload)).toString("base64url");
  return p + "." + createHmac("sha256", await getSecret()).update(p).digest("base64url");
}
export async function verify(token: string | undefined): Promise<any | null> {
  if (!token || !token.includes(".")) return null;
  const [p, sig] = token.split(".");
  const good = createHmac("sha256", await getSecret()).update(p).digest("base64url");
  const a = Buffer.from(sig), b = Buffer.from(good);
  if (a.length !== b.length || !timingSafeEqual(a, b)) return null;
  try { const d = JSON.parse(Buffer.from(p, "base64url").toString()); return d.exp > Date.now() ? d : null; } catch { return null; }
}
export function readSession(req: Request): Promise<any | null> {
  const c = req.headers.get("cookie") || "", m = c.match(/(?:^|;\s*)chateau_sid=([^;]+)/);
  return verify(m ? m[1] : undefined);
}
