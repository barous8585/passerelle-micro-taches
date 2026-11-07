/* Notre château — l'app sur le téléphone : installation sur l'écran d'accueil et notifications. */
(() => {
  const ua = navigator.userAgent || '';
  const isIOS = /iPhone|iPad|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const standalone = () => window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  const ls = { get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} } };
  let deferred = null, reg = null;

  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').then((r) => (reg = r)).catch(() => {});
  window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); deferred = e; });
  window.addEventListener('appinstalled', () => { deferred = null; ls.set('chateau-app-installed', '1'); hideBanner(); });

  const b64ToU8 = (s) => { const p = '='.repeat((4 - (s.length % 4)) % 4), b = atob((s + p).replace(/-/g, '+').replace(/_/g, '/')); return Uint8Array.from(b, (c) => c.charCodeAt(0)); };
  const api = async (path, body) => { const r = await fetch('/api/' + path, { method: body ? 'POST' : 'GET', credentials: 'same-origin', headers: body ? { 'content-type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined }); const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.message || 'Erreur ' + r.status); return d; };

  const pushSupported = () => 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  async function notifState() {
    if (!pushSupported()) return isIOS && !standalone() ? 'ios-install' : 'unsupported';
    if (Notification.permission === 'denied') return 'denied';
    const r = reg || (await navigator.serviceWorker.ready); const s = await r.pushManager.getSubscription();
    return s && Notification.permission === 'granted' ? 'on' : 'off';
  }
  async function enableNotifs() {
    if (!pushSupported()) throw new Error(isIOS ? 'Sur iPhone, installe d\'abord l\'app sur l\'écran d\'accueil, puis ouvre-la depuis son icône' : 'Ce navigateur ne gère pas les notifications');
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') throw new Error(blockedHelp());
    const r = reg || (await navigator.serviceWorker.ready);
    const { key } = await api('push/key');
    let sub = await r.pushManager.getSubscription();
    if (!sub) sub = await r.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToU8(key) });
    await api('push/subscribe', { sub: sub.toJSON() });
    ls.set('chateau-notifs', 'on');
    try { await api('push/test', {}); } catch (e) {}
    return true;
  }
  /* le téléphone a répondu « non » sans rien demander : l'autorisation a été coupée une fois, il faut la rouvrir à la main */
  function blockedHelp() {
    const samsung = /SamsungBrowser/.test(ua), android = /Android/.test(ua);
    if (isIOS) return 'Bloqué par l\'iPhone : Réglages → Notifications → Notre château → Autoriser les notifications.';
    if (android && samsung) return 'Bloqué par le téléphone. 1) Samsung Internet : ☰ → Paramètres → Sites et téléchargements → Autorisations des sites → Notifications → hadiatoukaba.fr → Autoriser. 2) Paramètres Android → Applications → Samsung Internet → Notifications → activer.';
    if (android) return 'Bloqué par le téléphone. 1) Paramètres Android → Applications → Chrome (et « Notre château » si l\'app est installée) → Notifications → activer. 2) Dans Chrome : ⋮ → Paramètres → Paramètres des sites → Notifications → hadiatoukaba.fr → Autoriser. Puis réessaie.';
    return 'Notifications bloquées : autorise-les pour hadiatoukaba.fr dans les réglages du navigateur (cadenas à gauche de l\'adresse).';
  }
  async function disableNotifs() {
    const r = reg || (await navigator.serviceWorker.ready); const s = await r.pushManager.getSubscription();
    if (s) { try { await api('push/unsubscribe', { endpoint: s.endpoint }); } catch (e) {} await s.unsubscribe(); }
    ls.set('chateau-notifs', 'off');
  }
  async function install() {
    if (deferred) { deferred.prompt(); const c = await deferred.userChoice.catch(() => null); deferred = null; return c && c.outcome === 'accepted'; }
    return false;
  }
  /* abonnement rafraîchi à chaque ouverture (les téléphones le renouvellent parfois) */
  setTimeout(async () => { try { if (pushSupported() && Notification.permission === 'granted' && window.__chateauNetlify && window.__chateauNetlify.me) { const r = reg || (await navigator.serviceWorker.ready); const s = await r.pushManager.getSubscription(); if (s) await api('push/subscribe', { sub: s.toJSON() }); } } catch (e) {} }, 8000);

  /* ───────── l'invitation discrète, une fois dans le château ───────── */
  const css = `#appInv{position:fixed;z-index:70;left:50%;bottom:calc(84px + env(safe-area-inset-bottom));transform:translate(-50%,20px);opacity:0;transition:.5s;width:min(380px,calc(100% - 28px));background:rgba(16,12,26,.94);color:#f6efe2;border:1px solid rgba(214,178,110,.35);border-radius:18px;padding:16px 16px 14px;box-shadow:0 14px 40px rgba(0,0,0,.45);font-family:"Jost",system-ui,sans-serif;font-size:14px;line-height:1.5}
  #appInv.on{opacity:1;transform:translate(-50%,0)}
  #appInv .t{font-family:"Cormorant Garamond",Georgia,serif;font-style:italic;font-size:22px;color:#f3d79a;margin:0 0 4px}
  #appInv .row{display:flex;gap:8px;margin-top:12px}
  #appInv button{flex:1;padding:11px 12px;border-radius:999px;border:0;font:inherit;font-weight:500;cursor:pointer}
  #appInv .go{background:linear-gradient(135deg,#f3c66b,#d6a24e);color:#2a1a10}
  #appInv .no{background:rgba(255,255,255,.08);color:rgba(246,239,226,.8)}
  #appInv .ios{margin-top:8px;padding:10px 12px;border-radius:12px;background:rgba(255,255,255,.06);font-size:13.5px}
  #appInv .ios b{color:#f3d79a;font-weight:500}`;
  function hideBanner() { const b = document.getElementById('appInv'); if (b) { b.classList.remove('on'); setTimeout(() => b.remove(), 500); } }
  function banner(title, text, goLabel, onGo, extra) {
    if (document.getElementById('appInv')) return;
    if (!document.getElementById('appInvCss')) { const st = document.createElement('style'); st.id = 'appInvCss'; st.textContent = css; document.head.appendChild(st); }
    const b = document.createElement('div'); b.id = 'appInv'; b.setAttribute('role', 'dialog');
    b.innerHTML = `<div class="t"></div><div class="x"></div>${extra || ''}<div class="row">${goLabel ? '<button class="go"></button>' : ''}<button class="no">Plus tard</button></div>`;
    b.querySelector('.t').textContent = title; b.querySelector('.x').textContent = text;
    if (goLabel) { const g = b.querySelector('.go'); g.textContent = goLabel; g.onclick = async () => { g.disabled = true; try { await onGo(); hideBanner(); } catch (e) { b.querySelector('.x').textContent = e.message || 'Impossible pour le moment'; g.disabled = false; } }; }
    b.querySelector('.no').onclick = () => { ls.set('chateau-inv-later', String(Date.now())); hideBanner(); };
    document.body.appendChild(b); requestAnimationFrame(() => requestAnimationFrame(() => b.classList.add('on')));
  }
  async function maybeInvite() {
    const later = Number(ls.get('chateau-inv-later') || 0);
    if (Date.now() - later < 3 * 864e5) return;
    const busy = !document.body.classList.contains('entered') || document.body.classList.contains('touring') || document.getElementById('welcome') || document.getElementById('door') && document.getElementById('door').offsetParent || document.querySelector('#atelier.open, .show#modal, #modal.show');
    if (busy || !(window.__chateauNetlify && window.__chateauNetlify.me)) return setTimeout(maybeInvite, 15000);
    const st = await notifState();
    if (st === 'ios-install') {
      banner('Notre château sur ton iPhone', 'Pour l\'avoir comme une vraie app, et recevoir un petit mot quand quelque chose de nouveau arrive :', null, null,
        '<div class="ios">1. Touche <b>Partager</b> <span style="font-size:16px">⬆︎</span> en bas de Safari<br>2. Choisis <b>« Sur l\'écran d\'accueil »</b><br>3. Ouvre le château depuis sa nouvelle icône</div>');
    } else if (!standalone() && deferred) {
      banner('Notre château, comme une app', 'Ajoute-le à ton écran d\'accueil : il s\'ouvre en plein écran, d\'un seul geste.', 'Installer', async () => { await install(); setTimeout(maybeInvite, 4000); });
    } else if (st === 'off') {
      banner('Être prévenu·e', 'Une notification quand l\'autre t\'écrit une lettre, accroche un souvenir ou entre dans le château.', 'Activer', enableNotifs);
    }
  }
  const start = () => setTimeout(maybeInvite, 45000);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();

  window.__chateauApp = { blockedHelp, isIOS, standalone, canInstall: () => !!deferred, install, enableNotifs, disableNotifs, notifState };
})();
