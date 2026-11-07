"""Fabrique public/index.html (version Netlify) à partir du musée de la page Claude.
Usage : python3 build_netlify.py /home/claude/chateau/index.html"""
import sys, re, os

src = open(sys.argv[1], encoding="utf-8").read()
out = src

def rep(a, b):
    global out
    assert out.count(a) == 1, ("introuvable ou multiple", a[:80])
    out = out.replace(a, b)

# 1. le pont vers le serveur Netlify, chargé avant le musée
out = '<!doctype html>\n<html lang="fr">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n<meta name="robots" content="noindex, nofollow">\n<link rel="manifest" href="/manifest.webmanifest">\n<link rel="apple-touch-icon" href="/apple-touch-icon.png">\n<meta name="theme-color" content="#0b0f24">\n<meta name="apple-mobile-web-app-capable" content="yes">\n<meta name="mobile-web-app-capable" content="yes">\n<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">\n<meta name="apple-mobile-web-app-title" content="Notre château">\n<script src="/shim.js"></script>\n<script src="/app.js" defer></script>\n' + out + '\n</html>\n'
# 2. attendre la connexion au lieu de 2,5 s
rep("const cl = await waitClaude(2500);", "const cl = await (window.__claudeReady || waitClaude(2500));")
# 3. réglages : inviter, importer, mot de passe, déconnexion
anchor = "    const backup = h('div', { class: 'field' },"
rep(anchor, """    const NF = window.__chateauNetlify;
    const account = NF ? h('div', { class: 'field' }, h('div', { text: 'Nos comptes', style: 'margin-bottom:6px' }),
      h('div', { class: 'mini', text: 'Connecté·e : ' + ((NF.me && (NF.me.name + ' · ' + NF.me.email)) || '') }),
      NF.me && NF.me.isOwner ? h('button', { class: 'ghost', text: '💌 Créer un lien d\\'invitation pour elle', style: 'margin-top:8px', onclick: async (e) => {
        const b = e.currentTarget; b.disabled = true;
        try { const link = await NF.invite(); try { await navigator.clipboard.writeText(link); toast('Lien copié : envoie-le-lui 💌'); } catch (er) {} const inp = h('input', { type: 'text', value: link, readonly: true, style: 'margin-top:8px' }); b.after(inp); inp.select(); }
        catch (er) { toast(er.message || 'Invitation impossible'); } b.disabled = false; } }) : null,
      NF.me && NF.me.isOwner ? h('button', { class: 'ghost', text: '📦 Importer les souvenirs de la page Claude', style: 'margin-top:8px', onclick: () => {
        const inp = h('input', { type: 'file', accept: '.json,application/json', style: 'display:none' }); document.body.appendChild(inp);
        inp.addEventListener('change', async () => { const f = inp.files[0]; inp.remove(); if (!f) return; try { const r = await NF.importFile(f, (t) => toast(t)); toast(r.already ? 'Vos souvenirs étaient déjà installés' : 'Souvenirs installés ✨ (' + r.docs + ' éléments, ' + r.files + ' fichiers)'); } catch (er) { toast(er.message || 'Import impossible'); } });
        inp.click(); } }) : null,
      (() => { const A = window.__chateauApp; if (!A) return null;
        const line = h('div', { class: 'mini', style: 'margin-top:12px', text: '…' });
        const nb = h('button', { class: 'ghost', text: '🔔 Notifications', style: 'margin-top:6px' });
        const ib = h('button', { class: 'ghost', text: '📱 Installer l\\'app sur ce téléphone', style: 'margin-top:8px;display:none' });
        const refresh = async () => { const st = await A.notifState().catch(() => 'unsupported');
          line.textContent = { on: '🔔 Notifications activées sur cet appareil', off: 'Notifications désactivées sur cet appareil', denied: 'Notifications bloquées : autorise-les dans les réglages du téléphone', 'ios-install': 'Sur iPhone : Partager ⬆︎ puis « Sur l\\'écran d\\'accueil », puis ouvre l\\'app depuis son icône pour activer les notifications', unsupported: 'Ce navigateur ne gère pas les notifications' }[st] || '';
          nb.textContent = st === 'on' ? '🔕 Couper les notifications' : '🔔 Activer les notifications'; nb.style.display = st === 'on' || st === 'off' ? '' : 'none';
          ib.style.display = A.canInstall() && !A.standalone() ? '' : 'none'; };
        nb.onclick = async () => { nb.disabled = true; try { if ((await A.notifState()) === 'on') { await A.disableNotifs(); toast('Notifications coupées'); } else { await A.enableNotifs(); toast('Notifications activées ✨'); } } catch (er) { toast(er.message || 'Impossible'); } nb.disabled = false; refresh(); };
        ib.onclick = async () => { await A.install(); refresh(); };
        refresh(); return h('div', {}, line, nb, ib); })(),
      h('button', { class: 'ghost', text: '🔑 Changer mon mot de passe', style: 'margin-top:8px', onclick: async () => {
        const cur = prompt('Mot de passe actuel'); if (!cur) return; const nx = prompt('Nouveau mot de passe (8 caractères minimum)'); if (!nx) return;
        try { await NF.password(cur, nx); toast('Mot de passe changé'); } catch (er) { toast(er.message || 'Changement impossible'); } } }),
      h('button', { class: 'ghost', text: 'Se déconnecter', style: 'margin-top:8px', onclick: () => NF.logout() })) : null;
""" + anchor)
rep(", backup];", ", account, backup];")
# 4. titre et description
out = re.sub(r"<title>.*?</title>", "<title>Notre château</title>", out, count=1)

os.makedirs("public", exist_ok=True)
open("public/index.html", "w", encoding="utf-8").write(out)
print("public/index.html prêt,", len(out), "caractères")
