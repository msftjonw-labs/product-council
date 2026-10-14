# Product Council

A service where seven AI seats — **Customer Success, Engineering, Product, Marketing, Security, Support** and a
**Skeptic** — each optionally running on a different frontier model, debate whether a gap observed in a customer
scenario should be productized. The council asks clarifying questions first, requires every claim to cite evidence,
and keeps researching until **≥ 75 %** of voting seats validly agree on a proposal (or the loop is exhausted).

See [docs/DESIGN.md](docs/DESIGN.md) for the full design.

## Quick start (offline, no API keys)

```powershell
python -m product_council run --question-file examples\day2_question.txt --answers examples\answers.json --mock
```

Outputs `out\decision.md` (decision memo) and `out\transcript.json` (full audit trail). Exit code `0` = consensus,
`2` = no consensus.

Interactive mode (answer clarifying questions and paste extra research data between rounds):

```powershell
python -m product_council run -q "Should we build X?" --interactive-research
```

## Using real models

```powershell
pip install -e .[all]
$env:AZURE_OPENAI_ENDPOINT = "https://<resource>.openai.azure.com"   # Entra ID auth via DefaultAzureCredential
$env:ANTHROPIC_API_KEY = "..."
```

Then edit `council.toml` and point each `[seats.<role>]` at a provider and model, e.g. Engineering on Claude,
Skeptic on a reasoning model, Customer Success on GPT. Supported provider types: `azure_openai`, `openai`
(also any OpenAI-compatible endpoint via `endpoint`), `anthropic`, `mock`.

## Knowledge sources

`[[sources]]` blocks map folders to evidence types (`internal_doc`, `customer_interview`, `team_vision`,
`company_vision`, `partnership`). The files under `knowledge/` are **fictional sample data** for the demo — replace
them with real exports or implement `ResearchSource` for Azure AI Search / SharePoint / interview repositories.

## Tests

```powershell
python -m unittest discover -s tests -t .
```
