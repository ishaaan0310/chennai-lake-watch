#!/usr/bin/env bash
# Deploys Chennai Lake Watch. Built for AWS CloudShell (nothing to install).
#   ./deploy.sh you@example.com            deploy + upload website
#   ./deploy.sh you@example.com --backfill also load the last year of history
set -euo pipefail

EMAIL="${1:-}"
BACKFILL="${2:-}"
if [[ -z "$EMAIL" || "$EMAIL" != *@* ]]; then
  echo "Usage: ./deploy.sh you@example.com [--backfill]" >&2
  exit 1
fi

REGION="us-west-2"   # the Sentinel-2 imagery lives here: fast and no transfer charges
STACK="${STACK_NAME:-chennai-lake-watch}"
cd "$(dirname "$0")"

echo "==> 1/5 Checking AWS access"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
echo "    account $ACCOUNT, region $REGION, stack $STACK"

echo "==> 2/5 Building the Lambda package (Linux wheels for Python 3.12)"
rm -rf build && mkdir build
python3 -m pip install --quiet --disable-pip-version-check \
  --target build --platform manylinux2014_x86_64 --implementation cp \
  --python-version 3.12 --only-binary=:all: -r lambda/requirements.txt
cp lambda/handler.py build/
# Trim files Lambda doesn't need.
find build -type d \( -name "__pycache__" -o -name "tests" \) -prune -exec rm -rf {} +
rm -rf build/bin
du -sh build | awk '{print "    package size " $1}'

echo "==> 3/5 Uploading code"
ART_BUCKET="lake-watch-artifacts-${ACCOUNT}-${REGION}"
if ! aws s3api head-bucket --bucket "$ART_BUCKET" 2>/dev/null; then
  aws s3api create-bucket --bucket "$ART_BUCKET" --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  aws s3api put-public-access-block --bucket "$ART_BUCKET" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  # Old code packages are deleted after 7 days.
  aws s3api put-bucket-lifecycle-configuration --bucket "$ART_BUCKET" \
    --lifecycle-configuration '{"Rules":[{"ID":"expire","Status":"Enabled","Filter":{},"Expiration":{"Days":7}}]}'
fi
aws cloudformation package --region "$REGION" --template-file template.yaml \
  --s3-bucket "$ART_BUCKET" --output-template-file packaged.yaml >/dev/null

echo "==> 4/5 Deploying the stack (first time takes 5-10 minutes: CloudFront is slow to create)"
aws cloudformation deploy --region "$REGION" --stack-name "$STACK" \
  --template-file packaged.yaml \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides AlertEmail="$EMAIL" \
  --no-fail-on-empty-changeset

output() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
SITE_BUCKET=$(output SiteBucketName)
SITE_URL=$(output WebsiteUrl)
STATE_MACHINE=$(output StateMachineArn)

echo "==> 5/5 Uploading the website"
aws s3 cp website/index.html "s3://$SITE_BUCKET/index.html" \
  --content-type "text/html; charset=utf-8" --cache-control "max-age=300" >/dev/null

if [[ "$BACKFILL" == "--backfill" ]]; then
  echo "==> Starting a one-year backfill (runs in the background, ~10-15 minutes)"
  aws stepfunctions start-execution --region "$REGION" --state-machine-arn "$STATE_MACHINE" \
    --name "backfill-$(date +%Y%m%d-%H%M%S)" --input '{"backfill_days": 365}' >/dev/null
fi

cat <<EOF

Done!
  Website:   $SITE_URL
  Workflow:  https://${REGION}.console.aws.amazon.com/states/home?region=${REGION}#/statemachines/view/${STATE_MACHINE}

Next:
  - Check $EMAIL and click "Confirm subscription" in the email from AWS Notifications.
  - Watch the workflow at the link above; the website fills in when it finishes.
EOF
