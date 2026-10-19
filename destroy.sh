#!/usr/bin/env bash
# Deletes everything this project created, so nothing keeps running.
set -euo pipefail
REGION="us-west-2"
STACK="${STACK_NAME:-chennai-lake-watch}"

SITE_BUCKET=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='SiteBucketName'].OutputValue" --output text 2>/dev/null || true)

read -r -p "Delete stack '$STACK' and all its data in $REGION? Type yes: " ANSWER
[[ "$ANSWER" == "yes" ]] || { echo "Cancelled."; exit 0; }

if [[ -n "$SITE_BUCKET" && "$SITE_BUCKET" != "None" ]]; then
  echo "Emptying website bucket $SITE_BUCKET"
  aws s3 rm "s3://$SITE_BUCKET" --recursive >/dev/null
fi
echo "Deleting stack (CloudFront takes a few minutes to remove)"
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ART_BUCKET="lake-watch-artifacts-${ACCOUNT}-${REGION}"
if aws s3api head-bucket --bucket "$ART_BUCKET" 2>/dev/null; then
  aws s3 rb "s3://$ART_BUCKET" --force >/dev/null
fi
echo "Everything is deleted."
