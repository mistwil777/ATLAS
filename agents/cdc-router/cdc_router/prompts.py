"""Prompts for cdc-router LLM calls — four-pass architecture (pass 0 + passes 1-2-3)."""

# ── Passe 0 : détection des flous, manques et incohérences ───────────────────
DETECT_GAPS_PROMPT = """\
Tu es un architecte logiciel expert. Tu reçois un cahier des charges (CDC) et tu dois \
identifier les points qui rendraient impossible une implémentation fiable.

## CDC à analyser
{cdc_text}

## Types de problèmes à détecter

**vague** : instruction présente mais non actionnable (trop ambiguë pour être testée)
  Exemples : "gérer les cas limites", "performance acceptable", "interface conviviale"

**missing** : information nécessaire à l'implémentation qui n'est pas dans le CDC
  Exemples : langue de sortie non spécifiée, format de date non précisé, niveaux de priorité non définis

**incoherent** : deux blocs ou contraintes qui se contredisent
  Exemples : "répondre en moins de 100ms" mais "appeler 3 APIs externes synchrones"

## Règles
- Ne signale QUE les problèmes bloquants pour l'implémentation
- Maximum 5 gaps — priorise par impact sur le scaffold
- Sois précis : cite le bloc concerné ou la phrase exacte
- Ne signale pas les choix stylistiques ou préférences — uniquement ce qui crée une ambiguïté fonctionnelle

## Format — JSON strict, tableau, aucun texte avant/après
[
  {{
    "type": "vague"|"missing"|"incoherent",
    "description": "<description du problème — une phrase>",
    "bloc_concerned": "<titre ou phrase du CDC concerné>",
    "impact": "high"|"medium"|"low"
  }}
]
Si aucun problème bloquant n'est détecté, retourne [] (tableau vide).
"""

# ── Passe 1 : segmentation fonctionnelle ─────────────────────────────────────
SEGMENTATION_PROMPT = """\
You are an expert software architect analyzing a functional specification (CDC).

Your ONLY task in this pass: decompose the CDC into independent functional blocs.
Do NOT classify blocs as ia/tool/code yet — focus entirely on WHAT each bloc does.

## Rules
- Each bloc = one precise function of the system
- Blocs must be independent (no overlap)
- Generate 2-4 evaluation criteria per bloc (verifiable assertions on the expected output)
  Each criterion: single sentence starting with a verb ("Output must...", "All fields must...")

## CDC to analyze
{cdc_text}

## Similar CDCs from past experience
{rag_examples}

## Output — return ONLY valid JSON, no markdown, no explanation
{{
  "segments": [
    {{
      "bloc": "<short description of what this bloc does>",
      "justification": "<why this is a separate functional bloc>",
      "criteria": ["<criterion 1>", "<criterion 2>"]
    }}
  ]
}}
"""

# ── Passe 2 : classification agent/outil/code d'intégration ──────────────────
CLASSIFICATION_PROMPT = """\
You are an expert software architect. You have a list of functional blocs to classify.

## Classification rules — apply strictly, in order

**AGENT (type="ia")** if ANY of these:
- Input is unstructured natural language (email, ticket, contract, free text)
- Task requires semantic interpretation (classification, summarisation, extraction, intent)
- Result is ambiguous or context-dependent — a deterministic function cannot produce it
- Task requires iterating over its own outputs — the system must decide autonomously when to stop, retry, or escalate based on intermediate results

**TOOL (type="tool")** if ALL of these (and NOT "ia"):
- Stateless callable with single responsibility: same input → same output, no memory, no side effects beyond its return value
- No decision loop: executes once and returns — it never decides to retry or escalate
- No LLM required: purely algorithmic or rule-based
- Reusable as-is by another team on a different project (reusable library function)
- Examples: OutputFormatter, JsonValidator, ConfidenceScorer, TextNormalizer

**INTEGRATION CODE (type="code")** if ALL of these (and NOT "ia" or "tool"):
- Pure glue code: adapts interfaces or transforms data formats between two components
- No business logic, no autonomy, no reusable abstraction
- Project-specific wiring that would be meaningless outside this pipeline (1-3 lines inline)

**EXCLUSIVITY RULE**: a PascalCase name cannot be "ia" in one bloc and "tool" in another.
If a name is ambiguous → choose "ia" if reasoning is required, "tool" otherwise.

## Available ATLAS agents (reuse exact name in agent_suggested if applicable)
{kg_agents}

## Available ATLAS tools (reuse exact name in agent_suggested if applicable)
{kg_tools}

## Blocs to classify
{segments_json}

## CDC context
{cdc_text}

{coherence_feedback_section}## Output — return ONLY valid JSON, no markdown, no explanation

REQUIRED fields:
- type "ia"   → agent_suggested REQUIRED (PascalCase). called_by: null.
- type "tool" → agent_suggested REQUIRED (PascalCase). called_by REQUIRED: exact agent_suggested of the "ia" bloc that calls this tool. NEVER empty string.
- type "code" → agent_suggested: null. called_by: null.

{{
  "blocs": [
    {{
      "bloc": "<same description as input>",
      "type": "ia" or "tool" or "code",
      "agent_suggested": "<PascalCase — REQUIRED for ia/tool, null for code>",
      "called_by": "<PascalCase ia agent — REQUIRED for tool, null for ia/code>",
      "justification": "<one sentence applying the decision rule above>",
      "criteria": ["<criterion 1>", "<criterion 2>"]
    }}
  ]
}}
"""

# ── Passe 3 : validation de cohérence architecturale ─────────────────────────
COHERENCE_PROMPT = """\
You are an expert software architect reviewing an architecture for consistency.

## Check the following blocs for architectural coherence:

1. Each "tool" bloc must have a called_by pointing to an existing "ia" bloc's agent_suggested.
2. No PascalCase name appears as both "ia" and "tool" in different blocs.
3. No circular dependencies between "ia" agents.
4. The pipeline is orderable (no deadlocks).

## Blocs to validate
{blocs_json}

## Return the corrected blocs — apply fixes inline, preserve correct blocs unchanged.
Return ONLY valid JSON, no markdown, no explanation.

{{
  "blocs": [
    {{
      "bloc": "...",
      "type": "ia" or "tool" or "code",
      "agent_suggested": "...",
      "called_by": "...",
      "justification": "...",
      "criteria": ["...", "..."]
    }}
  ],
  "corrections_applied": ["<description of each fix made, empty list if none>"]
}}
"""

# ── Correction / reprompt (conservés pour les retries JSON) ───────────────────
CORRECTION_PROMPT = """\
Your previous classification was evaluated and failed on the following criteria:

{failures}

Here is your previous output:
{previous_output}

Fix ONLY what failed. Keep correct blocs unchanged.
Preserve the "criteria" field on each bloc — update it only if the failure is about criteria quality.
Return ONLY valid JSON, no markdown, no explanation.

{{
  "blocs": [
    {{
      "bloc": "...",
      "type": "ia" or "tool" or "code",
      "agent_suggested": "<PascalCase name — REQUIRED for ia and tool, null ONLY for code>",
      "called_by": "<PascalCase agent — REQUIRED for tool, null for ia and code>",
      "justification": "...",
      "criteria": ["...", "..."]
    }}
  ]
}}
"""

REPROMPT = """\
Your previous response was not valid JSON. Return ONLY the JSON object, no markdown fences, no explanation.

Expected format:
{{
  "blocs": [
    {{
      "bloc": "...",
      "type": "ia" or "tool" or "code",
      "agent_suggested": "<PascalCase name — REQUIRED for ia and tool, null ONLY for code>",
      "called_by": "<PascalCase agent — REQUIRED for tool, null for ia and code>",
      "justification": "...",
      "criteria": ["...", "..."]
    }}
  ]
}}

Previous response:
{previous_response}
"""
