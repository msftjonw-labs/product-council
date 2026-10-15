# Product Council

A service where eight AI seats — **Customer Success, Engineering, Product, Marketing, Security, Support,
Forecast Agent** and a
**Skeptic** — each optionally running on a different frontier model, debate whether a gap observed in a customer
scenario should be productized. The council asks clarifying questions first, requires every claim to cite evidence,
and keeps researching until **≥ 75 %** of voting seats validly agree on a proposal (or the loop is exhausted).

See [docs/DESIGN.md](docs/DESIGN.md) for the full design.

## Customer experience: Windows desktop app

Launch **ProductCouncil.exe**. Customers do not install Python, edit configuration, or prepare input files.
They need an internet connection, an API key with quota/billing enabled, and access to a supported chat model.
Microsoft Word is not needed to generate the document; Word or another DOCX reader can open it.

1. **Connect a model.** Choose OpenAI or Anthropic, enter a masked API key, and click **Load available models**.
   The app retrieves the provider's model catalog and preselects the newest listed chat candidate; no model-ID
   lookup or typing is required. You can choose another model from the dropdown. Click **Validate key and
   continue** to test actual inference access and JSON responses before proceeding. The test may incur a charge.
   Catalog entries are candidates, not a guarantee of inference permissions or compatibility.
   **Azure OpenAI** is an advanced option: it requires the resource's HTTPS endpoint and an organization-specific
   deployment name supplied by your administrator, not a public model ID. Inference API keys do not provide
   management-plane deployment discovery. Manual entry is also available if a provider restricts model listing.
2. **Describe the productization idea.** Explain the customer/market gap and proposed product.
3. **Define the hypothesis, expected results, and success criteria.** All are mandatory before deliberation.
   Enter one success metric per line, ideally including a baseline, target, and evaluation timeframe.
4. **Discuss with the council.** Watch live progress and answer follow-up dialogs for clarification or more
   evidence. Blank answers mean unknown, not fabricated data. All eight agents and the moderator use the
   selected model with distinct roles. No automatic web research or sample knowledge is used in this flow.
5. **Preview and approve.** Review the proposal and full audit details, then click **Approve preview and save
   Word document** and choose a location. Until then, nothing is exported. Discarding, cancelling the save
   dialog, or closing without approval generates no document.

The Word document contains the idea, brief, complete final proposal, Forecast Agent's post-release outlook,
votes and dissent, clarifying answers, all discussion rounds and claims, research requests, and evidence
excerpts and source locations. **No consensus** remains explicit in the preview and exported document.
Approval authorizes document export only, not product release.

Keys are session-only by default. Select **Remember this connection on this Windows account** to save the
validated key, provider, model, and Azure endpoint (if applicable) in **Windows Credential Manager**.
OpenAI, Anthropic, and Azure connections are stored separately within the app's vault record. Future launches
load and validate the last saved provider automatically, then proceed to idea entry without asking for the key.
This small validation request may incur a charge. You do not need to generate a fresh API key each session.

On the idea page, **Manage connections** lets you switch providers, update keys/models, or forget a provider's
saved connection. Switching providers loads that provider's saved details if present. Unchecking Remember
and successfully validating removes that provider's existing saved connection and uses it only for the session.
**Forget saved connection** deletes the stored details immediately after confirmation; it does not revoke
the API key at its provider. Invalid/revoked keys return you to the editable connection form.

Saved connections belong to the current Windows account and do not automatically travel to other computers.
Credential Manager protects storage; the app still accesses keys in memory when calling a provider. There is
no plaintext file or alternate credential-backend fallback. Storage failures are explicit and do not pretend a
key was saved. Keys are never written into report/configuration files and are excluded from error messages.
Idea text, answers, and evidence are sent to the selected AI provider during discussion, so only supply data
you are authorized to share. Export approval is separate from this disclosed AI processing.
All agents sharing a model reduces setup friction but also reduces model diversity.

### Build and distribute the executable (maintainers only)

Build on Windows using Python 3.11+ with Tk support:

```powershell
python -m pip install -e ".[desktop,build]"
.\build-desktop.ps1
```

Distribute `dist\ProductCouncil.exe`. Python, provider SDKs, Tk, and Word-generation dependencies are bundled;
no configuration or sample knowledge files are required beside the executable. An unsigned build may trigger
Windows SmartScreen; sign and distribute through your organization's trusted channel for customer releases.
Application Control policies may also block the bundled Python DLLs extracted by a single-file build.
Deployment must be approved by the organization's administrators; do not disable or bypass those policies.
The app does not hardcode a moving "latest" model: it loads current catalog candidates and preselects by
provider-reported timestamp. Newest listed does not necessarily mean the latest or best model, and prices vary.
Changing providers, keys, or endpoints clears the model selection so it cannot be reused with different credentials.

For development without building:

```powershell
python -m product_council.desktop
```

The CLI and Python API below remain available for automation; their existing output behavior is unchanged.

## Quick start (offline, no API keys)

```powershell
python -m product_council run --question-file examples\day2_question.txt --brief examples\brief.json --answers examples\answers.json --mock
```

Outputs `out\decision.md` (decision memo) and `out\transcript.json` (full audit trail). Exit code `0` = consensus,
`2` = no consensus.

Interactive mode (answer clarifying questions and paste extra research data between rounds):

```powershell
python -m product_council run -q "Should we build X?" --interactive-research
```

## Required product brief

Before any agent runs, the requester must define a **hypothesis**, **expected result**, and **success metrics**.
Interactive runs collect these first. Non-interactive runs (including `--answers` or `--no-clarify`) require
`--brief <path>` with a JSON object:

```json
{
  "hypothesis": "Guided recommendations will reduce avoidable support escalations.",
  "expected_result": "Pilot customers resolve routine issues more independently after release.",
  "success_metrics": ["Reduce support cases per pilot customer by 20% within 90 days versus the prior 90 days."]
}
```

All three fields must be non-empty; metrics must be a list of non-empty strings. Include measurable targets
and evaluation timeframes. The brief is included in every agent's context, decision memo, and transcript as
requester-defined targets, **not proof**. Proposals retain the original metrics and may add more.
See [examples/brief.json](examples/brief.json) for a fictional sample.

The **Forecast Agent** forecasts post-release activities over 30/60/90 days: onboarding, adoption, monitoring,
support, maintenance, capacity, feedback, and follow-up releases. It identifies owners, dependencies, risks,
and metric checkpoints, distinguishing projections from facts. It participates in clarification, debate,
rebuttal, and voting. There are eight voters by default, so six valid agreements meet the 75% threshold.

Python callers must also supply the brief:

```python
import asyncio
from product_council import Council, ProductBrief, load_config

brief = ProductBrief(
    hypothesis="Guided recommendations will reduce avoidable support escalations.",
    expected_result="Pilot customers resolve routine issues more independently.",
    success_metrics=["Reduce support cases per customer by 20% within 90 days versus the prior 90 days."],
)
outcome = asyncio.run(Council(load_config("council.toml")).run("Should we build X?", brief=brief))
```

Existing callers must now supply `brief`, and existing configurations must add a `[seats.forecast]` block.
Like every other seat, it can use its own provider/model.

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

Desktop and Word tests require the `desktop` extra above; GUI tests are skipped if no desktop display is available.
