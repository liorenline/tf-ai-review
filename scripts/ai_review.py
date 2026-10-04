#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys

import anthropic
import requests

MARKER = "<!-- terraform-ai-review -->"
REDACTED = "(sensitive)"
MAX_DIFF_CHARS = 60_000
MAX_PLAN_CHARS = 80_000
LEVELS = ["info", "low", "medium", "high", "critical"]
ICONS = {"info": "ℹ️", "low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}

SYSTEM_PROMPT = """You are a senior DevOps and cloud security engineer reviewing a Terraform pull request.
You get two inputs: the git diff of the .tf code and a JSON summary of `terraform plan`
(values Terraform marks as sensitive are already redacted).

Focus on what can actually hurt in production:
- destroy or replace of stateful resources (databases, buckets, volumes, ECS services, load balancers);
- security: ingress from 0.0.0.0/0 or ::/0, public S3 access, IAM with "*" actions/resources,
  missing encryption, secrets hardcoded in code;
- reliability: missing deletion protection or backups, single-AZ, risky lifecycle settings;
- cost surprises: large instance types, NAT gateways, unbounded autoscaling;
- hygiene that matters: missing tags, hardcoded IDs/regions, unpinned provider or module versions.

Rules:
- Base every finding on the inputs and name the exact resource address.
- Skip pure formatting nitpicks. If the change is clean, return an empty findings list.
- Recommendations must be concrete (show the attribute or snippet to change when useful).
- Write summary, issues and recommendations in {language}.
Submit the result only through the submit_review tool."""

REVIEW_TOOL = {
    "name": "submit_review",
    "description": "Submit the structured review of the Terraform change.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-3 sentences: what this PR changes in the infrastructure.",
            },
            "risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "enum": LEVELS},
                        "resource": {"type": "string", "description": "Terraform resource address"},
                        "issue": {"type": "string"},
                        "recommendation": {"type": "string"},
                    },
                    "required": ["severity", "resource", "issue", "recommendation"],
                },
            },
        },
        "required": ["summary", "risk_level", "findings"],
    },
}


def redact(value, mask):
    if mask is True:
        return REDACTED
    if isinstance(value, dict) and isinstance(mask, dict):
        return {k: redact(v, mask.get(k)) for k, v in value.items()}
    if isinstance(value, list) and isinstance(mask, list):
        return [redact(v, mask[i] if i < len(mask) else None) for i, v in enumerate(value)]
    return value


def classify(actions: list[str]) -> str | None:
    if actions == ["create"]:
        return "create"
    if actions == ["delete"]:
        return "delete"
    if actions == ["update"]:
        return "update"
    if sorted(actions) == ["create", "delete"]:
        return "replace"
    return None


def summarize_plan(path: str):
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)

    changes = []
    counts = {"create": 0, "update": 0, "replace": 0, "delete": 0}
    for rc in plan.get("resource_changes", []):
        change = rc.get("change", {})
        kind = classify(change.get("actions", []))
        if kind is None:
            continue
        counts[kind] += 1

        before = redact(change.get("before"), change.get("before_sensitive"))
        after = redact(change.get("after"), change.get("after_sensitive"))
        entry = {"address": rc["address"], "action": kind}

        if kind == "update" and isinstance(before, dict) and isinstance(after, dict):
            entry["changed_attributes"] = {
                k: {"before": before.get(k), "after": after.get(k)}
                for k in sorted(set(before) | set(after))
                if before.get(k) != after.get(k)
            }
        elif kind == "delete":
            entry["before"] = before
        else:
            entry["after"] = after
            if kind == "replace" and change.get("replace_paths"):
                entry["replace_because_of"] = change["replace_paths"]
        changes.append(entry)
    return changes, counts


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated {len(text) - limit} chars ...]"


def ask_claude(diff: str, changes: list, model: str, language: str) -> dict:
    client = anthropic.Anthropic()
    plan_text = truncate(
        json.dumps(changes, indent=1, ensure_ascii=False, default=str), MAX_PLAN_CHARS
    )
    user_msg = (
        f"<diff>\n{truncate(diff, MAX_DIFF_CHARS) or '(empty)'}\n</diff>\n\n"
        f"<plan_changes>\n{plan_text}\n</plan_changes>"
    )
    msg = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM_PROMPT.replace("{language}", language),
        tools=[REVIEW_TOOL],
        tool_choice={"type": "tool", "name": "submit_review"},
        messages=[{"role": "user", "content": user_msg}],
    )
    for block in msg.content:
        if block.type == "tool_use":
            return block.input
    raise RuntimeError("Claude did not return a submit_review tool call")


def severity_rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else 0


def render(review: dict, counts: dict, model: str) -> str:
    risk = review.get("risk_level", "low")
    lines = [
        MARKER,
        "## 🤖 Terraform AI Review",
        "",
        f"**Plan:** {counts['create']} to create, {counts['update']} to update, "
        f"{counts['replace']} to replace, {counts['delete']} to destroy",
        f"**Risk:** {ICONS.get(risk, '')} `{risk}`",
        "",
        review.get("summary", ""),
    ]
    findings = sorted(
        review.get("findings", []),
        key=lambda f: severity_rank(f.get("severity", "info")),
        reverse=True,
    )
    if findings:
        lines += ["", "### Findings"]
        for f in findings:
            sev = f.get("severity", "info")
            lines += [
                "",
                f"{ICONS.get(sev, '')} **{sev.upper()}** — `{f.get('resource', '?')}`",
                "",
                f.get("issue", ""),
                "",
                f"**Fix:** {f.get('recommendation', '')}",
            ]
    else:
        lines += ["", "No issues found ✅"]
    lines += [
        "",
        f"<sub>Generated by `{model}`. AI review is advisory and does not replace human review.</sub>",
    ]
    return "\n".join(lines)


def upsert_comment(body: str) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    pr = os.environ["PR_NUMBER"]
    headers = {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
    }
    base = f"https://api.github.com/repos/{repo}"

    resp = requests.get(
        f"{base}/issues/{pr}/comments", headers=headers, params={"per_page": 100}, timeout=30
    )
    resp.raise_for_status()
    existing = next(
        (c for c in resp.json() if MARKER in (c.get("body") or "") and c["user"]["type"] == "Bot"),
        None,
    )
    if existing:
        r = requests.patch(
            f"{base}/issues/comments/{existing['id']}", headers=headers,
            json={"body": body}, timeout=30,
        )
    else:
        r = requests.post(
            f"{base}/issues/{pr}/comments", headers=headers, json={"body": body}, timeout=30
        )
    r.raise_for_status()
    print(f"Review comment {'updated' if existing else 'posted'} on PR #{pr}")


def publish(body: str) -> None:
    if all(os.environ.get(v) for v in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "PR_NUMBER")):
        upsert_comment(body)
    else:
        print(body)


def main() -> int:
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
    language = os.environ.get("REVIEW_LANGUAGE", "English")

    changes, counts = summarize_plan(os.environ.get("PLAN_JSON", "plan.json"))
    diff = read_text(os.environ.get("PR_DIFF", "pr.diff"))

    if not changes:
        publish(f"{MARKER}\n## 🤖 Terraform AI Review\n\nNo infrastructure changes in the plan ✅")
        return 0

    try:
        review = ask_claude(diff, changes, model, language)
    except (anthropic.APIError, RuntimeError) as e:
        print(f"::warning::AI review skipped: {e}")
        return 0

    publish(render(review, counts, model))

    risk = review.get("risk_level", "low")
    fail_on = os.environ.get("FAIL_ON", "").strip().lower()
    if fail_on in LEVELS and severity_rank(risk) >= severity_rank(fail_on):
        print(f"::error::Risk level '{risk}' is at or above FAIL_ON='{fail_on}'")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())