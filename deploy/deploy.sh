#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# LifeShot Deploy (Detector + API Handler + Layer + DDB + URLs + Upload Images)
# ============================================================
# Requires: aws cli v2 configured (aws configure / env creds)
# Local zip files expected next to this script:
#   pillow311.zip
#   detector_logic.zip
#   api_handler.zip
#
# Images folder expected (optional but recommended for end-to-end):
#   images/Test1/*.png|jpg
#   images/Test2/*.png|jpg
#
# Optional env overrides (examples):
#   AWS_REGION=us-east-1
#   SNS_TOPIC_ARN=arn:aws:sns:...
#   CREATE_BUCKET=YES
#   CREATE_DDB_TABLE=YES
#   CREATE_FUNCTION_URLS=YES
#   URL_AUTH_TYPE=NONE
#   SANITY_CHECK=YES
#   UPLOAD_IMAGES=YES
# ============================================================

# ========= CONFIG (defaults) =========
REGION="${AWS_REGION:-us-east-1}"
ARCH="${ARCH:-x86_64}"
RUNTIME="${RUNTIME:-python3.11}"

# Names
DETECTOR_LAMBDA_NAME="${DETECTOR_LAMBDA_NAME:-LifeShot_detector_logic}"
API_LAMBDA_NAME="${API_LAMBDA_NAME:-LifeShot_Api_Handler}"
LAYER_NAME="${LAYER_NAME:-pillow-python311}"
ROLE_NAME="${ROLE_NAME:-LifeShotLambdaRole}"

# Resources
BUCKET_NAME="${BUCKET_NAME:-lifeshot-pool-images}"
EVENTS_TABLE_NAME="${EVENTS_TABLE_NAME:-LifeShot_Events}"

# Behavior
SNS_TOPIC_ARN="${SNS_TOPIC_ARN:-}"                  # optional
CREATE_BUCKET="${CREATE_BUCKET:-YES}"               # YES/NO
CREATE_DDB_TABLE="${CREATE_DDB_TABLE:-YES}"         # YES/NO
CREATE_FUNCTION_URLS="${CREATE_FUNCTION_URLS:-YES}" # YES/NO
URL_AUTH_TYPE="${URL_AUTH_TYPE:-NONE}"              # NONE or AWS_IAM
SANITY_CHECK="${SANITY_CHECK:-YES}"                 # YES/NO
UPLOAD_IMAGES="${UPLOAD_IMAGES:-YES}"               # YES/NO

# Lambda settings
DETECTOR_TIMEOUT="${DETECTOR_TIMEOUT:-180}"         # seconds
API_TIMEOUT="${API_TIMEOUT:-30}"
MEMORY="${MEMORY:-512}"

# API handler env (presign)
PRESIGN_EXPIRES="${PRESIGN_EXPIRES:-900}"

# Handlers (module.function)
DETECTOR_HANDLER="${DETECTOR_HANDLER:-detector_logic.lambda_handler}"
API_HANDLER="${API_HANDLER:-api_handler.lambda_handler}"

# Local files (relative)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PILLOW_ZIP="$SCRIPT_DIR/pillow311.zip"
DETECTOR_ZIP="$SCRIPT_DIR/detector_logic.zip"
API_ZIP="$SCRIPT_DIR/api_handler.zip"

# Images folder (relative)
IMAGES_DIR="${IMAGES_DIR:-$SCRIPT_DIR/images}"
TEST1_DIR="$IMAGES_DIR/Test1"
TEST2_DIR="$IMAGES_DIR/Test2"

# S3 prefixes
S3_TEST1_PREFIX="${S3_TEST1_PREFIX:-LifeShot/Test1/}"
S3_TEST2_PREFIX="${S3_TEST2_PREFIX:-LifeShot/Test2/}"

# ========= helpers =========
log() { echo "[$(date +'%H:%M:%S')] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
need_file() { test -f "$1" || die "Missing file: $1"; }
need_dir() { test -d "$1" || die "Missing folder: $1"; }

# ========= sanity pre-checks =========
aws sts get-caller-identity --region "$REGION" >/dev/null
need_file "$PILLOW_ZIP"
need_file "$DETECTOR_ZIP"
need_file "$API_ZIP"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "$REGION")"
log "Deploying to account: $ACCOUNT_ID | region: $REGION | arch: $ARCH | runtime: $RUNTIME"

# ========= (optional) create S3 bucket =========
if [[ "${CREATE_BUCKET^^}" == "YES" ]]; then
  log "Ensuring S3 bucket exists: $BUCKET_NAME"
  if aws s3api head-bucket --bucket "$BUCKET_NAME" --region "$REGION" 2>/dev/null; then
    log "S3 bucket exists: $BUCKET_NAME"
  else
    log "Creating S3 bucket: $BUCKET_NAME"
    if [[ "$REGION" == "us-east-1" ]]; then
      aws s3api create-bucket --bucket "$BUCKET_NAME" --region "$REGION" >/dev/null
    else
      aws s3api create-bucket --bucket "$BUCKET_NAME" --region "$REGION" \
        --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
    fi

    # Block public access (recommended)
    aws s3api put-public-access-block \
      --bucket "$BUCKET_NAME" \
      --region "$REGION" \
      --public-access-block-configuration \
"BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" >/dev/null || true

    log "Created S3 bucket: $BUCKET_NAME"
  fi
else
  log "CREATE_BUCKET=NO (skipping S3 bucket creation)"
fi

# ========= (optional) upload images =========
if [[ "${UPLOAD_IMAGES^^}" == "YES" ]]; then
  log "Uploading images to S3..."
  need_dir "$IMAGES_DIR"
  need_dir "$TEST1_DIR"
  need_dir "$TEST2_DIR"

  log "Sync Test1 -> s3://$BUCKET_NAME/$S3_TEST1_PREFIX"
  aws s3 sync "$TEST1_DIR" "s3://$BUCKET_NAME/$S3_TEST1_PREFIX" --only-show-errors --region "$REGION"

  log "Sync Test2 -> s3://$BUCKET_NAME/$S3_TEST2_PREFIX"
  aws s3 sync "$TEST2_DIR" "s3://$BUCKET_NAME/$S3_TEST2_PREFIX" --only-show-errors --region "$REGION"

  log "Images uploaded."
else
  log "UPLOAD_IMAGES=NO (skipping images upload)"
fi

# ========= (optional) create DynamoDB table =========
if [[ "${CREATE_DDB_TABLE^^}" == "YES" ]]; then
  log "Ensuring DynamoDB table exists: $EVENTS_TABLE_NAME"
  if aws dynamodb describe-table --table-name "$EVENTS_TABLE_NAME" --region "$REGION" >/dev/null 2>&1; then
    log "DynamoDB table exists: $EVENTS_TABLE_NAME"
  else
    log "Creating DynamoDB table: $EVENTS_TABLE_NAME (PK: eventId [S])"
    aws dynamodb create-table \
      --region "$REGION" \
      --table-name "$EVENTS_TABLE_NAME" \
      --attribute-definitions AttributeName=eventId,AttributeType=S \
      --key-schema AttributeName=eventId,KeyType=HASH \
      --billing-mode PAY_PER_REQUEST >/dev/null

    log "Waiting for DynamoDB table to become ACTIVE..."
    aws dynamodb wait table-exists --table-name "$EVENTS_TABLE_NAME" --region "$REGION"
    log "DynamoDB table created: $EVENTS_TABLE_NAME"
  fi
else
  log "CREATE_DDB_TABLE=NO (skipping DynamoDB table creation)"
fi

# ========= create IAM role (trust policy) =========
cat > /tmp/lambda-trust.json <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "lambda.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
JSON

ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text 2>/dev/null || true)"
if [[ -z "${ROLE_ARN}" || "${ROLE_ARN}" == "None" ]]; then
  log "Creating IAM role: $ROLE_NAME"
  ROLE_ARN="$(aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document file:///tmp/lambda-trust.json \
    --query Role.Arn --output text)"
else
  log "IAM role exists: $ROLE_ARN"
fi

# Attach AWS managed basic logging
aws iam attach-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole >/dev/null || true

# Inline policy for required services
cat > /tmp/lifeshot-inline-policy.json <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "S3Access",
      "Effect": "Allow",
      "Action": ["s3:GetObject","s3:PutObject","s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::$BUCKET_NAME",
        "arn:aws:s3:::$BUCKET_NAME/*"
      ]
    },
    {
      "Sid": "Rekognition",
      "Effect": "Allow",
      "Action": ["rekognition:DetectLabels"],
      "Resource": "*"
    },
    {
      "Sid": "DynamoDB",
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem","dynamodb:GetItem","dynamodb:UpdateItem","dynamodb:Query","dynamodb:Scan"],
      "Resource": "arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/$EVENTS_TABLE_NAME"
    },
    {
      "Sid": "SNSPublish",
      "Effect": "Allow",
      "Action": ["sns:Publish"],
      "Resource": "${SNS_TOPIC_ARN:-*}"
    }
  ]
}
JSON

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name LifeShotInlinePolicy \
  --policy-document file:///tmp/lifeshot-inline-policy.json >/dev/null

log "Waiting for IAM propagation..."
sleep 10

# ========= publish layer =========
log "Publishing Layer: $LAYER_NAME"
LAYER_VERSION_ARN="$(aws lambda publish-layer-version \
  --region "$REGION" \
  --layer-name "$LAYER_NAME" \
  --compatible-runtimes "$RUNTIME" \
  --compatible-architectures "$ARCH" \
  --zip-file "fileb://$PILLOW_ZIP" \
  --query LayerVersionArn --output text)"

log "LayerVersionArn: $LAYER_VERSION_ARN"

# ========= create/update Detector lambda =========
log "Deploying Detector Lambda: $DETECTOR_LAMBDA_NAME"
DETECTOR_ARN="$(aws lambda get-function --region "$REGION" --function-name "$DETECTOR_LAMBDA_NAME" --query Configuration.FunctionArn --output text 2>/dev/null || true)"

if [[ -z "${DETECTOR_ARN}" || "${DETECTOR_ARN}" == "None" ]]; then
  DETECTOR_ARN="$(aws lambda create-function \
    --region "$REGION" \
    --function-name "$DETECTOR_LAMBDA_NAME" \
    --runtime "$RUNTIME" \
    --architectures "$ARCH" \
    --role "$ROLE_ARN" \
    --handler "$DETECTOR_HANDLER" \
    --timeout "$DETECTOR_TIMEOUT" \
    --memory-size "$MEMORY" \
    --zip-file "fileb://$DETECTOR_ZIP" \
    --environment "Variables={FRAMES_BUCKET=$BUCKET_NAME,EVENTS_TABLE_NAME=$EVENTS_TABLE_NAME,SNS_TOPIC_ARN=$SNS_TOPIC_ARN}" \
    --query FunctionArn --output text)"
else
  aws lambda update-function-code \
    --region "$REGION" \
    --function-name "$DETECTOR_LAMBDA_NAME" \
    --zip-file "fileb://$DETECTOR_ZIP" >/dev/null

  aws lambda update-function-configuration \
    --region "$REGION" \
    --function-name "$DETECTOR_LAMBDA_NAME" \
    --runtime "$RUNTIME" \
    --architectures "$ARCH" \
    --role "$ROLE_ARN" \
    --handler "$DETECTOR_HANDLER" \
    --timeout "$DETECTOR_TIMEOUT" \
    --memory-size "$MEMORY" \
    --environment "Variables={FRAMES_BUCKET=$BUCKET_NAME,EVENTS_TABLE_NAME=$EVENTS_TABLE_NAME,SNS_TOPIC_ARN=$SNS_TOPIC_ARN}" >/dev/null
fi

# Attach layer to Detector
aws lambda update-function-configuration \
  --region "$REGION" \
  --function-name "$DETECTOR_LAMBDA_NAME" \
  --layers "$LAYER_VERSION_ARN" >/dev/null

log "Detector Lambda ready."

# ========= create/update API Handler lambda =========
log "Deploying API Handler Lambda: $API_LAMBDA_NAME"
API_ARN="$(aws lambda get-function --region "$REGION" --function-name "$API_LAMBDA_NAME" --query Configuration.FunctionArn --output text 2>/dev/null || true)"

if [[ -z "${API_ARN}" || "${API_ARN}" == "None" ]]; then
  API_ARN="$(aws lambda create-function \
    --region "$REGION" \
    --function-name "$API_LAMBDA_NAME" \
    --runtime "$RUNTIME" \
    --architectures "$ARCH" \
    --role "$ROLE_ARN" \
    --handler "$API_HANDLER" \
    --timeout "$API_TIMEOUT" \
    --memory-size "$MEMORY" \
    --zip-file "fileb://$API_ZIP" \
    --environment "Variables={EVENTS_TABLE_NAME=$EVENTS_TABLE_NAME,IMAGES_BUCKET=$BUCKET_NAME,PRESIGN_EXPIRES=$PRESIGN_EXPIRES}" \
    --query FunctionArn --output text)"
else
  aws lambda update-function-code \
    --region "$REGION" \
    --function-name "$API_LAMBDA_NAME" \
    --zip-file "fileb://$API_ZIP" >/dev/null

  aws lambda update-function-configuration \
    --region "$REGION" \
    --function-name "$API_LAMBDA_NAME" \
    --runtime "$RUNTIME" \
    --architectures "$ARCH" \
    --role "$ROLE_ARN" \
    --handler "$API_HANDLER" \
    --timeout "$API_TIMEOUT" \
    --memory-size "$MEMORY" \
    --environment "Variables={EVENTS_TABLE_NAME=$EVENTS_TABLE_NAME,IMAGES_BUCKET=$BUCKET_NAME,PRESIGN_EXPIRES=$PRESIGN_EXPIRES}" >/dev/null
fi

log "API Handler Lambda ready."

# ========= Function URLs (Auth NONE by default) =========
DETECTOR_URL=""
API_URL=""

if [[ "${CREATE_FUNCTION_URLS^^}" == "YES" ]]; then
  log "Ensuring Function URLs (auth: $URL_AUTH_TYPE)"

  # Detector URL
  DETECTOR_URL="$(aws lambda get-function-url-config --region "$REGION" --function-name "$DETECTOR_LAMBDA_NAME" --query FunctionUrl --output text 2>/dev/null || true)"
  if [[ -z "${DETECTOR_URL}" || "${DETECTOR_URL}" == "None" ]]; then
    DETECTOR_URL="$(aws lambda create-function-url-config \
      --region "$REGION" \
      --function-name "$DETECTOR_LAMBDA_NAME" \
      --auth-type "$URL_AUTH_TYPE" \
      --cors 'AllowOrigins=["*"],AllowMethods=["*"],AllowHeaders=["*"]' \
      --query FunctionUrl --output text)"
  fi

  if [[ "${URL_AUTH_TYPE}" == "NONE" ]]; then
    aws lambda add-permission \
      --region "$REGION" \
      --function-name "$DETECTOR_LAMBDA_NAME" \
      --statement-id FunctionUrlPublicAccessDetector \
      --action lambda:InvokeFunctionUrl \
      --principal "*" \
      --function-url-auth-type NONE >/dev/null 2>&1 || true
  fi

  # API URL
  API_URL="$(aws lambda get-function-url-config --region "$REGION" --function-name "$API_LAMBDA_NAME" --query FunctionUrl --output text 2>/dev/null || true)"
  if [[ -z "${API_URL}" || "${API_URL}" == "None" ]]; then
    API_URL="$(aws lambda create-function-url-config \
      --region "$REGION" \
      --function-name "$API_LAMBDA_NAME" \
      --auth-type "$URL_AUTH_TYPE" \
      --cors 'AllowOrigins=["*"],AllowMethods=["*"],AllowHeaders=["*"]' \
      --query FunctionUrl --output text)"
  fi

  if [[ "${URL_AUTH_TYPE}" == "NONE" ]]; then
    aws lambda add-permission \
      --region "$REGION" \
      --function-name "$API_LAMBDA_NAME" \
      --statement-id FunctionUrlPublicAccessApi \
      --action lambda:InvokeFunctionUrl \
      --principal "*" \
      --function-url-auth-type NONE >/dev/null 2>&1 || true
  fi

else
  log "CREATE_FUNCTION_URLS=NO (skipping)"
fi

# ========= SANITY CHECK =========
if [[ "${SANITY_CHECK^^}" == "YES" ]]; then
  log "Running SANITY checks..."

  # Detector sanity (prints tail logs)
  log "Sanity: invoke Detector ($DETECTOR_LAMBDA_NAME)"
  aws lambda invoke \
    --region "$REGION" \
    --function-name "$DETECTOR_LAMBDA_NAME" \
    --payload "{\"prefix\":\"$S3_TEST1_PREFIX\",\"max_frames\":1}" \
    /tmp/detector_out.json \
    --cli-binary-format raw-in-base64-out \
    --log-type Tail \
    --query 'LogResult' \
    --output text | base64 --decode || true

  log "Detector response:"
  cat /tmp/detector_out.json || true
  echo

  # API sanity: GET /events
  log "Sanity: invoke API Handler ($API_LAMBDA_NAME) - GET /events"
  aws lambda invoke \
    --region "$REGION" \
    --function-name "$API_LAMBDA_NAME" \
    --payload '{"rawPath":"/events","requestContext":{"http":{"method":"GET","path":"/events"}}}' \
    /tmp/api_out.json \
    --cli-binary-format raw-in-base64-out \
    --log-type Tail \
    --query 'LogResult' \
    --output text | base64 --decode || true

  log "API response:"
  cat /tmp/api_out.json || true
  echo

  # Optional: if API URL public, do curl sanity
  if [[ "${CREATE_FUNCTION_URLS^^}" == "YES" && "${URL_AUTH_TYPE}" == "NONE" && -n "${API_URL}" && "${API_URL}" != "None" ]]; then
    log "Sanity: curl API URL GET /events"
    curl -sS "${API_URL}events" | head -c 2000 || true
    echo
  fi

  log "SANITY checks finished."
else
  log "SANITY_CHECK=NO (skipping)"
fi

# ========= Summary =========
echo "=============================="
echo "DONE."
echo "Region: $REGION"
echo "Arch: $ARCH | Runtime: $RUNTIME"
echo "S3 Bucket: $BUCKET_NAME"
echo "Uploaded images: $UPLOAD_IMAGES | Test1=>$S3_TEST1_PREFIX | Test2=>$S3_TEST2_PREFIX"
echo "DynamoDB Table: $EVENTS_TABLE_NAME (create: $CREATE_DDB_TABLE)"
echo "Role: $ROLE_NAME"
echo "Layer: $LAYER_NAME -> $LAYER_VERSION_ARN"
echo "Detector: $DETECTOR_LAMBDA_NAME (handler: $DETECTOR_HANDLER, timeout: $DETECTOR_TIMEOUT)"
echo "API:      $API_LAMBDA_NAME (handler: $API_HANDLER, timeout: $API_TIMEOUT)"
if [[ -n "${DETECTOR_URL}" && "${DETECTOR_URL}" != "None" ]]; then
  echo "Detector Function URL: $DETECTOR_URL"
fi
if [[ -n "${API_URL}" && "${API_URL}" != "None" ]]; then
  echo "API Function URL:      $API_URL"
fi
echo "=============================="
