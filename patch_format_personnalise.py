"""
Patch : remplace l'option "Email" figée par un vrai champ "Format
personnalisé (motif)" dans l'écran de dépôt client -- pour pouvoir valider
n'importe quel type de colonne d'entreprise (référence produit, SIRET, code
interne...), pas seulement les champs email/téléphone/date déjà prévus.

Lance-le à la racine du projet :
    python3 patch_format_personnalise.py
"""


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


patch("client_upload.html", [
    (
        "function construireAuthHeader(email, motdepasse) { return 'Basic ' + btoa(email + ':' + motdepasse); }",
        "function construireAuthHeader(email, motdepasse) { return 'Basic ' + btoa(email + ':' + motdepasse); }\n"
        "\n"
        "function escapeHtml(s) { return String(s).replace(/[&<>\"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c])); }",
    ),
    (
        "    schemaCourant.columns.forEach((col, i) => {\n"
        "      const tr = document.createElement('tr');\n"
        "      tr.innerHTML = `\n"
        "        <td>${col.name}</td>\n"
        "        <td><select onchange=\"modifierType(${i}, this.value)\">\n"
        "          <option value=\"string\" ${col.type==='string'?'selected':''}>Texte libre</option>\n"
        "          <option value=\"nom\" ${col.type==='nom'?'selected':''}>Nom / prénom (capitalisation auto)</option>\n"
        "          <option value=\"regex\" ${col.type==='regex'?'selected':''}>Email</option>\n"
        "          <option value=\"telephone\" ${col.type==='telephone'?'selected':''}>Téléphone (normalisé +33...)</option>\n"
        "          <option value=\"date\" ${col.type==='date'?'selected':''}>Date (AAAA-MM-JJ)</option>\n"
        "          <option value=\"number\" ${col.type==='number'?'selected':''}>Nombre</option>\n"
        "        </select></td>\n"
        "        <td><input type=\"checkbox\" ${col.required?'checked':''} onchange=\"schemaCourant.columns[${i}].required = this.checked\"></td>\n"
        "        <td><input type=\"checkbox\" ${col.sensible?'checked':''} onchange=\"schemaCourant.columns[${i}].sensible = this.checked\" title=\"Jamais montrée au worker, corrigée automatiquement seulement (espaces)\"></td>\n"
        "      `;\n"
        "      table.appendChild(tr);\n"
        "    });",
        "    schemaCourant.columns.forEach((col, i) => {\n"
        "      const tr = document.createElement('tr');\n"
        "      tr.innerHTML = `\n"
        "        <td>${col.name}</td>\n"
        "        <td>\n"
        "          <select onchange=\"modifierType(${i}, this.value)\">\n"
        "            <option value=\"string\" ${col.type==='string'?'selected':''}>Texte libre</option>\n"
        "            <option value=\"nom\" ${col.type==='nom'?'selected':''}>Nom / prénom (capitalisation auto)</option>\n"
        "            <option value=\"regex\" ${col.type==='regex'?'selected':''}>Format personnalisé (motif)</option>\n"
        "            <option value=\"telephone\" ${col.type==='telephone'?'selected':''}>Téléphone (normalisé +33...)</option>\n"
        "            <option value=\"date\" ${col.type==='date'?'selected':''}>Date (AAAA-MM-JJ)</option>\n"
        "            <option value=\"number\" ${col.type==='number'?'selected':''}>Nombre</option>\n"
        "          </select>\n"
        "          <div class=\"bloc-regex\" id=\"regex_${i}\" style=\"${col.type==='regex' ? '' : 'display:none;'} margin-top:6px;\">\n"
        "            <select onchange=\"appliquerPresetRegex(${i}, this.value)\" style=\"margin-bottom:4px; font-size:12px;\">\n"
        "              <option value=\"\">-- modèle courant --</option>\n"
        "              <option value=\"email\">Email</option>\n"
        "              <option value=\"cp_fr\">Code postal (France, 5 chiffres)</option>\n"
        "              <option value=\"tel_intl\">Téléphone international (E.164)</option>\n"
        "              <option value=\"ref_alphanum\">Référence produit / SKU (lettres + chiffres)</option>\n"
        "              <option value=\"entier\">Nombre entier uniquement</option>\n"
        "              <option value=\"siret\">SIRET (14 chiffres)</option>\n"
        "            </select>\n"
        "            <input type=\"text\" value=\"${escapeHtml(col.rule || '')}\" placeholder=\"Ton propre motif (regex), ex: ^[A-Z]{2}\\\\d{4}$\"\n"
        "                   oninput=\"schemaCourant.columns[${i}].rule = this.value\" style=\"font-family: var(--mono); font-size:12.5px;\">\n"
        "            <div style=\"font-size:11px; color: var(--text-tertiary); margin-top:2px;\">\n"
        "              Sert pour n'importe quelle colonne au format que tu connais (référence interne, code, identifiant...) --\n"
        "              même si aucun type prédéfini ne correspond à tes données.\n"
        "            </div>\n"
        "          </div>\n"
        "        </td>\n"
        "        <td><input type=\"checkbox\" ${col.required?'checked':''} onchange=\"schemaCourant.columns[${i}].required = this.checked\"></td>\n"
        "        <td><input type=\"checkbox\" ${col.sensible?'checked':''} onchange=\"schemaCourant.columns[${i}].sensible = this.checked\" title=\"Jamais montrée au worker, corrigée automatiquement seulement (espaces)\"></td>\n"
        "      `;\n"
        "      table.appendChild(tr);\n"
        "    });",
    ),
    (
        "function modifierType(idx, valeur) {\n"
        "  const col = schemaCourant.columns[idx];\n"
        "  col.type = valeur;\n"
        "  if (valeur === 'regex') col.rule = '^[\\\\w.-]+@[\\\\w.-]+\\\\.\\\\w+$';\n"
        "  else delete col.rule;\n"
        "}",
        "function modifierType(idx, valeur) {\n"
        "  const col = schemaCourant.columns[idx];\n"
        "  col.type = valeur;\n"
        "  if (valeur === 'regex') {\n"
        "    if (!col.rule) col.rule = '^[\\\\w.-]+@[\\\\w.-]+\\\\.\\\\w+$';\n"
        "    document.getElementById(`regex_${idx}`).style.display = '';\n"
        "  } else {\n"
        "    delete col.rule;\n"
        "    const bloc = document.getElementById(`regex_${idx}`);\n"
        "    if (bloc) bloc.style.display = 'none';\n"
        "  }\n"
        "}\n"
        "\n"
        "const PRESETS_REGEX = {\n"
        "  email: '^[\\\\w.-]+@[\\\\w.-]+\\\\.\\\\w+$',\n"
        "  cp_fr: '^\\\\d{5}$',\n"
        "  tel_intl: '^\\\\+[1-9]\\\\d{7,14}$',\n"
        "  ref_alphanum: '^[A-Za-z0-9_-]+$',\n"
        "  entier: '^-?\\\\d+$',\n"
        "  siret: '^\\\\d{14}$',\n"
        "};\n"
        "\n"
        "function appliquerPresetRegex(idx, cle) {\n"
        "  if (!cle) return;\n"
        "  const col = schemaCourant.columns[idx];\n"
        "  col.rule = PRESETS_REGEX[cle];\n"
        "  const input = document.querySelector(`#regex_${idx} input[type=\"text\"]`);\n"
        "  if (input) input.value = col.rule;\n"
        "}",
    ),
], "client_upload.html")

print("\nTerminé.")
