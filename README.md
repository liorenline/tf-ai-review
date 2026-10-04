# tf-ai-review

A GitHub Action that automatically reviews Terraform changes in pull requests using the Claude API. On every PR it runs `terraform plan`, sends the plan and the diff to Claude, and posts a comment with the risks it found: security issues, resource deletions, unexpected costs, and best-practice violations.

## Usage

Add the `ANTHROPIC_API_KEY` secret to your repository, then create `.github/workflows/terraform-ai-review.yml`:

```yaml
name: Terraform AI Review

on:
  pull_request:
    paths:
      - "infra/**"

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: liorenline/tf-ai-review@v1
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          working-directory: infra
```

The action runs `terraform init` and `terraform plan` itself, so any credentials your Terraform code needs (for example, AWS via `aws-actions/configure-aws-credentials`) must be configured in an earlier step.

### Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `anthropic-api-key` | yes | | Anthropic API key |
| `working-directory` | no | `terraform` | Directory with Terraform code |
| `model` | no | `claude-sonnet-5-5` | Claude model id |
| `language` | no | `English` | Language of the review text |
| `fail-on` | no | empty | Fail the job at this risk level or above: `low`, `medium`, `high`, `critical` |
| `github-token` | no | `github.token` | Token used to post the PR comment |

## How it works

```
PR opened or updated
  -> terraform init + terraform plan
  -> terraform show -json            -> plan.json
  -> git diff base...HEAD            -> pr.diff
  -> scripts/ai_review.py
       1. keeps only create / update / replace / delete changes from the plan
       2. masks sensitive values (passwords, keys)
       3. sends the plan and diff to the Claude API
       4. receives a structured response (risk level, list of findings)
       5. posts a PR comment or updates the previous one
```

Key design decisions:

- **Structured output via tool use.** Claude does not return free-form text; it fills in the JSON schema of the `submit_review` tool. The script builds the Markdown comment itself, so the format is always consistent.
- **Token efficiency.** Only resources that actually change are sent to the model, and for updates only the changed attributes.
- **Data protection.** `terraform show -json` contains sensitive values in plain text and only flags them with an `after_sensitive` mask. The script replaces them with `(sensitive)` before sending anything.
- **One comment per PR.** The comment contains a hidden marker, so each new push updates it instead of creating a duplicate.
- **Fail open.** If the API is unavailable or returns an error, the workflow does not fail; a warning is written to the logs instead.

## Project structure

```
.github/workflows/terraform-ai-review.yml   workflow that runs the action on this repo
action.yml                                  action definition (inputs and steps)
scripts/ai_review.py                        review logic
scripts/requirements.txt                    Python dependencies
terraform/main.tf                           demo infrastructure (S3 bucket)
LICENSE                                     MIT license
examples/risky-change.tf                    intentionally insecure resources for testing
```

## Requirements

- A GitHub repository.
- An Anthropic API key with credit on the account (console.anthropic.com). API usage is billed separately from a Claude.ai subscription.
- No AWS account is needed: the demo provider uses mock credentials, so `terraform plan` runs without access to AWS.

## Getting started

### 1. Create an API key

In console.anthropic.com, open API Keys, click Create Key, and copy the key right away (it starts with `sk-ant-`). An existing key cannot be viewed again; you can only create a new one. Add a few dollars of credit under Billing.

### 2. Push the code to a repository

```bash
git init -b main
git add .
git commit -m "init tf-ai-review"
git remote add origin git@github.com:<username>/tf-ai-review.git
git push -u origin main
```

The `.github/workflows` folder must be at the repository root, otherwise GitHub will not pick up the workflow. To check that it was committed: `git ls-files | grep workflows`.

### 3. Configure the repository

- Settings -> Secrets and variables -> Actions -> New repository secret. Name: `ANTHROPIC_API_KEY`, value: the key from step 1. You do not need to add `GITHUB_TOKEN`; GitHub provides it automatically.
- Settings -> Actions -> General -> Workflow permissions -> Read and write permissions. Without this, the bot cannot post comments (error 403).

### 4. Test it with a pull request

```bash
git checkout -b test/risky
cp examples/risky-change.tf terraform/
git add terraform/risky-change.tf
git commit -m "add bastion, iam policy and rds"
git push -u origin test/risky
```

On GitHub, open a pull request from `test/risky` into `main`. The workflow runs only on pull requests and only when files in the `terraform/` folder change. Within about a minute, a comment from `github-actions` appears on the PR. Expected findings: SSH open to 0.0.0.0/0, an IAM policy with `"*"`, a public unencrypted database, a hardcoded password, and an expensive instance type.

To check that the comment updates, fix something in `terraform/risky-change.tf` (for example, `publicly_accessible = false`) and push to the same branch. The comment is updated and the fixed finding disappears.

### 5. Close the test PR without merging

The test PR should not be merged. It exists only to check the review, and `risky-change.tf` contains intentionally insecure resources. After a merge they would end up in `main` and show up in the plan of every following PR. In a real project connected to AWS, merging and then running `apply` would actually create these resources.

Steps: click Close pull request, then Delete branch.

If files needed in `main` (for example, `scripts/ai_review.py`) were changed in the test branch, move them over separately:

```bash
git checkout main
git checkout test/risky -- scripts/ai_review.py
git commit -m "update ai_review.py"
git push
```

## Running locally

Useful for experimenting with the prompt: without GitHub environment variables, the script prints the review to the console.

```bash
cd terraform
terraform init
terraform plan -out=tfplan
terraform show -json tfplan > ../plan.json
cd ..
git diff main -- terraform > pr.diff
pip install -r scripts/requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python scripts/ai_review.py
```

## Configuration

All settings are passed as action inputs (see the Inputs table above). For cheaper and faster reviews of simple PRs, set `model: claude-haiku-4-5-20251001`.

To block merging PRs with critical issues: set `fail-on: critical`, then in Settings -> Branches add a rule for `main` that requires the `review` check to pass.

## Connecting a real AWS account via OIDC

Instead of long-lived keys stored as secrets, the workflow gets temporary credentials through an IAM role.

1. In AWS IAM, add the identity provider `token.actions.githubusercontent.com` with audience `sts.amazonaws.com`.
2. Create a role with a trust policy for your repository (`repo:<username>/tf-ai-review:pull_request`) and read-only permissions: `ReadOnlyAccess` plus access to the state bucket is enough for `plan`.
3. Add the `AWS_ROLE_ARN` secret, add `id-token: write` to the workflow `permissions`, and add this step after checkout, before the review action:
   ```yaml
   - uses: aws-actions/configure-aws-credentials@v4
     with:
       role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
       aws-region: eu-central-1
   ```
4. Remove the mock credentials from the `provider "aws"` block and add a `backend "s3"`.

## Limitations

- Without remote state, every plan builds the infrastructure from scratch, so all resources, including those already in `main`, appear as `create`. A realistic review requires an S3 backend.
- The demo provider mode only supports creating resources and does not support data sources.
- The plan and diff are sent to an external API. Before using this on work or client repositories, get approval from your team and security.
- Only values that Terraform marks as sensitive are masked. A secret hardcoded in a `.tf` file will still appear in the diff (and the review will flag it).
- Pull requests from forks do not get access to secrets, so the review does not run for them. Do not switch to `pull_request_target`; it is unsafe.
- The review is advisory and does not replace human review.

## Ideas for further development

- Terragrunt support: `terragrunt run-all plan` and collecting JSON plans from all modules.
- Inline comments on specific lines via the GitHub Review API.
- Combining with checkov or tfsec: deterministic checks plus Claude for explanations and context.
- Prompt caching for the system prompt when PRs are frequent.
- A set of test PRs with known issues to evaluate review quality after prompt changes.
