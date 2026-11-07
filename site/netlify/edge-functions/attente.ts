/* Avant l'heure du cadeau, le QR code (hadiatoukaba.fr) affiche une page d'attente avec un compte à rebours.
   À l'heure dite, la même adresse ouvre le musée — sans rien changer au papier.
   thaka.fr n'est pas concerné. Pour vérifier le musée avant l'heure : ajouter ?apercu=1 à l'adresse. */

// minuit, heure de Paris (UTC+1 en hiver)
const OUVERTURE = Date.parse("2026-11-08T00:00:00+01:00");
const DOMAINES = ["hadiatoukaba.fr", "www.hadiatoukaba.fr", "hadiatoukaba.eu", "www.hadiatoukaba.eu"];

export default async (req: Request, context: { next: () => Promise<Response> }) => {
  const url = new URL(req.url);
  const reste = OUVERTURE - Date.now();
  if (reste <= 0 || !DOMAINES.includes(url.hostname) || url.searchParams.has("apercu")) return context.next();
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
  main{position:relative;z-index:1;max-width:520px}
  .coeur{font-size:44px;color:#e2486a;animation:bat 1.6s ease-in-out infinite;text-shadow:0 0 24px rgba(226,72,106,.6)}
  @keyframes bat{0%,100%{transform:scale(1)}15%{transform:scale(1.18)}30%{transform:scale(1)}45%{transform:scale(1.12)}}
  .k{font-family:Inter,sans-serif;font-size:11px;letter-spacing:.42em;text-transform:uppercase;color:#d6ad62;margin:18px 0 10px}
  h1{font-weight:500;font-style:italic;font-size:clamp(38px,9vw,56px);line-height:1.05}
  p{font-size:clamp(19px,4.6vw,23px);line-height:1.45;color:rgba(246,239,226,.82);margin-top:16px;font-style:italic}
  .cpt{display:flex;gap:10px;justify-content:center;margin:30px 0 8px}
  .cpt div{min-width:78px;padding:14px 8px 10px;border:1px solid rgba(214,173,98,.45);border-radius:14px;background:rgba(255,255,255,.04)}
  .cpt b{display:block;font-family:Inter,sans-serif;font-weight:500;font-size:clamp(26px,7vw,34px);color:#f3d79a;font-variant-numeric:tabular-nums}
  .cpt span{font-family:Inter,sans-serif;font-size:10.5px;letter-spacing:.2em;text-transform:uppercase;color:rgba(246,239,226,.55)}
  .bas{font-size:17px;margin-top:26px;color:rgba(246,239,226,.6)}
</style></head><body>
<canvas id="c"></canvas>
<main>
  <div class="coeur">♥</div>
  <div class="k">Un cadeau pour toi</div>
  <h1>Hadiatou,<br>patience…</h1>
  <p>Ce QR code s'ouvrira à minuit.<br>Reviens le scanner à 00h00 : quelque chose t'attend.</p>
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
  // un ciel d'étoiles qui scintillent
  const c = document.getElementById('c'), x = c.getContext('2d'); let W, H, E = [];
  const taille = () => { W = c.width = innerWidth * devicePixelRatio; H = c.height = innerHeight * devicePixelRatio; E = Array.from({ length: 140 }, () => ({ x: Math.random() * W, y: Math.random() * H, r: (Math.random() * 1.3 + .3) * devicePixelRatio, p: Math.random() * 6.28 })); };
  addEventListener('resize', taille); taille();
  (function ciel(t) { x.clearRect(0, 0, W, H); E.forEach((e) => { x.globalAlpha = .35 + .65 * Math.abs(Math.sin(t / 1400 + e.p)); x.fillStyle = '#fff4dc'; x.beginPath(); x.arc(e.x, e.y, e.r, 0, 7); x.fill(); }); requestAnimationFrame(ciel); })(0);
</script>
</body></html>`;
