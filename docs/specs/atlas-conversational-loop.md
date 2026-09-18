# ATLAS — Boucle conversationnelle client

**Statut** : spécification approuvée — en attente d'implémentation  
**Date** : 2026-09-18  
**Scope** : cdc-router, atlas-judge, api_scaffold, run_store, dashboard HITL

---

## 1. Contexte et vision

### Problème actuel

ATLAS est aujourd'hui un pipeline batch silencieux. Il ingère un CDC, produit des agents, rate des critères, et le client découverte les erreurs après coup dans le dashboard. La boucle de correction est réactive : échouer → diagnostiquer → corriger → réessayer.

Trois types de problèmes se mélangent dans cette boucle sans être distingués :

- **Erreurs IA** : le modèle a mal exécuté une instruction explicite du CDC
- **Erreurs code** : logique déterministe incorrecte, testable unitairement
- **Manques de spec** : le CDC ne contenait pas assez d'information — seul le client peut décider

Ces trois causes appellent trois réponses différentes. Les traiter de la même manière crée du bruit, des boucles infinies, et une expérience client dégradée.

### Vision

ATLAS est proactif. Avant de produire le moindre agent, il détecte les ambiguïtés et les manques dans le CDC et pose des questions précises au client. Pendant la construction, si une décision appartient au client, le scaffold se suspend et demande plutôt que de deviner et rater. Chaque résolution — qu'elle vienne de l'IA ou du client — est indexée dans le RAG et le KG pour que les mêmes blocages n'apparaissent plus sur les projets suivants.

### Alignement avec l'état de l'art

- **MetaGPT** : phase de clarification pré-génération (Product Manager role)
- **LangGraph** : interrupt() pour suspension dynamique mid-pipeline
- **Gap comblé** : classification automatique de la cause racine avant routing — n'existe dans aucun framework actuel

---

## 2. Les trois piliers

### Pilier 1 — CDC Intake conversationnel (pass 0)

Avant tout scaffold, cdc-router exécute un pass 0 qui analyse le CDC pour détecter :

| Type | Définition | Exemple |
|---|---|---|
| **Flou** | Instruction présente mais non actionnable | "gérer les cas limites" |
| **Manque** | Information nécessaire absente | langue de sortie non spécifiée |
| **Incohérence** | Deux blocs se contredisent | critère A suppose une donnée que le bloc B ne fournit pas |

Résultat : maximum 3 questions priorisées posées au client avant le premier scaffold. Les réponses sont stockées comme contraintes permanentes au niveau projet, pas au niveau run.

**Règle** : une question déjà posée et répondue sur un type de CDC similaire ne doit jamais être re-posée (voir section 5 — KG).

### Pilier 2 — Classification des causes d'échec

Pour chaque critère avec score < 3, le judge ajoute un champ `cause` :

| Cause | Définition | Routing |
|---|---|---|
| `model_error` | L'IA a mal exécuté une instruction explicite du CDC | Boucle scaffold → improvement-advisor |
| `code_error` | Logique déterministe incorrecte (calcul, parsing, regex) | Suggestion de fix déterministe |
| `missing_spec` | Le CDC ne précisait pas ce comportement | Suspension → question client |

**Algorithme de classification** (dans le prompt atlas-judge) :

```
1. Le CDC contient-il une instruction explicite sur ce critère ?
   → oui  →  model_error
   → non  →  missing_spec

2. L'échec concerne-t-il une transformation déterministe ?
   → oui  →  code_error (prioritaire sur model_error)
```

**Règle de sécurité** : en cas de doute entre `model_error` et `missing_spec`, choisir `missing_spec`. Mieux vaut poser une question inutile une fois que de boucler indéfiniment.

### Pilier 3 — Routing des causes

```
model_error  →  boucle scaffold (cycles 1+2 avec improvement-advisor)
code_error   →  suggestion de fix déterministe, pas de re-scaffold complet
missing_spec →  suspension → question client → réponse stockée dans JSON consolidé
               → réponse injectée dans CDC enrichi → nouveau scaffold
```

Les trois causes alimentent le RAG et le KG à chaque résolution (voir section 5).

---

## 3. Architecture des composants modifiés

### cdc-router

**Avant** : 3 passes (segmentation, classification, cohérence)  
**Après** : pass 0 ajouté en tête

```
Pass 0 : détection des flous/manques/incohérences → questions client
Pass 1 : segmentation fonctionnelle
Pass 2 : classification ia/tool/code + judge iterations
Pass 3 : validation cohérence architecturale
```

Pass 0 est ignoré si le CDC est déjà annoté avec des `client_constraints` (projet existant).

### atlas-judge

**Avant** : retourne `score`, `criteria_scores`, `feedback`  
**Après** : chaque critère échoué inclut un champ `cause`

```json
{
  "criterion": "Consistency across French and English inputs",
  "score": 2,
  "comment": "L'agent retourne des labels anglais au lieu des labels français requis.",
  "cause": "missing_spec"
}
```

### api_scaffold (atlas-dashboard)

**Avant** : cycle 1 (3 tentatives) → improvement-advisor → diagnose  
**Après** :

```
Cycle 1 (3 tentatives)
  → si missing_spec détectée : suspension → waiting_for_client
  → si model_error/code_error uniquement : improvement-advisor → cycle 2
  → si cycle 2 échoue : diagnose
```

### run_store

Nouveaux champs dans le run :

```json
{
  "status": "waiting_for_client",
  "pending_questions": [...],
  "client_answers": {},
  "agent_state": {
    "PriorityClassifier": {
      "consolidated_context": {...},
      "attempts_log": [...]
    }
  }
}
```

### dashboard HITL

Nouveau badge `waiting` dans la liste des runs.  
Nouvelle vue "Décisions en attente" (voir section 7).

---

## 4. Flux complet avec états

```
CDC ingéré
    │
    ▼
[Pass 0 — détection flous]
    │
    ├─ questions générées ──► waiting_for_client ──► client répond
    │                                                      │
    │◄─────────────────────────────────────────────────────┘
    │
    ▼
[Passes 1-2-3 — routing]
    │
    ▼
[Scaffold cycle 1 — 3 tentatives]
    │
    ├─ judge_passed ──────────────────────────────► approved (HITL)
    │
    ├─ missing_spec détectée ─────────────────────► waiting_for_client
    │                                                      │
    │◄─────────────────────────────────────────────────────┘
    │
    ├─ model_error/code_error uniquement
    │       │
    │       ▼
    │  [improvement-advisor]
    │       │
    │       ▼
    │  [Scaffold cycle 2 — 3 tentatives]
    │       │
    │       ├─ judge_passed ──────────────────────► approved (HITL)
    │       │
    │       └─ échec ─────────────────────────────► diagnose → HITL manuel
    │
    └─ no advisor data ──────────────────────────► diagnose → HITL manuel
```

---

## 5. Schémas de données

### JSON consolidé par agent (anti-accumulation de contexte)

Maintenu dans `run["agent_state"][agent_name]["consolidated_context"]`.  
Mis à jour à chaque cycle, jamais concaténé.

```json
{
  "agent_name": "PriorityClassifier",
  "client_constraints": {
    "output_language": "fr",
    "priority_labels": ["critique", "haute", "normale", "basse"],
    "empty_ticket_behavior": "classify_as_low"
  },
  "resolved_errors": [
    {
      "criterion": "output format",
      "fix": "retourner un objet JSON avec champ 'priority' et 'confidence_score'"
    }
  ],
  "open_issues": [
    {
      "criterion": "edge cases multilingues",
      "cause": "missing_spec",
      "awaiting_client": true,
      "question_id": "q_003"
    }
  ],
  "failed_criteria_history": {
    "Consistency across French and English inputs": {
      "occurrences": 3,
      "last_score": 2,
      "last_cause": "missing_spec"
    }
  }
}
```

Le scaffold reçoit ce JSON comme contexte structuré, pas les logs bruts des tentatives précédentes.

### pending_questions

```json
[
  {
    "id": "q_001",
    "agent": "PriorityClassifier",
    "criterion": "Consistency across French and English inputs",
    "question": "Quels labels de priorité le système doit-il retourner pour les 4 niveaux ?",
    "question_type": "text",
    "hint": "ex: critique / haute / normale / basse",
    "cause": "missing_spec",
    "created_at": "2026-09-18T09:00:00"
  }
]
```

`question_type` : `text`, `single_choice`, `multiple_choice`  
Le LLM qui génère la question choisit le type selon le critère concerné.

### RAG — entrées indexées

| Événement | entry_type | Contenu |
|---|---|---|
| `missing_spec` résolue | `client_decision` | question + réponse + critère + type de CDC |
| `model_error` corrigée | `scaffold_fix` | critère + cause + correction appliquée |
| `code_error` résolue | `deterministic_fix` | pattern d'erreur + correction |

### KG — edges créés

```
PriorityClassifier  --FAILED_ON-->       "output_language"
"output_language"   --RESOLVED_BY-->     "client_answer:fr_labels"
CDC_type:SaaS       --REQUIRES-->        "output_language_spec"
```

Avec le temps, cdc-router lit le KG au pass 0 et sait déjà que `CDC_type:SaaS` nécessite une spécification de langue — sans attendre l'échec.

---

## 6. Spécification des nouvelles fonctions clés

### `_detect_cdc_gaps(cdc_text, kg_agents, llm_fn, llm_cfg) → list[dict]`

Appelée dans cdc-router pass 0.  
Retourne une liste de gaps détectés avec leur type (`vague`, `missing`, `incoherent`) et le bloc concerné.

### `_generate_questions(gaps, cdc_text, llm_fn, llm_cfg) → list[dict]`

Transforme les gaps en questions structurées avec `question_type` et `hint`.  
Maximum 3 questions, priorisées par impact sur le scaffold.  
Filtre les questions déjà répondues dans le KG pour ce type de CDC.

### `_classify_cause(criterion, score, cdc_text, llm_fn, llm_cfg) → str`

Appelée dans atlas-judge pour chaque critère avec score < 3.  
Retourne `"model_error"`, `"code_error"`, ou `"missing_spec"`.

### `_build_consolidated_context(agent_name, attempts_log, client_answers) → dict`

Construit le JSON consolidé à partir des tentatives précédentes et des réponses client.  
Remplace la concaténation de `cr_context`.

### `store.set_pending_questions(run_id, agent_name, questions) → None`

Écrit les questions dans `run["pending_questions"]` et passe le statut à `waiting_for_client`.

### `POST /api/runs/{run_id}/answer`

```python
body: { "answers": {"q_001": "critique/haute/normale/basse"} }
```

1. Écrit les réponses dans `run["client_answers"]`
2. Met à jour le JSON consolidé de l'agent
3. Indexe la décision dans le RAG
4. Remet le statut à `scaffolding`
5. Relance le scaffold avec le contexte enrichi

---

## 7. Contraintes de design UX

### Principe directeur

ATLAS ne pose jamais une question qui révèle son ignorance. Il pose des questions qui démontrent sa compréhension du projet et sa rigueur.

### Reformulation systématique

| À éviter | À utiliser |
|---|---|
| "Information manquante : langue de sortie" | "Pour garantir la cohérence avec vos conventions métier, confirmez les labels attendus" |
| "Le CDC ne précise pas les edge cases" | "Votre système rencontrera des tickets vides — comment souhaitez-vous qu'ils soient traités ?" |
| "missing_spec détectée sur le critère X" | "Avant de finaliser cet agent, une décision métier est nécessaire sur ce point" |
| "waiting_for_client" (badge) | "En attente de validation" |

### Maquette de la vue "Décisions en attente"

```
─────────────────────────────────────────────────────
  PriorityClassifier — Décisions en attente  (2)

  Avant de finaliser cet agent, 2 points
  nécessitent votre validation métier.

  ① Labels de priorité
     L'agent classifie les tickets en 4 niveaux.
     Quels termes votre équipe utilise-t-elle ?

     [ critique / haute / normale / basse     ]
     ex: critique / haute / normale / basse

  ② Tickets sans contenu
     Comment traiter un ticket vide ou illisible ?
     ○ Classer en priorité basse par défaut
     ○ Rejeter avec message d'erreur explicite
     ○ Escalader vers un opérateur humain

  [ Valider et continuer ]
─────────────────────────────────────────────────────
```

### Statuts visibles dans le dashboard

| Statut interne | Affiché client |
|---|---|
| `waiting_for_client` | En attente de validation |
| `scaffolding` | Génération en cours |
| `model_error` loop | Optimisation en cours |
| `missing_spec` detected | Décision requise |

---

## 8. Ordre d'implémentation recommandé

Les composants sont dépendants dans cet ordre — ne pas inverser.

**Étape 1 — Classification judge** (`cause` par critère)
- Modifier le prompt atlas-judge (pointwise)
- Ajouter `_classify_cause()` ou l'intégrer dans le prompt
- Mettre à jour `criteria_scores` dans le run_store
- *Dépendances* : aucune

**Étape 2 — JSON consolidé** (anti-accumulation)
- Implémenter `_build_consolidated_context()`
- Remplacer la concaténation de `cr_context` dans `_run_scaffold_cycle`
- *Dépendances* : aucune (peut être parallélisé avec étape 1)

**Étape 3 — Format de question contraint**
- Implémenter `_generate_questions()` avec `question_type` et `hint`
- *Dépendances* : étape 1 (`cause` nécessaire pour générer les bonnes questions)

**Étape 4 — Asynchronisme `waiting_for_client`**
- Étendre `run_store` avec `pending_questions`, `client_answers`, `agent_state`
- Nouveau statut `waiting_for_client`
- `store.set_pending_questions()`
- `POST /api/runs/{run_id}/answer`
- Routing dans `api_scaffold` sur `missing_spec`
- *Dépendances* : étapes 1 et 3

**Étape 5 — Dashboard "Décisions en attente"**
- Badge `waiting` dans la liste des runs
- Vue questions avec `question_type` (texte, choix unique, choix multiple)
- Wording selon contraintes section 7
- *Dépendances* : étape 4

**Étape 6 — CDC pass 0** (cdc-router)
- `_detect_cdc_gaps()` dans cdc-router
- Intégration dans le flow avant passes 1-2-3
- Filtre KG pour ne pas re-poser les questions connues
- *Dépendances* : étapes 3, 4, 5

**Étape 7 — RAG/KG indexing des résolutions**
- Indexer `client_decision`, `scaffold_fix`, `deterministic_fix` dans le RAG
- Créer edges `RESOLVED_BY`, `REQUIRES` dans le KG
- *Dépendances* : toutes les étapes précédentes

---

## 9. Limites acceptées à court terme

| Limite | Impact | Mitigation |
|---|---|---|
| Cold start RAG vide | Beaucoup de questions sur les premiers projets | Corpus de seed à constituer progressivement |
| `code_error` approximative sans tests unitaires | Quelques faux positifs classés `model_error` | Acceptable — améliore avec le volume de données |
| Pass 0 peut sur-détecter sur les bons CDCs | Questions inutiles sur un CDC exhaustif | Seuil de confiance à calibrer sur les premiers projets |
| Latence accrue en mode `waiting_for_client` | Pipeline non synchrone | Architecture async déjà prévue (étape 4) |

---

## 10. Trajectoire d'apprentissage attendue

```
Projet 1  →  3 questions posées, 2 cycles scaffold
Projet 5  →  1 question (2 autres déjà dans le KG pour ce type de CDC)
Projet 10 →  0 question, scaffold passe du premier coup sur les patterns connus
```

Chaque friction devient une connaissance. Le système apprend de ses propres blocages et de chaque décision client.
