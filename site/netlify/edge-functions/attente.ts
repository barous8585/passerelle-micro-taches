/* Avant l'heure du cadeau, le QR code (hadiatoukaba.fr) affiche une page d'attente avec un compte à rebours.
   À l'heure dite, la même adresse ouvre le musée — sans rien changer au papier.
   thaka.fr n'est pas concerné. Pour vérifier le musée avant l'heure : ajouter ?apercu=1 à l'adresse. */

// minuit, heure de Paris (UTC+1 en hiver)
const OUVERTURE = Date.parse("2026-11-08T00:00:00+01:00");
const DOMAINES = ["hadiatoukaba.fr", "www.hadiatoukaba.fr", "hadiatoukaba.eu", "www.hadiatoukaba.eu"];

export default async (req: Request, context: { next: () => Promise<Response> }) => {
  const url = new URL(req.url);
  const reste = OUVERTURE - Date.now();
  const cadeau = /^cadeau-/.test(url.searchParams.get("invite") || "");   // le lien du QR code, sur n'importe quelle adresse
  if (reste <= 0 || !(DOMAINES.includes(url.hostname) || cadeau) || url.searchParams.has("apercu")) return context.next();
  return new Response(page(reste), { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
};

export const config = { path: "/" };

const page = (reste: number) => `<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<title>Pour toi, Hadiatou</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,500;1,400;1,500&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
  *{box-sizing:border-box;margin:0}
  html,body{height:100%}
  body{background:radial-gradient(120% 90% at 50% 0%,#2a1730 0%,#140c1c 55%,#0a0710 100%);color:#f6efe2;font-family:'Cormorant Garamond',Georgia,serif;display:flex;align-items:center;justify-content:center;padding:24px 16px;overflow:hidden;text-align:center}
  canvas{position:fixed;inset:0;width:100%;height:100%;z-index:0}
  main{position:relative;z-index:1;max-width:520px;width:100%}
  .photo{position:relative;width:min(58vw,250px);aspect-ratio:3/4;margin:0 auto 4px;border-radius:18px;padding:5px;background:linear-gradient(145deg,#f3d79a,#a8803f 45%,#f0cf8a);box-shadow:0 18px 50px rgba(0,0,0,.55),0 0 40px rgba(226,72,106,.18);opacity:0;transform:translateY(10px) scale(.98);transition:opacity 1.4s ease,transform 1.4s ease}
  .photo.on{opacity:1;transform:none}
  .photo img{display:block;width:100%;height:100%;object-fit:cover;border-radius:14px}
  .photo .coeur{position:absolute;left:50%;bottom:-24px;transform:translateX(-50%);font-size:38px}
  .sans .coeur{font-size:44px}
  .coeur{font-size:44px;color:#e2486a;animation:bat 1.6s ease-in-out infinite;text-shadow:0 0 24px rgba(226,72,106,.6);will-change:transform}
  @keyframes bat{0%,100%{transform:scale(1)}15%{transform:scale(1.18)}30%{transform:scale(1)}45%{transform:scale(1.12)}}
  .k{font-family:Inter,sans-serif;font-size:11px;letter-spacing:.42em;text-transform:uppercase;color:#d6ad62;margin:30px 0 8px}
  .sans .k{margin-top:18px}
  h1{font-weight:500;font-style:italic;font-size:clamp(38px,9vw,56px);line-height:1.05}
  p{font-size:clamp(18px,4.4vw,23px);line-height:1.4;color:rgba(246,239,226,.82);margin-top:12px;font-style:italic}
  .cpt{display:flex;gap:10px;justify-content:center;margin:22px 0 6px}
  .cpt div{min-width:76px;padding:11px 8px 8px;border:1px solid rgba(214,173,98,.45);border-radius:14px;background:rgba(255,255,255,.04)}
  .cpt b{display:block;font-family:Inter,sans-serif;font-weight:500;font-size:clamp(26px,7vw,34px);color:#f3d79a;font-variant-numeric:tabular-nums}
  .cpt span{font-family:Inter,sans-serif;font-size:10.5px;letter-spacing:.2em;text-transform:uppercase;color:rgba(246,239,226,.55)}
  .h{font-variant-numeric:lining-nums;font-feature-settings:'lnum' 1;font-style:normal;font-weight:500;color:#f3d79a}
  .bas{font-size:16.5px;margin-top:18px;color:rgba(246,239,226,.6)}
</style></head><body>
<canvas id="c"></canvas><canvas id="c2"></canvas>
<main class="sans">
  <div class="photo" id="ph" hidden><img id="im" alt="Nous deux"><div class="coeur">♥</div></div>
  <div class="coeur" id="c0">♥</div>
  <div class="k">Un cadeau pour toi</div>
  <h1>Hadiatou,<br>patience…</h1>
  <p>Ce QR code s'ouvrira à minuit.<br>Reviens le scanner à <span class="h">00h00</span> : quelque chose t'attend.</p>
  <div class="cpt"><div><b id="h">--</b><span>heures</span></div><div><b id="m">--</b><span>minutes</span></div><div><b id="s">--</b><span>secondes</span></div></div>
  <div class="bas">Garde-le précieusement : il ne s'ouvrira que pour toi, et pour toujours.</div>
</main>
<script>
  // le temps restant vient du serveur : l'horloge du téléphone n'a pas d'importance
  const fin = performance.now() + ${reste};
  const z = (n) => String(n).padStart(2, '0');
  (function tic() {
    const r = Math.max(0, fin - performance.now()), t = Math.ceil(r / 1000);
    document.getElementById('h').textContent = z(Math.floor(t / 3600));
    document.getElementById('m').textContent = z(Math.floor(t / 60) % 60);
    document.getElementById('s').textContent = z(t % 60);
    if (r <= 0) { setTimeout(() => location.reload(), 900); return; }
    setTimeout(tic, 250);
  })();
  // notre photo : chiffrée, elle ne s'affiche qu'avec le code contenu dans le QR code
  (async () => {
    try {
      const tok = new URLSearchParams(location.search).get('invite'); if (!tok || !crypto.subtle) return;
      const buf = new Uint8Array(await (await fetch('/attente.bin')).arrayBuffer());
      const key = await crypto.subtle.importKey('raw', await crypto.subtle.digest('SHA-256', new TextEncoder().encode(tok)), 'AES-GCM', false, ['decrypt']);
      const jpg = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: buf.slice(0, 12) }, key, buf.slice(12));
      const im = document.getElementById('im'), ph = document.getElementById('ph');
      im.onload = () => { document.getElementById('c0').remove(); document.querySelector('main').classList.remove('sans'); ph.hidden = false; requestAnimationFrame(() => ph.classList.add('on')); };
      im.src = URL.createObjectURL(new Blob([jpg], { type: 'image/jpeg' }));
    } catch (e) {}
  })();
  // un ciel d'étoiles : dessiné une seule fois ; seules quelques étoiles scintillent, 12 fois par seconde
  const c = document.getElementById('c'), c2 = document.getElementById('c2'), x = c.getContext('2d'), x2 = c2.getContext('2d');
  const R = Math.min(devicePixelRatio || 1, 1.5); let W, H, T = [];
  const taille = () => {
    W = c.width = c2.width = Math.round(innerWidth * R); H = c.height = c2.height = Math.round(innerHeight * R);
    x.clearRect(0, 0, W, H); x.fillStyle = '#fff4dc';
    for (let i = 0; i < 120; i++) { x.globalAlpha = .25 + Math.random() * .6; x.beginPath(); x.arc(Math.random() * W, Math.random() * H, (Math.random() * 1.2 + .3) * R, 0, 7); x.fill(); }
    T = Array.from({ length: 22 }, () => ({ x: Math.random() * W, y: Math.random() * H, r: (Math.random() * 1.1 + .7) * R, p: Math.random() * 6.28 }));
  };
  addEventListener('resize', taille); taille();
  const scint = () => { const t = performance.now(); x2.clearRect(0, 0, W, H); x2.fillStyle = '#fff4dc'; T.forEach((e) => { x2.globalAlpha = Math.abs(Math.sin(t / 900 + e.p)); x2.beginPath(); x2.arc(e.x, e.y, e.r, 0, 7); x2.fill(); }); };
  scint(); setInterval(() => { if (!document.hidden) scint(); }, 85);
</script>
</body></html>`;
