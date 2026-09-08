# ATLAS Collect — Specification

## Objectif

Permettre aux agents ATLAS de collecter des données d'exécution pour améliorer leur compétence au fil du temps, dans le strict respect du RGPD et avec consentement explicite selon le contexte d'utilisation.

---

## Trois modes de collecte

| Mode | Contexte | Base légale | Ce qui est collecté |
|---|---|---|---|
| `off` | Défaut — collaborateur Capgemini, usage standalone | — | Rien |
| `internal` | Architecte/dev sur ses propres projets | Intérêt légitime | Métriques d'exécution uniquement |
| `client` | Projet client avec accord contractuel B2B | Contrat signé avec clause de collecte | Métriques + outputs anonymisés |

---

## Activation

La collecte est contrôlée par une variable d'environnement :

```bash
# Aucune collecte (défaut)
ATLAS_COLLECT=off

# Métriques uniquement — projets internes
ATLAS_COLLECT=internal

# Métriques + outputs anonymisés — projet client B2B
ATLAS_COLLECT=client
```

En l'absence de la variable, le comportement est identique à `off`. Aucune collecte ne se produit sans activation explicite.

---

## Ce qui est collecté

### Mode `internal` et `client` — métriques d'exécution

- Timestamp UTC de l'exécution
- Nom et version de l'agent
- Durée d'exécution (secondes)
- Statut : `success` | `failure`
- Type d'erreur (catégorie courte, ex: `missing_dependency`, `llm_timeout`)
- Message d'erreur (sans stack trace, sans données utilisateur)
- Versions des dépendances clés

### Mode `client` uniquement — outputs validés

- Outputs produits par l'agent après validation interne (ex : paires validées par pair-forge)
- **Anonymisés avant tout stockage** — voir section Anonymisation
- Jamais les inputs (contenu des fichiers traités)

---

## Ce qui n'est jamais collecté

- Contenu des fichiers ou documents traités par les agents
- Données personnelles identifiables (noms, emails, identifiants)
- Stack traces complètes (peuvent contenir des chemins système ou des données)
- Outputs bruts non anonymisés
- Toute donnée sans base légale établie

---

## Anonymisation

Avant tout envoi en mode `client`, les champs suivants sont supprimés des outputs :

```
email, name, user, author, source, file, path, url
```

Cette liste est extensible dans `collect.py` selon les besoins du projet.

---

## Base légale par mode

### Mode `internal`
Intérêt légitime du responsable de traitement (Art. 6.1.f RGPD) — amélioration des outils internes, aucune donnée tiers impliquée.

### Mode `client`
Contrat B2B avec clause de collecte explicite (Art. 6.1.b RGPD). La clause doit stipuler :
- La nature des données collectées (métriques + outputs anonymisés)
- La finalité (amélioration des agents ATLAS)
- La durée de conservation
- Les droits du client sur ses données

Le mode `client` ne doit être activé qu'après signature du contrat incluant cette clause.

---

## Architecture technique

Chaque agent ATLAS embarque un module `collect.py` indépendant (comme `docker.py` et `install.py`). Ce module est autonome — il ne dépend d'aucune infrastructure externe et est silencieux si `ATLAS_COLLECT` est absent ou vaut `off`.

Interface actuelle : log structuré JSON via le logger Python `atlas.collect`.
Interface future : branchable sur le RAG+KG central via `ATLAS_BUS_URL` quand ce composant sera disponible.

---

## Évolution

Ce document est la référence contractuelle et technique pour toute implémentation de collecte dans ATLAS. Toute modification de la liste des champs collectés ou des modes disponibles doit être reflétée ici avant d'être codée.
