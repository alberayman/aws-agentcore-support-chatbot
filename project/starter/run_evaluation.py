#!/usr/bin/env python3
"""Upload the eval dataset to S3 and run a Bedrock Evaluations LLM-as-a-judge job.

    python run_evaluation.py                      # upload + create + poll
    python run_evaluation.py --no-wait            # create and exit
    python run_evaluation.py --status <jobArn>    # check an existing job

Reads the S3 bucket and evaluation role from the bug-report-testing-stack
outputs, so nothing needs pasting by hand.

This is a bring-your-own-inference (BYOI) evaluation: generate-eval-dataset.py
already ran every test case through the harness and recorded the replies, so
Bedrock only judges the recorded text. That is why inferenceConfig uses
`precomputedInferenceSource` rather than `bedrockModel` — and why
`inferenceSourceIdentifier` MUST equal the `modelIdentifier` written into the
JSONL by generate-eval-dataset.py (default: "my-support-chatbot").
"""

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

TESTING_STACK = "bug-report-testing-stack"


def stack_outputs(cfn, stack_name):
    stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


def check_dataset(path, expected_identifier):
    """Fail early on the mistakes that make a job fail minutes later."""
    lines = [l for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        sys.exit(f"{path} is empty — run generate-eval-dataset.py first.")
    identifiers, errors = set(), 0
    for n, line in enumerate(lines, 1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.exit(f"{path} line {n} is not valid JSON: {exc}")
        for key in ("prompt", "referenceResponse", "modelResponses"):
            if key not in rec:
                sys.exit(f"{path} line {n} is missing '{key}'.")
        resp = rec["modelResponses"][0]
        identifiers.add(resp.get("modelIdentifier"))
        if resp.get("response", "").startswith("[HARNESS_ERROR]"):
            errors += 1
        if "<thinking>" in resp.get("response", "").lower():
            print(f"  WARNING line {n}: response still contains <thinking> — "
                  "run patch_thinking.py and regenerate.")
    if errors:
        print(f"  WARNING {errors} record(s) contain [HARNESS_ERROR] — the judge "
              "will score those as failures.")
    if identifiers != {expected_identifier}:
        sys.exit(f"modelIdentifier in the dataset is {identifiers}, but "
                 f"--inference-source-id is '{expected_identifier}'. They must match.")
    print(f"  {len(lines)} records, identifier '{expected_identifier}' — looks valid.")
    return len(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--jsonl", default="output_eval_dataset.jsonl")
    p.add_argument("--job-name", default=None,
                   help="Evaluation job name (default: support-chatbot-eval-<n>).")
    p.add_argument("--judge-model", default="us.amazon.nova-pro-v1:0",
                   help="Model that acts as the judge.")
    p.add_argument("--inference-source-id", default="my-support-chatbot",
                   help="Must match modelIdentifier in the JSONL.")
    # NOTE: Model-as-a-Judge only accepts "General", which is absent from
    # botocore's taskType enum (the service is ahead of the SDK model).
    p.add_argument("--task-type", default="General",
                   choices=["General", "Summarization", "Classification",
                            "QuestionAndAnswer", "Generation", "Custom"])
    p.add_argument("--metrics", default="Builtin.Correctness",
                   help="Comma-separated metric names.")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--no-wait", action="store_true")
    p.add_argument("--status", default=None,
                   help="Poll an existing job ARN instead of creating one.")
    args = p.parse_args()

    bedrock = boto3.client("bedrock", region_name=args.region)

    if args.status:
        return poll(bedrock, args.status)

    cfn = boto3.client("cloudformation", region_name=args.region)
    out = stack_outputs(cfn, TESTING_STACK)
    bucket = out["EvalDatasetBucketName"]
    role_arn = out["BedrockEvalRoleArn"]
    print(f"bucket: {bucket}\nrole:   {role_arn}")

    print(f"\nValidating {args.jsonl} ...")
    check_dataset(args.jsonl, args.inference_source_id)

    key = f"input/{Path(args.jsonl).name}"
    boto3.client("s3", region_name=args.region).upload_file(
        args.jsonl, bucket, key)
    dataset_uri = f"s3://{bucket}/{key}"
    print(f"uploaded -> {dataset_uri}")

    # Job names must be unique per account; suffix keeps re-runs from clashing.
    job_name = args.job_name or f"support-chatbot-eval-{int(time.time())}"
    output_uri = f"s3://{bucket}/output/"

    print(f"\nCreating evaluation job '{job_name}'")
    print(f"  judge:   {args.judge_model}")
    print(f"  metrics: {args.metrics}")
    resp = bedrock.create_evaluation_job(
        jobName=job_name,
        jobDescription="LLM-as-a-judge evaluation of the AgentCore support chatbot",
        roleArn=role_arn,
        applicationType="ModelEvaluation",
        evaluationConfig={
            "automated": {
                "datasetMetricConfigs": [{
                    "taskType": args.task_type,
                    "dataset": {
                        "name": "support-chatbot-tests",
                        "datasetLocation": {"s3Uri": dataset_uri},
                    },
                    "metricNames": [m.strip() for m in args.metrics.split(",")],
                }],
                "evaluatorModelConfig": {
                    "bedrockEvaluatorModels": [
                        {"modelIdentifier": args.judge_model}
                    ]
                },
            }
        },
        inferenceConfig={
            "models": [{
                "precomputedInferenceSource": {
                    "inferenceSourceIdentifier": args.inference_source_id
                }
            }]
        },
        outputDataConfig={"s3Uri": output_uri},
    )

    job_arn = resp["jobArn"]
    print(f"\njobArn: {job_arn}")
    print(f"results will land under: {output_uri}")

    if args.no_wait:
        print(f"\nCheck later with:\n  python run_evaluation.py --status {job_arn}")
        return

    poll(bedrock, job_arn)


def poll(bedrock, job_arn, timeout=2400):
    print("\nPolling (a 9-record job usually finishes in a few minutes)...")
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        job = bedrock.get_evaluation_job(jobIdentifier=job_arn)
        status = job["status"]
        if status != last:
            print(f"  status: {status}")
            last = status
        if status in ("Completed", "Failed", "Stopped"):
            if status != "Completed":
                print(json.dumps(
                    {k: v for k, v in job.items() if "ailure" in k or "essage" in k},
                    default=str, indent=2))
            print(f"\noutput: {job['outputDataConfig']['s3Uri']}")
            print("Open the job in the Bedrock console -> Evaluations to read the "
                  "score, and screenshot that page for your submission.")
            return
        time.sleep(20)
    print("Timed out polling; the job may still be running. Re-check with --status.")


if __name__ == "__main__":
    main()
