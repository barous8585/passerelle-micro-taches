"""
analyzer.py — Moteur de découpage et de détection d'anomalies
pour la plateforme de micro-tâches de data-cleaning.

Corrige et enrichit le script initial :
  - détection PILOTÉE PAR LE SCHÉMA (plus de faux positifs sur des colonnes non concernées)
  - regex de date effectivement utilisée
  - injection de gold standard (tâches pièges à réponse connue)
  - attribution du niveau de redondance (nb de workers par tâche)
  - fonction de consensus par similarité pour le texte libre
"""

import csv
import datetime
import json
import random
import re
from difflib import SequenceMatcher

import regex  # bibliothèque tierce (distincte de re) : supporte un timeout
              # sur le matching -- indispensable car col["rule"] est fourni
              # par le client et peut contenir un motif ReDoS catastrophique.

# ---------------------------------------------------------------------------
# 1. RÈGLES DE VALIDATION PAR TYPE DE CHAMP
# ---------------------------------------------------------------------------

TIMEOUT_REGEX_CLIENT = 0.5  # secondes -- au-delà, on considère le motif comme
                            # trop coûteux et on traite la valeur comme invalide
                            # plutôt que de bloquer le serveur pour tout le monde.

TELEPHONE_RE = re.compile(r"^\+33[1-9]\d{8}$")  # format cible après normalisation


def _valider_regex_client(value, rule):
    """Applique une regex FOURNIE PAR LE CLIENT à une valeur, avec un
    filet de sécurité contre les motifs ReDoS (répétitions imbriquées,
    alternances catastrophiques) et les motifs syntaxiquement invalides."""
    try:
        return bool(regex.match(rule, value, timeout=TIMEOUT_REGEX_CLIENT))
    except TimeoutError:
        return False  # motif trop coûteux à évaluer -- traité comme non conforme
    except regex.error:
        return False  # motif mal formé -- idem, pas une raison de planter


VALIDATORS = {
    "regex": _valider_regex_client,
    "date": lambda value, rule: bool(re.match(r"^\d{4}-\d{2}-\d{2}$", value)),
    "telephone": lambda value, rule: bool(TELEPHONE_RE.match(value)),
    "required": lambda value, rule: value.strip() not in ("", "null", "n/a", "none", "?"),
}


# ---------------------------------------------------------------------------
# 1bis. NORMALISATION AUTOMATIQUE -- appliquée AVANT la détection d'anomalies,
# pour que le worker ne perde jamais son temps sur ce qu'un script règle en
# une fraction de seconde. Chaque fonction ne fait qu'UNE chose déterministe
# (pas de "devinette" sur le contenu) -- voir DOMAINES_EMAIL_CONNUS plus bas
# pour le seul cas où on hésite volontairement à corriger seul.
# ---------------------------------------------------------------------------

def normaliser_espaces(valeur):
    if not isinstance(valeur, str):
        return valeur
    return re.sub(r"\s+", " ", valeur).strip()


def normaliser_telephone(valeur):
    """Convertit un numéro français vers le format +33XXXXXXXXX, quels que
    soient les espaces/points/tirets ou le préfixe (0X, +33X, 0033X)."""
    brut = re.sub(r"[^\d+]", "", valeur)
    if brut.startswith("0033"):
        brut = "+33" + brut[4:]
    elif brut.startswith("33") and not brut.startswith("+"):
        brut = "+" + brut
    elif brut.startswith("0") and len(brut) == 10:
        brut = "+33" + brut[1:]
    return brut


def normaliser_date(valeur):
    """Convertit JJ/MM/AAAA ou JJ-MM-AAAA vers AAAA-MM-JJ, mais UNIQUEMENT
    quand le format n'est pas ambigu (jour > 12, donc forcément le jour et
    pas le mois). Sinon on laisse tel quel -- mieux vaut la faire flaguer
    "ambiguë" que de deviner et se tromper un mois sur deux."""
    for sep in ("/", "-"):
        parts = valeur.split(sep)
        if len(parts) == 3 and all(p.isdigit() for p in parts) and len(parts[2]) == 4:
            a, b, annee = int(parts[0]), int(parts[1]), int(parts[2])
            if a > 12 >= b:  # a ne peut être que le jour
                try:
                    return datetime.date(annee, b, a).isoformat()
                except ValueError:
                    return valeur  # date impossible (ex: 31 février) -- laissée pour être flaguée
    return valeur


def normaliser_nom(valeur):
    """Majuscule en début de chaque mot -- imparfait sur des cas rares
    (ex: 'McDonald' redevient 'Mcdonald'), volontairement accepté car
    l'enjeu est cosmétique, pas une erreur de fond sur la donnée."""
    valeur = normaliser_espaces(valeur)
    return valeur.title() if valeur else valeur


NORMALISATION_PAR_TYPE = {
    "telephone": normaliser_telephone,
    "date": normaliser_date,
    "nom": normaliser_nom,
}


# Domaines email très courants -- sert UNIQUEMENT à suggérer une correction
# au worker (badge d'alerte), jamais à corriger seul. Deviner un domaine
# email et se tromper produirait une donnée fausse avec une confiance totale.
DOMAINES_EMAIL_CONNUS = [
    "gmail.com", "hotmail.com", "hotmail.fr", "outlook.com", "outlook.fr",
    "yahoo.com", "yahoo.fr", "orange.fr", "wanadoo.fr", "free.fr",
    "laposte.net", "sfr.fr", "icloud.com", "live.fr",
]


def suggerer_domaine_email(valeur):
    """Si le domaine ressemble fortement (distance d'édition <= 2) à un
    domaine connu sans y correspondre exactement, retourne une suggestion
    textuelle -- ne modifie jamais la valeur elle-même."""
    if "@" not in valeur:
        return None
    domaine = valeur.rsplit("@", 1)[-1].lower().strip()
    if domaine in DOMAINES_EMAIL_CONNUS:
        return None  # déjà correct, rien à suggérer
    for connu in DOMAINES_EMAIL_CONNUS:
        if abs(len(domaine) - len(connu)) <= 2 and SequenceMatcher(None, domaine, connu).ratio() >= 0.75:
            return connu
    return None


def nettoyer_ligne(row, header, schema):
    """
    Applique la normalisation automatique déterministe à chaque colonne
    selon son type déclaré, puis lance la détection d'anomalies sur la
    version nettoyée. Retourne (ligne_nettoyee, badges) -- si badges est
    vide, la ligne peut être validée sans aucune intervention humaine.
    """
    col_index_by_name = {name: i for i, name in enumerate(header)}
    ligne_nettoyee = list(row)

    for col in schema["columns"]:
        idx = col_index_by_name.get(col["name"])
        if idx is None or idx >= len(ligne_nettoyee):
            continue
        valeur = normaliser_espaces(ligne_nettoyee[idx])
        fonction = NORMALISATION_PAR_TYPE.get(col.get("type"))
        if fonction:
            valeur = fonction(valeur)
        ligne_nettoyee[idx] = valeur

    badges = detect_anomalies(ligne_nettoyee, header, schema)

    # Suggestion de domaine email (n'empêche jamais la validation automatique
    # d'être bloquée -- si le format est déjà valide par ailleurs, on ajoute
    # juste un avertissement informatif au lieu de compter ça comme une vraie
    # anomalie bloquante).
    for col in schema["columns"]:
        if col.get("type") != "regex":
            continue
        idx = col_index_by_name.get(col["name"])
        if idx is None:
            continue
        suggestion = suggerer_domaine_email(ligne_nettoyee[idx])
        if suggestion:
            badges.append({
                "col_index": idx, "type": "warning",
                "msg": f"Domaine probablement '{suggestion}' ?",
            })

    return ligne_nettoyee, badges


def detect_anomalies(row, header, schema):
    """
    Analyse une ligne en appliquant UNIQUEMENT les règles définies dans le
    schéma pour chaque colonne (fini les faux positifs génériques).
    """
    badges = []
    col_index_by_name = {name: i for i, name in enumerate(header)}

    for col in schema["columns"]:
        col_name = col["name"]
        if col_name not in col_index_by_name:
            continue  # colonne absente du fichier réel, on ignore

        idx = col_index_by_name[col_name]
        if idx >= len(row):
            continue
        raw_value = row[idx]
        value = raw_value.strip()

        # Espaces superflus (début/fin ou doublés à l'intérieur)
        if value != raw_value or "  " in value:
            badges.append({"col_index": idx, "type": "warning", "msg": "Espaces superflus"})

        # Champ requis mais vide/suspect
        if col.get("required") and not VALIDATORS["required"](value, None):
            badges.append({"col_index": idx, "type": "info", "msg": "Champ vide/absent"})
            continue  # inutile de tester le format si vide

        # Validation de format selon le type déclaré dans le schéma
        col_type = col.get("type")
        if col_type == "regex" and value:
            if not VALIDATORS["regex"](value, col["rule"]):
                badges.append({"col_index": idx, "type": "danger", "msg": f"Format {col_name} invalide"})
        elif col_type == "date" and value:
            if not VALIDATORS["date"](value, None):
                badges.append({"col_index": idx, "type": "danger", "msg": "Format de date ambigu (attendu AAAA-MM-JJ)"})

    return badges


# ---------------------------------------------------------------------------
# 2. DÉCOUPAGE EN MICRO-TÂCHES + GOLD STANDARD + REDONDANCE
# ---------------------------------------------------------------------------

def normaliser_automatiquement(valeur):
    """
    Seule correction appliquée aux colonnes SENSIBLES, jamais montrées à un
    worker -- volontairement minimale (juste les espaces) car aucune
    intelligence sémantique par type n'est assez sûre pour corriger une
    donnée que personne ne relit humainement. Une vraie faute de frappe
    dans le nom ou l'email restera non corrigée : c'est le prix du choix
    de confidentialité, assumé plutôt que masqué.
    """
    return normaliser_espaces(valeur)


def choisir_redondance(nb_badges, seuil_critique=2):
    """Plus une ligne a d'anomalies détectées, plus elle est jugée risquée
    -> on lui assigne plus de workers pour croiser les réponses."""
    if nb_badges >= seuil_critique:
        return 3
    if nb_badges >= 1:
        return 2
    return 1  # ligne propre : un seul passage suffit


# ---------------------------------------------------------------------------
# 1bis. INFÉRENCE AUTOMATIQUE DU SCHÉMA (interface client)
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"^[\w.-]+@[\w.-]+\.\w+$")
DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_FR_RE = re.compile(r"^\d{2}[/-]\d{2}[/-]\d{4}$")
NUMBER_RE = re.compile(r"^-?\d+([.,]\d+)?$")


NOM_COLONNE_INDICES = {
    "regex_email": ("mail",),
    "date": ("date", "naiss", "created", "born"),
    "number": ("age", "prix", "montant", "qty", "quantite", "num", "id"),
}


def _deviner_type_colonne(valeurs, nom_colonne="", seuil_majorite=0.6):
    """Devine le type d'une colonne à partir d'un échantillon de valeurs.

    Vote majoritaire plutôt qu'accord unanime : le client dépose justement
    un fichier SALE, donc exiger 100% de conformité empêcherait quasiment
    toute inférence utile (une seule valeur cassée ferait tout basculer en
    'string'). On considère qu'une colonne est du type X si au moins
    `seuil_majorite` des valeurs non vides matchent -- les autres deviendront
    des badges d'anomalie lors du découpage, ce qui est le comportement voulu.
    """
    non_vides = [v.strip() for v in valeurs if v.strip()]
    if not non_vides:
        return {"type": "string"}

    n = len(non_vides)
    part_email = sum(bool(EMAIL_RE.match(v)) for v in non_vides) / n
    part_date = sum(bool(DATE_ISO_RE.match(v) or DATE_FR_RE.match(v)) for v in non_vides) / n
    part_number = sum(bool(NUMBER_RE.match(v)) for v in non_vides) / n

    # Le nom de la colonne est un indice fort qu'on ne veut pas ignorer :
    # si l'en-tête suggère un type, on abaisse le seuil de confiance requis
    # sur le contenu (utile quand une bonne partie des valeurs sont sales).
    nom_lower = nom_colonne.lower()
    seuil_email = 0.35 if any(k in nom_lower for k in NOM_COLONNE_INDICES["regex_email"]) else seuil_majorite
    seuil_date = 0.35 if any(k in nom_lower for k in NOM_COLONNE_INDICES["date"]) else seuil_majorite
    seuil_number = 0.35 if any(k in nom_lower for k in NOM_COLONNE_INDICES["number"]) else seuil_majorite

    if part_email >= seuil_email:
        return {"type": "regex", "rule": EMAIL_RE.pattern}
    if part_date >= seuil_date:
        return {"type": "date"}
    if part_number >= seuil_number:
        return {"type": "number"}
    return {"type": "string"}


def infer_schema(file_path, sample_size=20):
    """
    Lit les N premières lignes d'un CSV et propose un schema_json.
    Le client pourra ensuite corriger cette proposition dans l'UI avant de
    lancer réellement le découpage en micro-tâches -> évite de lui demander
    de connaître le format JSON attendu.
    """
    with open(file_path, mode="r", encoding="utf-8") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,")
        except csv.Error:
            dialect = csv.excel

        reader = csv.reader(f, dialect)
        header = next(reader)
        colonnes_valeurs = {name: [] for name in header}

        for i, row in enumerate(reader):
            if i >= sample_size:
                break
            for name, cell in zip(header, row):
                colonnes_valeurs[name].append(cell)

    schema = {"columns": []}
    for name in header:
        devine = _deviner_type_colonne(colonnes_valeurs[name], nom_colonne=name)
        valeurs = colonnes_valeurs[name]
        # Une colonne est jugée "required" si aucune valeur de l'échantillon
        # n'est vide -- indicatif seulement, le client garde la main dessus.
        required = all(v.strip() for v in valeurs) if valeurs else False
        schema["columns"].append({"name": name, "required": required, **devine})

    return schema, header, list(colonnes_valeurs.values())


def process_csv_to_microtasks(file_path, project_id, schema_definition,
                               taux_gold=0.08, gold_answers=None):
    """
    Découpe le CSV, génère les alertes, injecte des gold standards et
    prépare le payload prêt pour la table `micro_tasks`.

    gold_answers : dict optionnel {row_id: reponse_attendue} pour les lignes
                   dont TU connais déjà la correction (piège qualité).
    """
    micro_tasks_payload = []
    gold_answers = gold_answers or {}

    with open(file_path, mode="r", encoding="utf-8") as f:
        # Détection automatique du délimiteur (; ou , selon les fichiers clients)
        sample = f.read(2048)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,")
        except csv.Error:
            dialect = csv.excel  # repli sur virgule par défaut

        reader = csv.reader(f, dialect)
        header = next(reader)

        for row_id, row in enumerate(reader):
            ligne_nettoyee, badges = nettoyer_ligne(row, header, schema_definition)

            # Une ligne n'est gold standard QUE si on possède réellement sa
            # réponse attendue -- sinon la comparaison n'a aucun sens et pénalise
            # injustement le worker. `taux_gold` sert à choisir, parmi les lignes
            # déjà propres (0 badge), lesquelles promouvoir en gold standard en
            # utilisant leur propre valeur nettoyée comme réponse de référence.
            # Ce tirage doit se faire AVANT la décision d'auto-validation
            # ci-dessous : c'est ce qui permet de garder un contrôle qualité
            # humain sur un échantillon des lignes propres, plutôt que de
            # toutes les valider sans jamais les faire passer devant un worker.
            is_gold = row_id in gold_answers
            if not is_gold and not badges and random.random() < taux_gold:
                is_gold = True
                gold_answers[row_id] = ligne_nettoyee

            # Cas 1 -- ligne propre APRÈS nettoyage automatique et non tirée
            # au sort comme gold standard : validée sans aucune intervention
            # humaine, le worker ne la verra jamais.
            auto_validee = not badges and not is_gold

            task_data = {
                "project_id": project_id,
                "row_id": row_id,
                "raw_data": ligne_nettoyee,
                "header": header,
                "badges": badges,
                "schema_json": schema_definition,
                "redundancy_level": 1 if is_gold else choisir_redondance(len(badges)),
                "is_gold_standard": is_gold,
                "gold_answer": gold_answers.get(row_id),
                "status": "completed" if auto_validee else "available",
                "resultat_final": ligne_nettoyee if auto_validee else None,
            }
            micro_tasks_payload.append(task_data)

    nb_auto = sum(1 for t in micro_tasks_payload if t["status"] == "completed")
    nb_gold = sum(t["is_gold_standard"] for t in micro_tasks_payload)
    print(f"✅ {len(micro_tasks_payload)} micro-tâches générées "
          f"({nb_auto} validées automatiquement, {nb_gold} gold standards, "
          f"{len(micro_tasks_payload) - nb_auto} pour les workers).")
    return micro_tasks_payload


# ---------------------------------------------------------------------------
# 3. CONSENSUS ENTRE PLUSIEURS SOUMISSIONS (le vrai nerf de la guerre)
# ---------------------------------------------------------------------------

def similarite(a, b):
    """Score de similarité 0-1 entre deux chaînes (Levenshtein-like, natif Python)."""
    return SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio()


def valider_consensus(submissions, schema_definition, header, trust_scores=None,
                       seuil_similarite=0.9, seuil_exclusion_vote=20.0):
    """
    submissions : liste de listes de cellules (une par worker ayant traité la ligne)
    trust_scores : trust_score de chaque worker correspondant, dans le même ordre
                   (utilisé pour PONDÉRER le consensus -- un worker sous
                   `seuil_exclusion_vote` ne pèse plus dans le choix de la valeur
                   retenue, mais reste évalué par rapport à cette valeur).

    Retourne le détail par colonne + `ecarts_par_worker` : pour chaque worker,
    la proportion de colonnes où sa réponse rejoint la majorité pondérée.
    Sert à ajuster le trust_score ensuite (API), PAS à décider du paiement :
    la décision produit est de payer même en cas de désaccord (hors gold
    standard), et de ne sanctionner que la réputation.
    """
    n = len(submissions)
    if n == 1:
        return {"status": "auto_valide", "valeur_retenue": submissions[0],
                "ecarts_par_worker": [1.0]}

    poids = [ts if ts >= seuil_exclusion_vote else 0.0 for ts in (trust_scores or [100.0] * n)]
    if sum(poids) == 0:
        # Sécurité : si TOUT le monde est sous le seuil, on ne veut pas se
        # retrouver sans consensus du tout -> on retombe sur un poids égal.
        poids = [1.0] * n

    col_index_by_name = {name: i for i, name in enumerate(header)}
    resultat_par_colonne = {}
    ecarts_matrice = [[] for _ in range(n)]  # ecarts_matrice[i] = liste de bool par colonne

    for col in schema_definition["columns"]:
        idx = col_index_by_name.get(col["name"])
        if idx is None:
            continue
        valeurs = [sub[idx] if idx < len(sub) else "" for sub in submissions]

        if col.get("type") in ("regex", "date"):
            normalisees = [v.strip().lower() for v in valeurs]
            poids_par_valeur = {}
            for v, w in zip(normalisees, poids):
                poids_par_valeur[v] = poids_par_valeur.get(v, 0.0) + w
            valeur_majoritaire, poids_gagnant = max(poids_par_valeur.items(), key=lambda kv: kv[1])
            part = poids_gagnant / sum(poids)
            for i, v in enumerate(normalisees):
                ecarts_matrice[i].append(v == valeur_majoritaire)
            resultat_par_colonne[col["name"]] = (
                {"status": "accord", "valeur": valeurs[normalisees.index(valeur_majoritaire)]}
                if part > 0.5 else {"status": "litige", "candidats": valeurs}
            )
        else:
            # Texte libre : chaque candidat "reçoit" le poids de tous ceux qui
            # lui sont similaires -> le candidat le plus soutenu (pondéré par
            # la confiance de ses soutiens) devient la référence.
            scores_pond = [
                sum(poids[j] for j in range(n) if j != i and similarite(valeurs[i], valeurs[j]) >= seuil_similarite)
                + poids[i]
                for i in range(n)
            ]
            i_gagnant = max(range(n), key=lambda i: scores_pond[i])
            valeur_majoritaire = re.sub(r"\s+", " ", valeurs[i_gagnant]).strip()
            part = scores_pond[i_gagnant] / sum(poids)
            for i, v in enumerate(valeurs):
                ecarts_matrice[i].append(similarite(v, valeurs[i_gagnant]) >= seuil_similarite)
            resultat_par_colonne[col["name"]] = (
                {"status": "accord", "valeur": valeur_majoritaire}
                if part > 0.5 else {"status": "litige", "candidats": valeurs}
            )

    ecarts_par_worker = [
        (sum(cols) / len(cols) if cols else 1.0) for cols in ecarts_matrice
    ]
    a_des_litiges = any(v["status"] == "litige" for v in resultat_par_colonne.values())
    return {
        "status": "litige_partiel" if a_des_litiges else "valide",
        "detail": resultat_par_colonne,
        "ecarts_par_worker": ecarts_par_worker,
    }
