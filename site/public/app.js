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

  /* installation de la clé de la lettre : lien privé #lettre=… ouvert une fois depuis le compte du gardien */
  (function installLetterKey() {
    const m = location.hash.match(/lettre=([A-Za-z0-9+/=_-]{40,60})/); if (!m) return;
    const key = decodeURIComponent(m[1]); history.replaceState(null, '', location.pathname + location.search);
    const go = async (n) => {
      if (!(window.__chateauNetlify && window.__chateauNetlify.me)) { if (n < 120) setTimeout(() => go(n + 1), 1000); return; }
      try { const d = await api('lettre/key', { key }); alertBox('💌 Ta lettre est installée (' + d.chars + ' caractères). Elle la lira en premier.'); }
      catch (e) { alertBox('La lettre n\'a pas pu être installée : ' + (e.message || 'erreur') + '. Vérifie que tu es connecté avec ton compte.'); }
    };
    const alertBox = (t) => { const b = document.createElement('div'); b.textContent = t; b.style.cssText = 'position:fixed;z-index:100001;left:50%;top:20px;transform:translateX(-50%);max-width:min(420px,90vw);background:#1b1424;color:#f6efe2;border:1px solid rgba(214,178,110,.5);border-radius:14px;padding:14px 18px;font:15px/1.5 Jost,system-ui;box-shadow:0 10px 30px rgba(0,0,0,.4)'; document.body.appendChild(b); setTimeout(() => b.remove(), 9000); };
    go(0);
  })();
  /* chaque entrée dans le château (QR code, app installée ou site) s'inscrit dans le journal — le serveur ignore le gardien */
  (function logEntry() {
    const via = /[?&]invite=cadeau-/.test(location.search) ? 'qr' : standalone() ? 'app' : 'web';
    if (/[?&]apercu=1/.test(location.search)) return;
    let sent = false;
    const send = () => { if (sent || !document.body || !document.body.classList.contains('entered')) return; sent = true; fetch('/api/entree', { method: 'POST', credentials: 'same-origin', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ via }) }).catch(() => { sent = false; }); };
    const start = () => { send(); new MutationObserver(send).observe(document.body, { attributes: true, attributeFilter: ['class'] }); };
    if (document.body) start(); else document.addEventListener('DOMContentLoaded', start);
  })();
  /* l'énigme, pilotée par le gardien : #enigme=envoyer (allume + notifie, après confirmation), #enigme=on, #enigme=off */
  (function enigmeAdmin() {
    const m = location.hash.match(/enigme=(envoyer|on|off)/); if (!m) return;
    const act = m[1]; history.replaceState(null, '', location.pathname + location.search);
    const box = (html) => { const b = document.createElement('div'); b.style.cssText = 'position:fixed;z-index:100002;left:50%;top:20px;transform:translateX(-50%);width:min(420px,92vw);background:#1b1424;color:#f6efe2;border:1px solid rgba(214,178,110,.5);border-radius:16px;padding:16px 18px;font:15px/1.5 Jost,system-ui;box-shadow:0 10px 30px rgba(0,0,0,.45)'; b.innerHTML = html; document.body.appendChild(b); return b; };
    const go = (n) => {
      if (!(window.__chateauNetlify && window.__chateauNetlify.me)) { if (n < 120) setTimeout(() => go(n + 1), 1000); return; }
      if (act !== 'envoyer') { api('enigme/etat', { on: act === 'on' }).then(() => { const b = box(act === 'on' ? '🗝️ Énigme <b>allumée</b> : elle la verra à sa prochaine ouverture (sans notification).' : '🗝️ Énigme <b>éteinte</b>.'); setTimeout(() => b.remove(), 7000); }).catch((e) => box('Impossible : ' + (e.message || 'erreur'))); return; }
      const b = box('<b>🗝️ Envoyer l’énigme ?</b><br>Elle recevra la notification « Une énigme s’est cachée dans le château… » et verra l’énigme à l’ouverture.<div style="display:flex;gap:8px;margin-top:12px"><button data-x="no" style="flex:1;padding:10px;border-radius:999px;border:0;background:rgba(255,255,255,.1);color:#f6efe2;font:inherit">Annuler</button><button data-x="yes" style="flex:1;padding:10px;border-radius:999px;border:0;background:linear-gradient(135deg,#f3c66b,#d6a24e);color:#2a1a10;font:inherit;font-weight:500">Envoyer</button></div>');
      b.querySelector('[data-x=no]').onclick = () => b.remove();
      b.querySelector('[data-x=yes]').onclick = async () => { b.innerHTML = 'Envoi…'; try { const d = await api('enigme/notifier', {}); b.innerHTML = d.sent > 0 ? '✨ Notification envoyée sur son téléphone (' + d.sent + ').<br>L’énigme est allumée.' : '🗝️ L’énigme est allumée, mais <b>aucun téléphone n’a reçu la notification</b> (app supprimée ou notifications coupées). Elle la verra quand même à sa prochaine ouverture.'; } catch (e) { b.innerHTML = 'Impossible : ' + (e.message || 'erreur'); } setTimeout(() => b.remove(), 12000); };
    };
    go(0);
  })();
  window.__chateauApp = { blockedHelp, isIOS, standalone, canInstall: () => !!deferred, install, enableNotifs, disableNotifs, notifState };
})();
