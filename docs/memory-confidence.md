# Atlas Memory — Base vivante avec confiance évolutive

**Auteur :** ATLAS Core Team  
**Statut :** Approuvé — en cours d'implémentation  
**Composant :** `platform/atlas-memory`

---

## Problème à résoudre

Aujourd'hui, la base mémoire d'ATLAS stocke des expériences **sans qualifier leur fiabilité**.

Un agent scaffoldé avec un score juge de 2.0 (mauvais) et un agent avec un score de 5.0 (excellent) ont exactement le même poids dans la base. Quand ATLAS cherche un template pour construire un nouvel agent, il peut tomber sur un mauvais exemple et croire que c'est du gold.

C'est comme un cerveau qui ne distinguerait pas un souvenir flou d'un souvenir précis.

---

## Principe fondamental

> **On ne supprime jamais. On qualifie.**

Chaque entrée en base reste pour toujours — elle porte l'histoire. Ce qui change, c'est sa **confiance** : un score entre 0.0 et 1.0 qui dit à ATLAS "dans quelle mesure tu peux te fier à cette information".

### Analogie cerveau humain

```
Cerveau :  "Je me souviens qu'un agent de classification avait un prompt avec TODO."
           "Confiance : 0.2 — je sais que c'était une tentative ratée."
           "Je me souviens aussi d'un agent similaire qui marchait bien."
           "Confiance : 0.9 — validé par le juge et testé."
           "Je privilégie le second pour répondre à ta demande."

KG :       "CategoryClassifier est lié à 'classification tickets', à 'confidence_score',
            à 'CDC support telecom'. Ces connexions se sont renforcées à chaque run réussi."
```

---

## Architecture : deux couches de mémoire

### RAG (Retrieval-Augmented Generation) — La mémoire épisodique

Stocke des **expériences concrètes** : du code généré, des évaluations de juge, des traces de pipeline.

Technologie : Qdrant (base vectorielle locale, airGap-compatible).

Chaque expérience indexée contient :
```json
{
  "entry_type": "scaffold",
  "content": "Agent CategoryClassifier — classifie des tickets support...",
  "payload": {
    "agent_name": "CategoryClassifier",
    "run_id": "42b39d07-...",
    "judge_score": 5.0
  },
  "confidence": 0.95,
  "timestamp": "2026-09-22T09:00:00Z"
}
```

**Avant (aujourd'hui) :** la recherche retourne les résultats triés uniquement par similarité cosine (à quel point le texte ressemble à la requête).

**Après :** la recherche trie par `score_combiné = similarité × confiance`. Un excellent scaffold avec confidence 0.95 remonte devant un scaffold médiocre avec confidence 0.2, même si ce dernier ressemble davantage textuellement à la requête.

### KG (Knowledge Graph) — La mémoire sémantique

Stocke les **relations entre concepts** : quel agent produit quoi, qui enchaîne avec qui, quelles capacités sont maîtrisées.

Technologie : NetworkX (graphe dirigé, persisté en JSON).

Chaque nœud agent contient maintenant :
```json
{
  "name": "CategoryClassifier",
  "node_type": "agent",
  "capabilities": ["classification", "support-tickets"],
  "inputs": ["ticket_text", "ticket_id"],
  "outputs": ["priority", "category", "confidence", "justification"],
  "judge_score": 5.0,
  "confidence": 1.0,
  "score_history": [
    {"score": 1.0, "run_id": "abc...", "timestamp": "2026-09-21T10:00:00Z"},
    {"score": 3.5, "run_id": "def...", "timestamp": "2026-09-21T14:00:00Z"},
    {"score": 5.0, "run_id": "42b...", "timestamp": "2026-09-21T16:00:00Z"}
  ],
  "coaching_history": [
    {"patch": "Remplacer TODO dans MAIN_PROMPT par instructions complètes", "criterion": "output_format", "delta": 2.5}
  ]
}
```

Le `score_history` est l'équivalent de "je me souviens de mes tentatives successives, et je sais dans quelle mesure j'ai progressé".

---

## Les nouvelles opérations

### `update_confidence` — Affiner la fiabilité d'une expérience RAG

```python
# Quand un agent est re-scaffoldé avec un meilleur score,
# l'ancienne expérience perd de la confiance
memory.run(
    operation="update_confidence",
    point_id="uuid-de-l-ancienne-experience",
    confidence=0.3  # était 0.6 avant, maintenant supplanté par mieux
)
```

### `invalidate` — Marquer comme dépassé (sans supprimer)

```python
# L'utilisateur supprime un run de test
# Les expériences restent en base mais ne remontent plus dans les recherches
memory.run(
    operation="invalidate",
    point_id="uuid-du-mauvais-scaffold",
    invalidated_by="user_delete"  # ou "run_id_meilleur"
)
# Résultat : confidence = 0.0, invalidated_by = "user_delete", entrée toujours présente
```

### `update_agent_score` — Mettre à jour le score KG

```python
# Après chaque scaffold réussi ou install
memory.run(
    operation="update_agent_score",
    name="CategoryClassifier",
    judge_score=5.0,
    run_id="42b39d07-..."
)
# Résultat : score_history += nouvelle entrée, confidence = 5.0/5.0 = 1.0
```

---

## Cycle de vie complet d'un agent dans la base

```
1. Premier scaffold — score 1.0
   → RAG indexe l'expérience avec confidence = 0.20
   → KG enregistre le nœud avec judge_score = 1.0, confidence = 0.20

2. Deuxième scaffold (re-scaffold) — score 3.5
   → RAG : confidence de l'expérience précédente → 0.30 (dépassée mais conservée)
   → RAG indexe la nouvelle expérience avec confidence = 0.70
   → KG : score_history += {3.5}, confidence = 0.70

3. Troisième scaffold — score 5.0
   → RAG : confidence de l'expérience précédente → 0.35 (dépassée)
   → RAG indexe avec confidence = 1.0
   → KG : score_history += {5.0}, confidence = 1.0

4. Installation
   → KG marque l'agent comme "installed: true"
   → Les connexions KG se renforcent (CHAINS_WITH, PRODUCES, etc.)

5. Si suppression du run (test)
   → RAG : toutes les expériences du run → confidence = 0.0, invalidated_by = "user_delete"
   → KG : nœud non touché si agent installé, sinon marqué confidence = 0.0
```

---

## Impact sur la recherche RAG

### Avant

```
Requête : "classifier tickets support français"

Résultats (triés par similarité cosine) :
  1. scaffold CategoryClassifier — run abc — score cosine 0.92 ← mauvais scaffold score 1.0
  2. scaffold PriorityClassifier — run 42b — score cosine 0.88 ← excellent scaffold score 5.0
  3. scaffold CategoryClassifier — run def — score cosine 0.85 ← moyen scaffold score 3.5
```

### Après

```
Requête : "classifier tickets support français"

score_combiné = cosine × confidence

Résultats :
  1. scaffold PriorityClassifier — run 42b  — cosine 0.88 × confidence 1.00 = 0.88 ✓ excellent
  2. scaffold CategoryClassifier — run def  — cosine 0.85 × confidence 0.35 = 0.30
  3. scaffold CategoryClassifier — run abc  — cosine 0.92 × confidence 0.00 = 0.00 ✗ invalidé
```

Le bon exemple remonte même s'il est légèrement moins similaire textuellement.

---

## Suppression d'un run — niveau 1 (implémenté maintenant)

Endpoint : `DELETE /api/runs/{run_id}`

Ce qui se passe :
1. Suppression du fichier `~/.atlas/runs/{run_id}.json`
2. Suppression des fichiers scaffoldés sur disque (`output_path` dans le run JSON)
3. Invalidation des entrées RAG liées à ce run (`confidence = 0.0`)
4. Les nœuds KG **déjà installés** ne sont pas touchés

Ce qui ne se passe pas :
- Aucune suppression dans Qdrant ni dans `graph.json`
- L'historique reste lisible pour l'audit

---

## Fichiers modifiés

| Fichier | Changement |
|---|---|
| `platform/atlas-memory/atlas_memory/rag.py` | `confidence` dans payload, `update_confidence()`, `invalidate()`, tri combiné |
| `platform/atlas-memory/atlas_memory/kg.py` | `judge_score`/`confidence` sur nœuds, `update_agent_score()`, `score_history` |
| `platform/atlas-memory/atlas_memory/agent.py` | 3 nouvelles opérations dans le dispatch |
| `platform/atlas-memory/atlas_memory/tests/smoke/test_memory.py` | Tests pour les nouvelles opérations |
| `tools/atlas-dashboard/atlas_dashboard/app.py` | Passer `confidence` à l'install, `DELETE /api/runs/{run_id}` |
| `tools/atlas-dashboard/atlas_dashboard/templates/index.html` | Bouton "Supprimer ce run" avec confirmation |
