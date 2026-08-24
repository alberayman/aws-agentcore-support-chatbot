# Customer Support Chatbot — Amazon Bedrock AgentCore

A customer support chatbot for a fictional online shop, built on the **Amazon Bedrock
AgentCore managed harness**. All routing, information gathering and grounding lives in
a single system prompt (`system_prompt.txt`) — there are no condition nodes and no
separate classifier.

Model: `us.amazon.nova-pro-v1:0`, greedy decoding (temperature 0, topK 1).
Region: `us-east-1`. Account type: AWS Academy Learner Lab (`voclabs`).

## Architecture

```
customer ──► chat.py ──► AgentCore managed harness ──► Nova Pro
                              │  (stateful per session)
                              └─► AgentCore Gateway
                                      └─► Lambda: bug-report-tool-stack-create-bug-report
                                              └─► DynamoDB: bug-report-tool-stack-bug-reports
```

The gateway exposes the Lambda to the model as **`bugreports___create_bug_report`**
(`<targetName>___<toolName>`, three underscores). The system prompt names the tool
with that exact prefixed string.

## The three routes

| Route | Behaviour |
|---|---|
| **BUG_REPORT** | Audits which of `description` / `stepsToReproduce` / `environment` the customer has given, asks for one missing fact per turn, then calls the tool and reports the real `ticketId`. |
| **PLATFORM_QUESTION** | Answers only from the FAQ substituted at `{{FAQ}}`. If the FAQ does not cover it, falls through to OTHER. |
| **OTHER** | Two-sentence hand-off to 1-800-555-0199 (Mon–Fri). Also the destination for prompt-injection attempts. |

## Reproducing

```bash
cd project/starter
python -m venv /workspace/.venv-agentcore --system-site-packages   # see Finding 1
/workspace/.venv-agentcore/bin/pip install -r requirements.txt
export PY=/workspace/.venv-agentcore/bin/python

aws cloudformation deploy --template-file cloudformation-tool.yaml \
  --stack-name bug-report-tool-stack --capabilities CAPABILITY_NAMED_IAM --region us-east-1
$PY setup_gateway.py            # gateway + tool target -> agentcore_config.json
$PY create_harness.py           # harness from system_prompt.txt
$PY set_harness_memory.py --mode disabled    # see Finding 3
$PY patch_thinking.py           # see Finding 2
$PY chat.py                     # manual multi-turn testing

$PY generate-eval-dataset.py --tests-json harness-tests.json
aws cloudformation deploy --template-file cloudformation-testing.yaml \
  --stack-name bug-report-testing-stack --capabilities CAPABILITY_NAMED_IAM --region us-east-1
$PY run_evaluation.py --judge-model amazon.nova-pro-v1:0     # see Finding 5
```

Verify tickets really landed — never trust the transcript alone (Finding 4):

```bash
aws dynamodb scan --table-name bug-report-tool-stack-bug-reports --region us-east-1 \
  --query 'Items[].[ticketId.S,createdAt.S,environment.S]' --output table
```

## Added tooling

The three scripts below are not part of the starter; each exists because of a
finding below.

| Script | Purpose |
|---|---|
| `set_harness_memory.py` | Toggles the harness's cross-session memory (`--mode disabled\|managed`, `--show`). |
| `patch_thinking.py` | Strips Nova's `<thinking>` blocks in `chat.py` and `generate-eval-dataset.py`. Idempotent. |
| `run_evaluation.py` | Validates the JSONL, uploads to S3, creates the Evaluations job, polls to completion. |

## Prompt iteration log

Seven versions. Each row is a real observed defect, not a hypothetical.

| v | Change | Observed result |
|---|---|---|
| 1 | Baseline: routes, three required facts, prose "do not call the tool until you have all three" | Called the tool on turn 1 with **no details**, filing ticket `86f36d72` with `description: "User reported a bug but did not specify what is broken."` Also leaked `<thinking>` to the customer. |
| 2 | Added `OUTPUT RULES` forbidding `<thinking>`; hardened the gate with repeated prohibitions | Gate worked (turn 1 asked a question), but **tool calling stopped entirely**. Reported a ticket ID from a *previous* conversation. |
| 3 | Rebalanced gate to positive branches; explicit anti-fabrication rule | Still no tool call. Invented a plausible UUID (`8b655540-…`) with no DynamoDB row behind it. |
| 4 | **Removed** the `<thinking>` prohibition (single-line A/B) | Tool calling returned immediately. See Finding 2. |
| 5 | Gated the `"not provided"` escape on having actually asked twice | Fixed the placeholder ticket, but produced a file-then-ask hybrid: filed `df5323e9` *and* asked for the environment in the same turn. |
| 6 | Required an explicit three-line fact audit inside the reasoning channel; "a turn is either a question or a tool call, never both" | Correct. No placeholder tickets after this point. |
| 7 | Clarified that one sentence can supply several facts | Fixed re-asking for steps the customer had already given in the same sentence. |

The DynamoDB table records this history: three rows with `environment: "not provided"`
(`86f36d72` 22:46, `58241cb0` 23:11, `df5323e9` 23:14) and none after v6 went live at
23:16.

## Findings

**1. The sandbox reverts system site-packages on idle restart.** `pip install -r
requirements.txt` into `/opt/venv` succeeded, then silently reverted to boto3 1.42.54
after a workspace timeout, producing
`AttributeError: 'BedrockAgentCoreControl' object has no attribute 'list_harnesses'`
— which reads like a missing API rather than a lost install (1.42.54 exposes 86
operations for that service; 1.43.76 exposes 165). Project dependencies now live in a
workspace-local venv that the image restore cannot touch.

**2. Nova's reasoning output and its tool use are coupled.** Suppressing `<thinking>`
in the system prompt also suppresses tool calls: the model then satisfies "tell the
customer their ticket ID" by inventing one. Measured with a single-line A/B on an
otherwise identical prompt, same gateway and harness:

| Prompt | `[tool call]` | DynamoDB row |
|---|---|---|
| forbids `<thinking>` | no | none — `BUG-123456`, then `8b655540-…` |
| allows `<thinking>` | yes | `f3a28f8d-…` created |

With `apiFormat: converse_stream`, the reasoning pass appears to be where the model
commits to a `toolUse` block. Presentation was therefore moved to the client
(`patch_thinking.py`) rather than the prompt. The same insight drove the v6 fix: since
the reasoning channel exists and is hidden, the prompt makes the model write its
fact audit *there*, where the decision is actually made.

**3. Managed memory breaks test isolation.** AgentCore attaches a managed memory
resource by default. It persists across `runtimeSessionId`, so a "fresh" session
recalled a previous conversation and re-served its ticket ID instead of filing a new
one. `generate-eval-dataset.py`'s claim that a new session per test case "keeps every
test independent" is therefore false out of the box — eval scores would carry
test-order effects. Disabling memory fixed the isolation, and multi-turn collection
still works, so within-session state is independent of the memory resource.

**4. A transcript is not evidence that a ticket exists.** Two separate failure modes
produced convincing replies with nothing in the database, one of them a correctly
formatted UUID. `chat.py` also does not always print `[tool call]` even when the tool
did fire, because the harness runs tools server-side and the `contentBlockStart`
carrying `toolUse` is not always surfaced in the stream. Every claim in this README is
backed by a DynamoDB scan or a CloudWatch log line, not by a transcript.

**5. Two SDK/service mismatches, in opposite directions.** botocore 1.43.76's
`taskType` enum omits `General`, which is the only value Model-as-a-Judge accepts —
the service was ahead of the SDK model, and the blocker was client-side `argparse`
validation, not the API. Separately, the starter's `bedrock-eval-role` cannot invoke
`us.amazon.nova-pro-v1:0`: a `us.` inference profile is a distinct resource type from a
foundation model and needs its own ARN in the policy. Worked around by judging with the
base model id `amazon.nova-pro-v1:0`; the correct fix is to add an
`inference-profile/*` resource to the role in `cloudformation-testing.yaml`.

**6. The model will satisfy a required parameter rather than ask for it.** Three
different prompt clauses across v1, v4 and v5 each produced a ticket with a fabricated
or placeholder field. This is why the Lambda validates the three fields server-side and
returns an error instead of writing an incomplete row — a required field cannot be
defended in the prompt alone.

## Test suite

`harness-tests.json` — 9 cases, each run in its own session (genuinely isolated once
memory is disabled):

- **3 bug report** — complete one-turn report (must file); vague "your website is broken" (must ask); description + steps but no environment (must ask for environment only)
- **3 platform question** — returns, refund timing, guest checkout
- **3 other** — out-of-scope coding request; in-topic question the FAQ does not cover; request for a human

`bug-02` and `bug-03` are the regression tests for the v1 and v5 defects respectively.
`bug-01` guards against over-correcting into never calling the tool.

## Evaluation observations

Job `support-chatbot-eval-1787527623` (`evaluation-job/vrpe1t5zypuk`) · Judge
`amazon.nova-pro-v1:0` · Metric `Builtin.Correctness` · 9 records,
bring-your-own-inference.

**Mean correctness: 1.000 (9/9)**

| # | Case | Score |
|---|---|---|
| 1 | bug-01-complete-report | 1.0 |
| 2 | bug-02-missing-details | 1.0 |
| 3 | bug-03-missing-environment | 1.0 |
| 4 | faq-01-return-policy | 1.0 |
| 5 | faq-02-refund-timing | 1.0 |
| 6 | faq-03-guest-checkout | 1.0 |
| 7 | other-01-out-of-scope-request | 1.0 |
| 8 | other-02-not-in-faq | 1.0 |
| 9 | other-03-human-agent | 1.0 |

Observations:

1. **All three routes scored equally.** The FAQ and hand-off cases (4-9) are the
   easy half: the reference response and the FAQ text overlap heavily, so a
   grounded answer scores well almost by construction. The bug cases (1-3) are the
   ones that carry information, because each is a regression test for a defect that
   actually occurred — `bug-02` for the v1 premature tool call, `bug-03` for the v5
   file-then-ask hybrid, and `bug-01` guarding against over-correcting into never
   calling the tool at all.

2. **Nothing was marked down, and that is worth being sceptical about.** The judge's
   own explanation for case 1 shows it reasoning around a gap rather than
   penalising it: the reply was `"Ticket <uuid> has been filed. The engineering team
   will follow up."`, and the judge noted it *"does not explicitly confirm that the
   bug report has been filed"* but accepted the ticket ID as implying it. That is a
   defensible call, but it shows the metric rewards semantic overlap with the
   reference rather than strict compliance — a 1.000 here means "no contradictions",
   not "no room to improve". The judge also shares a model family with the system
   under test, so shared blind spots are plausible.

3. **The score is a poor discriminator; the datastore is the real one.** Earlier
   prompt versions would have scored well on this metric while filing junk. v3
   produced `"8b655540-0d89-447f-8a16-0a029a93584c"` — a correctly formatted UUID
   with no DynamoDB row behind it — which reads as a perfectly good ticket
   confirmation to a text-only judge. Likewise the v1 and v5 placeholder tickets
   (`environment: "not provided"`) would have satisfied case 1's reference response.
   The defects were caught by scanning DynamoDB and CloudWatch, not by the
   evaluation. An LLM-as-a-judge run over transcripts cannot see whether a side
   effect occurred, so it belongs alongside datastore assertions, not instead of
   them.

4. **Coverage, not score, is the honest next step.** Nine single-turn cases with a
   perfect result mean the suite is no longer discriminating. What it does not cover:
   multi-turn collection (verified only manually), a customer who refuses to answer,
   two bugs in one conversation, a tool-failure path, and injection attempts beyond
   the single "ignore your instructions" phrasing tried by hand.

## Known limitations

- The FAQ is embedded verbatim in the system prompt, so its ~6 KB is re-sent on every
  turn. It does not scale to a larger document, and a policy change means redeploying a
  harness with no version history for the document itself. A Bedrock Knowledge Base
  with a vector index is the production answer.
- The eval dataset is single-turn. Multi-turn collection is verified manually through
  `chat.py` plus a DynamoDB scan.
- Bug reports are free-text customer input written straight to DynamoDB with no PII
  scrubbing and no retention policy.
- The judge is the same model family as the system under test, which risks shared
  blind spots.

## Evidence map

| Rubric criterion | Evidence |
|---|---|
| Classification and routing | `system_prompt.txt` STEP 1; transcripts covering all three routes, including a prompt-injection attempt handled as OTHER |
| Bug report path | `chat.py` transcript with per-turn questions and `[tool call] bugreports___create_bug_report`; DynamoDB row `97ee179f-…` matching that transcript |
| Platform question / other | Transcripts of a covered question, an uncovered in-topic question (student discount), and an out-of-scope request |
| Testing and evaluation | `harness-tests.json`, `output_eval_dataset.jsonl`, Evaluations results screenshot, and the observations above |

## Cleanup

```bash
$PY cleanup_agentcore.py
aws s3 rm s3://udacity-agentic-engineer-c1-eval-155992032109 --recursive
aws cloudformation delete-stack --stack-name bug-report-testing-stack --region us-east-1
aws cloudformation delete-stack --stack-name bug-report-tool-stack --region us-east-1
```

