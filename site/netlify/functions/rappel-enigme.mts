/* Notre château : chaque heure, demande au château s'il faut rappeler les énigmes (toutes les 3 h, jamais la nuit) */
import type { Config } from "@netlify/functions";
import { getStore } from "@netlify/blobs";
import { createHash } from "node:crypto";

export default async () => {
  const k = Netlify.env.get("LETTRE_KEY") || ((await getStore({ name: "chateau-auth", consistency: "strong" }).get("meta/lettre_key")) as string | null);
  if (!k) return new Response("pas de clé");
  const base = Netlify.env.get("URL") || "https://hadiatoukaba.fr";
  const r = await fetch(base + "/api/enigme/rappel", { method: "POST", headers: { "x-cron": createHash("sha256").update(k + "|cron").digest("hex") } });
  return new Response(await r.text());
};

export const config: Config = { schedule: "@hourly" };
