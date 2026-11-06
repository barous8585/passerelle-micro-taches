# Notre château — version Netlify

Le musée de la page Claude, avec son propre serveur sur Netlify :
- `public/index.html` : le musée (fabriqué par `build_netlify.py` à partir de la version Claude)
- `public/shim.js` : la porte d'entrée (comptes) et le lien entre le musée et le serveur
- `netlify/functions/api.mts` : comptes, base partagée, envoi de fichiers, présence en direct
- `netlify/functions/blob.mts` : sert les photos, sons et vidéos (uniquement aux personnes connectées)
- `netlify/functions/import-chateau/` : installe une seule fois les souvenirs venus de la page Claude
- Stockage : Netlify Blobs (privé, rien à configurer). La clé des sessions est créée toute seule.

Sécurité : seules deux personnes peuvent avoir un compte. Le premier compte créé est celui du gardien ;
le second ne peut être créé qu'avec un lien d'invitation (Atelier → Réglages), valable une seule fois.

Mettre à jour le musée après une modification de la version Claude :
    python3 build_netlify.py chemin/vers/index.html
