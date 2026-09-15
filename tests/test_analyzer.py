"""Tests unitaires du moteur d'analyse (analyzer.py) -- sans base de données."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyzer import (choisir_redondance, detect_anomalies, infer_schema,
                       similarite, valider_consensus)

SCHEMA_TEST = {
    "columns": [
        {"name": "nom", "type": "string", "required": True},
        {"name": "email", "type": "regex", "rule": r"^[\w.-]+@[\w.-]+\.\w+$", "required": True},
        {"name": "date_naiss", "type": "date", "required": True},
    ]
}
HEADER_TEST = ["nom", "email", "date_naiss"]


def test_detect_anomalies_ligne_propre():
    row = ["Jean Dupont", "jdupont@gmail.com", "1998-04-03"]
    assert detect_anomalies(row, HEADER_TEST, SCHEMA_TEST) == []


def test_detect_anomalies_email_invalide():
    row = ["Jean Dupont", "jdupont@@gmail.com", "1998-04-03"]
    badges = detect_anomalies(row, HEADER_TEST, SCHEMA_TEST)
    assert any(b["type"] == "danger" and b["col_index"] == 1 for b in badges)


def test_detect_anomalies_date_ambigue():
    row = ["Jean Dupont", "jdupont@gmail.com", "03/04/1998"]
    badges = detect_anomalies(row, HEADER_TEST, SCHEMA_TEST)
    assert any("date" in b["msg"].lower() for b in badges)


def test_detect_anomalies_champ_vide():
    row = ["Jean Dupont", "jdupont@gmail.com", "n/a"]
    badges = detect_anomalies(row, HEADER_TEST, SCHEMA_TEST)
    assert any(b["type"] == "info" for b in badges)


def test_choisir_redondance_scale_avec_le_nombre_de_badges():
    assert choisir_redondance(0) == 1
    assert choisir_redondance(1) == 2
    assert choisir_redondance(2) == 3


def test_infer_schema_detecte_email_malgre_donnees_sales(tmp_path):
    csv_content = (
        "nom;email;date_naiss\n"
        "Jean Dupont;jdupont@gmail.com;1998-04-03\n"
        " Marie   Curie;marie.curie@@ens.fr;03/04/1998\n"
        "Paul Martin;paul.martin;n/a\n"
        "Sophie Legrand ;sophie@legrand.fr;1990-11-30\n"
    )
    fichier = tmp_path / "test.csv"
    fichier.write_text(csv_content, encoding="utf-8")

    schema, header, _ = infer_schema(str(fichier))
    types_par_nom = {c["name"]: c["type"] for c in schema["columns"]}

    assert types_par_nom["email"] == "regex"
    assert types_par_nom["date_naiss"] == "date"
    assert header == ["nom", "email", "date_naiss"]


def test_valider_consensus_une_seule_soumission():
    res = valider_consensus([["Jean Dupont", "jdupont@gmail.com", "1998-04-03"]], SCHEMA_TEST, HEADER_TEST)
    assert res["status"] == "auto_valide"


def test_valider_consensus_accord_normalise_espaces():
    subs = [
        ["Marie Curie", "marie.curie@ens.fr", "1998-04-03"],
        ["Marie  Curie", "marie.curie@ens.fr", "1998-04-03"],  # double espace
    ]
    res = valider_consensus(subs, SCHEMA_TEST, HEADER_TEST)
    assert res["status"] == "valide"
    assert res["detail"]["nom"]["valeur"] == "Marie Curie"  # normalisé, pas le double espace


def test_valider_consensus_pondere_exclut_worker_peu_fiable():
    subs = [["Jean Dupont", "jdupont@gmail.com", "1998-04-03"]] * 2 + [
        ["Paul Martin", "autre@x.fr", "2000-01-01"]
    ]
    trust_scores = [90, 85, 15]  # le 3e est sous le seuil d'exclusion (20)
    res = valider_consensus(subs, SCHEMA_TEST, HEADER_TEST, trust_scores=trust_scores)
    assert res["status"] == "valide"  # les 2 workers fiables l'emportent
    assert res["ecarts_par_worker"][2] == 0.0  # le 3e est mesuré en désaccord total


def test_similarite_detecte_quasi_identite():
    assert similarite("Jean Dupont", "jean dupont") > 0.95
    assert similarite("Jean Dupont", "Paul Martin") < 0.5
