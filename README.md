# ATLAS

Framework d'agents autonomes pour construire des solutions agentiques de bout en bout — architecture **Agentic Mesh** (agents pairs découverts dynamiquement, exécution isolée par subprocess, mémoire collective partagée, composabilité sans couplage fort).

Chaque agent est un package Python indépendant : installable, testable et déployable seul. Les agents se composent via **atlas-bus** (isolation subprocess, découverte automatique par entry-points). **atlas-memory** fournit la mémoire partagée (RAG + Knowledge Graph) qui permet à chaque agent de s'améliorer dans le temps.

## Agents

| Agent | Description | Statut |
|---|---|---|
| [pair-forge](agents/pair-forge/) | Transforme des documents bruts en paires de fine-tuning (génération LLM + validation judge) | stable |
| [agent-tester](agents/agent-tester/) | Valide tout agent ATLAS : contrat structurel, contrats de prompt, tests comportementaux | stable |
| [agent-forge](agents/agent-forge/) | Génère le scaffold complet d'un nouvel agent ATLAS depuis une spec CDC ou en mode interactif | stable |
| [atlas-bus](agents/atlas-bus/) | Couche de découverte, isolation et routage — chaîne les agents en pipelines sans couplage | stable |
| [atlas-memory](agents/atlas-memory/) | Mémoire partagée RAG (Qdrant + fastembed) + Knowledge Graph (networkx) pour tous les agents | stable |
| [cdc-router](agents/cdc-router/) | Segmente un CDC en blocs ia/code et route chaque bloc vers l'agent ATLAS approprié via le KG | stable |
| [atlas-judge](agents/atlas-judge/) | Évalue les outputs LLM des agents — pointwise, pairwise (anti position-bias), reference-based | stable |

## Architecture

```
CDC → cdc-router
         ├── agents existants → atlas-bus → exécution isolée (subprocess)
         └── agents manquants → agent-forge → scaffold → agent-tester → atlas-bus

atlas-memory ← indexe chaque expérience validée (CDC, scaffold, eval, trace)
             → fournit contexte RAG + KG à tous les agents

atlas-judge ← évalue tout output LLM des agents (pointwise / pairwise / reference)
            → indexe les évaluations dans atlas-memory pour amélioration continue
```

## Quickstart

```bash
# Installer tous les agents
uv pip install -e agents/pair-forge/ -e agents/agent-tester/ \
               -e agents/agent-forge/ -e agents/atlas-bus/ \
               -e "agents/atlas-memory/[dev]" \
               -e "agents/cdc-router/" \
               -e "agents/atlas-judge/[dev]"

# Lister les agents découverts
atlas-bus list

# Router un CDC
atlas-bus run cdc-router --cdc path/to/cdc.txt

# Évaluer un output
python -m atlas_judge evaluate "output à évaluer" --agent cdc-router --mode pointwise

# Indexer une expérience dans la mémoire partagée
atlas-memory index cdc "Extraire les noms clients depuis des emails entrants"

# Interroger le Knowledge Graph
atlas-memory graph
```

## Contrat ATLAS

Tout agent du framework expose :

- `check` — vérifie les versions des outils (manquants / dépréciés / non testés)
- `init` — génère un Dockerfile depuis le manifeste de dépendances
- `install` — installation intelligente avec smoke tests
- `tools.py` — manifeste de dépendances : `primary`, `alternatives`, `smoke_test`, `tested_up_to`
- `tests/smoke/` — smoke tests avec fixtures de référence
- Entry-point `atlas.agents` — découverte automatique par atlas-bus

Valider un agent avec agent-tester :

```bash
python -m agent_tester run --agent agents/pair-forge --level 1  # contrat structurel
python -m agent_tester run --agent agents/pair-forge --level 2  # contrats de prompt
python -m agent_tester run --agent agents/pair-forge --level 3 --llm-provider bedrock --llm-model-id us.amazon.nova-pro-v1:0
```

## Développement local

```bash
python -m venv .venv
.venv/bin/pip install -e agents/pair-forge/ -e agents/agent-tester/ \
                      -e agents/agent-forge/ -e agents/atlas-bus/ \
                      -e "agents/atlas-memory/[dev]" \
                      -e agents/cdc-router/ \
                      -e "agents/atlas-judge/[dev]"

# Ajouter un nouvel agent dans .vscode/settings.json > python.analysis.extraPaths
# puis : .venv/bin/pip install -e agents/mon-agent/
```

## Licence

MIT
