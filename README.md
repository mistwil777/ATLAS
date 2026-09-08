# ATLAS

Collection of autonomous agents for building agentic solutions.

Each agent is an independent Python package: installable, testable, and deployable standalone.

## Agents

| Agent | Description | Status |
|---|---|---|
| [pair-forge](agents/pair-forge/) | Transforms raw documents into fine-tuning pairs (LLM generation + judge validation) | stable |
| [agent-tester](agents/agent-tester/) | Validates any ATLAS agent: structural contract, prompt contracts, behavioral golden tests | stable |

## Install

```bash
# All agents
uv pip install -e agents/pair-forge/ -e agents/agent-tester/

# Single agent
uv pip install git+https://github.com/mistwil777/ATLAS.git#subdirectory=agents/pair-forge
```

## ATLAS Agent Contract

Every agent in this collection exposes:

- `check` — verifies tool versions (missing / deprecated / untested)
- `init` — generates a Dockerfile from its dependency manifest
- `install` — smart install with smoke tests and auto-replacement on failure
- `tools.py` — dependency manifest with `primary`, `alternatives`, `smoke_test`, `tested_up_to`
- `tests/smoke/` — capability smoke tests with reference fixtures

Validate any agent with `agent-tester`:

```bash
python -m agent_tester run --agent agents/pair-forge --level 1  # structural contract
python -m agent_tester run --agent agents/pair-forge --level 2  # prompt contracts
python -m agent_tester run --agent agents/pair-forge --level 3 --llm-provider bedrock --llm-model-id us.amazon.nova-pro-v1:0
```

## License

MIT
