"""
Patch : contrôle qualité renforcé (blocage des soumissions non corrigées +
correction du bug qui écrasait silencieusement les corrections des workers
sur les lignes gold standard) + hausse du taux d'échantillonnage qualité.

Lance-le à la racine du projet (là où sont api.py, analyzer.py, etc.) :
    python3 patch_controle_qualite.py
"""
import re


def patch(chemin, remplacements, nom):
    with open(chemin, encoding="utf-8") as f:
        contenu = f.read()
    for i, (ancien, nouveau) in enumerate(remplacements, start=1):
        if ancien not in contenu:
            print(f"❌ {nom} : ancre {i}/{len(remplacements)} introuvable -- le fichier a peut-être déjà changé, patch annulé pour ce fichier.")
            return False
        contenu = contenu.replace(ancien, nouveau, 1)
        print(f"{i}/{len(remplacements)} : {nom} ok")
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)
    return True


# ---------------------------------------------------------------------------
# 1. analyzer.py -- taux d'échantillonnage qualité 8% -> 25%
# ---------------------------------------------------------------------------
patch("analyzer.py", [
    (
        'def process_csv_to_microtasks(file_path, project_id, schema_definition,\n'
        '                               taux_gold=0.08, gold_answers=None):',
        'def process_csv_to_microtasks(file_path, project_id, schema_definition,\n'
        '                               taux_gold=0.25, gold_answers=None):',
    ),
], "analyzer.py")


# ---------------------------------------------------------------------------
# 2. api.py -- import, blocage à la soumission, correction du bug gold standard
# ---------------------------------------------------------------------------
patch("api.py", [
    (
        'from analyzer import (infer_schema, normaliser_automatiquement,\n'
        '                       process_csv_to_microtasks, valider_consensus)',
        'from analyzer import (detect_anomalies, infer_schema, normaliser_automatiquement,\n'
        '                       normaliser_espaces, process_csv_to_microtasks, valider_consensus)',
    ),
    (
        '        # Recombine les corrections du worker (colonnes visibles) avec les\n'
        '        # valeurs des colonnes sensibles -- jamais montrées ni éditées par\n'
        '        # lui, juste normalisées automatiquement (espaces).\n'
        '        reponse_complete = _reconstituer_reponse_complete(\n'
        '            task.header, dechiffrer_json(task.raw_data), payload.reponse, task.schema_json\n'
        '        )\n',
        '        # Recombine les corrections du worker (colonnes visibles) avec les\n'
        '        # valeurs des colonnes sensibles -- jamais montrées ni éditées par\n'
        '        # lui, juste normalisées automatiquement (espaces).\n'
        '        reponse_complete = _reconstituer_reponse_complete(\n'
        '            task.header, dechiffrer_json(task.raw_data), payload.reponse, task.schema_json\n'
        '        )\n'
        '\n'
        '        # Contrôle qualité bloquant : les badges affichés au worker (champ\n'
        '        # requis vide, format invalide) ne servaient jusqu\'ici que d\'indice\n'
        '        # visuel -- rien n\'empêchait de soumettre quand même sans corriger.\n'
        '        # On relance la même détection sur SA réponse et on refuse la\n'
        '        # soumission si un problème bloquant subsiste sur une colonne QU\'IL\n'
        '        # PEUT VOIR ET CORRIGER -- jamais sur une colonne sensible, que le\n'
        '        # worker ne voit ni ne modifie jamais (ça bloquerait sans issue).\n'
        '        # Les simples avertissements (espaces) ne bloquent pas : ils sont\n'
        '        # déjà corrigés silencieusement ci-dessous.\n'
        '        badges_apres_soumission = detect_anomalies(reponse_complete, task.header, task.schema_json)\n'
        '        _, _, badges_visibles_apres_soumission = _filtrer_pour_worker(\n'
        '            task.header, reponse_complete, badges_apres_soumission, task.schema_json\n'
        '        )\n'
        '        bloquants_visibles = [\n'
        '            b for b in badges_visibles_apres_soumission\n'
        '            if b["type"] == "danger" or b["msg"] == "Champ vide/absent"\n'
        '        ]\n'
        '        if bloquants_visibles:\n'
        '            raise HTTPException(\n'
        '                status_code=422,\n'
        '                detail={\n'
        '                    "message": "Certains champs ne sont pas encore corrects, corrige-les avant de valider.",\n'
        '                    "colonnes": bloquants_visibles,\n'
        '                },\n'
        '            )\n'
        '\n'
        '        # Les espaces superflus restants (colonnes non couvertes par la\n'
        '        # normalisation automatique par type, ex. ville/code postal) sont\n'
        '        # corrigés silencieusement plutôt que de pénaliser le worker pour ça.\n'
        '        reponse_complete = [normaliser_espaces(v) for v in reponse_complete]\n',
    ),
    (
        '        # --- Cas gold standard : seul cas où le trust_score bouge FORTEMENT,\n'
        '        # car c\'est le seul signal 100% fiable (on connaît la vraie réponse).\n'
        '        # Le worker est payé dans tous les cas -- gold standard = contrôle\n'
        '        # qualité invisible, pas une pénalité de rémunération. ---\n'
        '        if task.is_gold_standard:\n'
        '            correct = reponse_complete == dechiffrer_json(task.gold_answer)\n'
        '            worker = db.query(User).get(worker_id)\n'
        '            worker.trust_score = min(100.0, worker.trust_score + 1) if correct \\\n'
        '                else max(0.0, worker.trust_score - 5)\n'
        '            worker.tasks_completed += 1\n'
        '            prix = db.query(Project).get(task.project_id).prix_par_ligne\n'
        '            worker.solde_disponible += prix\n'
        '            db.query(Submission).filter_by(micro_task_id=task_id, worker_id=worker_id).update({"montant": prix})\n'
        '            task.status = TaskStatus.completed\n'
        '            # La réponse de référence du gold standard EST la valeur propre connue\n'
        '            # -- on la restitue telle quelle au client, peu importe ce que le worker a tapé.\n'
        '            task.resultat_final = task.gold_answer\n'
        '            db.commit()\n'
        '            return {"status": "completed", "paye": True}',
        '        # --- Cas gold standard : sert à calibrer le trust_score d\'un worker\n'
        '        # sur des lignes dont on connaît déjà la réponse attendue.\n'
        '        #\n'
        '        # ATTENTION -- ici, "réponse attendue" vient à 100% du nettoyage\n'
        '        # automatique lui-même (aucune vérité externe fournie), donc ce\n'
        '        # n\'est PAS une vérité absolue : si le worker n\'est pas d\'accord\n'
        '        # avec elle, ça peut vouloir dire qu\'il a repéré une vraie erreur\n'
        '        # que la machine avait laissée passer. Avant, on jetait purement et\n'
        '        # simplement sa correction et on gardait la version machine -- une\n'
        '        # ligne fausse pouvait donc rester "correcte" pour toujours, sans\n'
        '        # que personne ne s\'en aperçoive. Maintenant, un désaccord est traité\n'
        '        # comme un litige normal (voir "Projets & litiges" dans l\'admin) : la\n'
        '        # correction du worker est conservée et la ligne est signalée pour\n'
        '        # arbitrage humain, au lieu d\'être écrasée en silence.\n'
        '        if task.is_gold_standard:\n'
        '            reponse_reference = dechiffrer_json(task.gold_answer)\n'
        '            correct = reponse_complete == reponse_reference\n'
        '            worker = db.query(User).get(worker_id)\n'
        '            worker.trust_score = min(100.0, worker.trust_score + 1) if correct \\\n'
        '                else max(0.0, worker.trust_score - 5)\n'
        '            worker.tasks_completed += 1\n'
        '            prix = db.query(Project).get(task.project_id).prix_par_ligne\n'
        '            worker.solde_disponible += prix\n'
        '            db.query(Submission).filter_by(micro_task_id=task_id, worker_id=worker_id).update({"montant": prix})\n'
        '            task.status = TaskStatus.completed\n'
        '            if correct:\n'
        '                task.resultat_final = task.gold_answer\n'
        '                task.eu_litige = False\n'
        '            else:\n'
        '                task.resultat_final = chiffrer_json(reponse_complete)\n'
        '                task.eu_litige = True\n'
        '            db.commit()\n'
        '            return {"status": "completed", "paye": True}',
    ),
], "api.py")


# ---------------------------------------------------------------------------
# 3. worker_dashboard.html -- affichage clair des champs bloquants + style
# ---------------------------------------------------------------------------
patch("worker_dashboard.html", [
    (
        '  .champ input.a-corriger { border-color: var(--danger); }\n'
        '  .champ input.a-verifier { border-color: var(--warning); }',
        '  .champ input.a-corriger { border-color: var(--danger); }\n'
        '  .champ input.a-verifier { border-color: var(--warning); }\n'
        '  .champ input.input-bloquant { border-color: var(--danger); box-shadow: 0 0 0 3px var(--danger-bg); animation: secouer .3s; }\n'
        '  @keyframes secouer { 0%, 100% { transform: translateX(0); } 25% { transform: translateX(-4px); } 75% { transform: translateX(4px); } }',
    ),
    (
        "    if (!res.ok) {\n"
        "      msg.textContent = \"Erreur lors de l'envoi. Réessaie.\";\n"
        "      btnPasser.disabled = false;\n"
        "      btnValider.disabled = false;\n"
        "      btnValider.textContent = 'Valider';\n"
        "      return;\n"
        "    }",
        "    if (!res.ok) {\n"
        "      inputs.forEach(inp => inp.classList.remove('input-bloquant'));\n"
        "      if (res.status === 422) {\n"
        "        const erreur = await res.json();\n"
        "        const colonnes = (erreur.detail && erreur.detail.colonnes) || [];\n"
        "        colonnes.forEach(b => {\n"
        "          const champ = document.querySelector(`#zone_tache input[data-col=\"${b.col_index}\"]`);\n"
        "          if (champ) { champ.classList.add('input-bloquant'); champ.focus(); }\n"
        "        });\n"
        "        msg.textContent = colonnes.length\n"
        "          ? `À corriger avant de valider : ${colonnes.map(b => b.msg).join(', ')}.`\n"
        "          : \"Certains champs ne sont pas encore corrects, corrige-les avant de valider.\";\n"
        "      } else {\n"
        "        msg.textContent = \"Erreur lors de l'envoi. Réessaie.\";\n"
        "      }\n"
        "      btnPasser.disabled = false;\n"
        "      btnValider.disabled = false;\n"
        "      btnValider.textContent = 'Valider';\n"
        "      return;\n"
        "    }",
    ),
], "worker_dashboard.html")


# ---------------------------------------------------------------------------
# 4. tests/test_api.py -- adapte un test au nouveau blocage (corrige la date
#    avant de soumettre, comme le ferait un vrai worker maintenant)
# ---------------------------------------------------------------------------
patch("tests/test_api.py", [
    (
        '    # Le worker ne soumet que les 2 colonnes visibles\n'
        '    r = client.post(\n'
        '        f"/tasks/{tache[\'task_id\']}/submit", json={"reponse": tache["raw_data"]},\n'
        '        headers=auth_header("w1@uco.fr", "pw123"),\n'
        '    )\n'
        '    assert r.status_code == 200',
        '    # Le worker ne soumet que les 2 colonnes visibles -- si l\'une d\'elles a\n'
        '    # un badge bloquant (ex. date ambiguë), il doit d\'abord la corriger avant\n'
        '    # que la soumission soit acceptée.\n'
        '    reponse_corrigee = list(tache["raw_data"])\n'
        '    idx_date = tache["header"].index("date_naiss")\n'
        '    reponse_corrigee[idx_date] = "1998-04-03"\n'
        '    r = client.post(\n'
        '        f"/tasks/{tache[\'task_id\']}/submit", json={"reponse": reponse_corrigee},\n'
        '        headers=auth_header("w1@uco.fr", "pw123"),\n'
        '    )\n'
        '    assert r.status_code == 200',
    ),
], "tests/test_api.py")

print("\nTerminé.")
