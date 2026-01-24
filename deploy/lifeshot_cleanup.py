#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""LifeShot cleanup script.

Cosmetic refactor only:
- Improves readability via spacing, section headers, and comments.
- Does not change functionality or behavior.
"""

import os
import sys
import time
import json
import boto3
from botocore.exceptions import ClientError


# =============================================================================
# Configuration (env overrides)
# =============================================================================
REGION = os.getenv("AWS_REGION", "us-east-1")

# Delete only things matching these prefixes (change if needed)
STACK_PREFIX = os.getenv("STACK_PREFIX", "LifeShot")  # for Lambda/API/Cognito/Dynamo/SNS naming
S3_BUCKET_PREFIXES = [
    p.strip()
    for p in os.getenv("S3_BUCKET_PREFIXES", "lifeshot,lifeshotweb").split(",")
    if p.strip()
]

# Safety switch: start in dry-run mode
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes", "y")

# IAM cleanup (do NOT touch LabRole)
IAM_ROLE_TO_DELETE = os.getenv("IAM_ROLE_TO_DELETE", "LifeShotLambdaRole")
IAM_INLINE_POLICY_TO_DELETE = os.getenv("IAM_INLINE_POLICY_TO_DELETE", "LifeShotDataAccess")


# =============================================================================
# Small helpers
# =============================================================================


# Log a message to stdout.
def log(msg: str) -> None:
    print(msg)


# Print an error to stderr and exit.
def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# Create a boto3 client pinned to the configured region.
def safe_client(service: str):
    return boto3.client(service, region_name=REGION)


# Return True if a resource name matches the configured STACK_PREFIX.
def should_delete_name(name: str) -> bool:
    return name.startswith(STACK_PREFIX)


# Return True if an S3 bucket name matches one of the allowed deletion prefixes.
def should_delete_bucket(bucket: str) -> bool:
    b = bucket.lower()
    return any(b.startswith(p.lower()) for p in S3_BUCKET_PREFIXES)


# Call an AWS API function, optionally ignoring specific error codes.
def try_call(fn, *, ignore_codes=()):
    try:
        return fn()
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ignore_codes:
            return None
        raise


# =============================================================================
# Cleanup steps
# =============================================================================


# Delete API Gateway v2 HTTP APIs that match STACK_PREFIX.
def delete_apigw_http_apis():
    apigw = safe_client("apigatewayv2")
    log("\n== API Gateway v2 (HTTP APIs) ==")
    apis = apigw.get_apis().get("Items", [])
    for a in apis:
        name = a.get("Name", "")
        if not (a.get("ProtocolType") == "HTTP" and should_delete_name(name)):
            continue
        api_id = a["ApiId"]
        log(f"- Delete API: {name} ({api_id})")
        if not DRY_RUN:
            try_call(lambda: apigw.delete_api(ApiId=api_id))


# Delete Lambda functions (and their Function URLs) and matching Lambda layers.
def delete_lambda_functions_and_layers():
    lam = safe_client("lambda")
    log("\n== Lambda functions ==")
    paginator = lam.get_paginator("list_functions")
    for page in paginator.paginate():
        for fn in page.get("Functions", []):
            name = fn["FunctionName"]
            if not should_delete_name(name):
                continue
            log(f"- Delete Lambda: {name}")

            if DRY_RUN:
                continue

            # If function URL exists, delete it first (best-effort)
            try:
                lam.get_function_url_config(FunctionName=name)
                try_call(
                    lambda: lam.delete_function_url_config(FunctionName=name),
                    ignore_codes=("ResourceNotFoundException",),
                )
            except ClientError:
                pass

            try_call(
                lambda: lam.delete_function(FunctionName=name),
                ignore_codes=("ResourceNotFoundException",),
            )

    log("\n== Lambda layers ==")
    # Delete layers that start with STACK_PREFIX or explicit LifeShot-Pillow
    paginator2 = lam.get_paginator("list_layers")
    for page in paginator2.paginate():
        for layer in page.get("Layers", []):
            layer_name = layer.get("LayerName", "")
            if not (layer_name.startswith(STACK_PREFIX) or layer_name == "LifeShot-Pillow"):
                continue

            log(f"- Delete Layer: {layer_name}")
            if DRY_RUN:
                continue

            # Delete all versions
            vers = lam.list_layer_versions(LayerName=layer_name).get("LayerVersions", [])
            for v in vers:
                ver = v["Version"]
                log(f"  - Delete version {ver}")
                try_call(
                    lambda: lam.delete_layer_version(
                        LayerName=layer_name, VersionNumber=ver
                    ),
                    ignore_codes=("ResourceNotFoundException",),
                )


# Delete CloudWatch log groups for LifeShot Lambdas.
def delete_cloudwatch_logs():
    logs = safe_client("logs")
    log("\n== CloudWatch Log Groups ==")
    paginator = logs.get_paginator("describe_log_groups")
    for page in paginator.paginate(logGroupNamePrefix="/aws/lambda/"):
        for g in page.get("logGroups", []):
            name = g.get("logGroupName", "")
            # only delete LifeShot lambdas logs
            if not name.startswith(f"/aws/lambda/{STACK_PREFIX}"):
                continue
            log(f"- Delete LogGroup: {name}")
            if not DRY_RUN:
                try_call(
                    lambda: logs.delete_log_group(logGroupName=name),
                    ignore_codes=("ResourceNotFoundException",),
                )


# Delete DynamoDB tables that match STACK_PREFIX.
def delete_dynamodb_tables():
    ddb = safe_client("dynamodb")
    log("\n== DynamoDB tables ==")
    paginator = ddb.get_paginator("list_tables")
    for page in paginator.paginate():
        for t in page.get("TableNames", []):
            if not should_delete_name(t):
                continue
            log(f"- Delete table: {t}")
            if DRY_RUN:
                continue
            try_call(
                lambda: ddb.delete_table(TableName=t),
                ignore_codes=("ResourceNotFoundException",),
            )


# Delete SNS topics that match STACK_PREFIX.
def delete_sns_topics():
    sns = safe_client("sns")
    log("\n== SNS topics ==")
    paginator = sns.get_paginator("list_topics")
    for page in paginator.paginate():
        for t in page.get("Topics", []):
            arn = t["TopicArn"]
            # TopicArn ends with name
            name = arn.split(":")[-1]
            if not should_delete_name(name):
                continue
            log(f"- Delete topic: {name} ({arn})")
            if DRY_RUN:
                continue
            try_call(lambda: sns.delete_topic(TopicArn=arn))


# Delete Cognito user pools that match STACK_PREFIX.
def delete_cognito_user_pools():
    cognito = safe_client("cognito-idp")
    log("\n== Cognito user pools ==")
    pools = cognito.list_user_pools(MaxResults=60).get("UserPools", [])
    for p in pools:
        name = p.get("Name", "")
        if not should_delete_name(name):
            continue
        pool_id = p["Id"]
        log(f"- Delete user pool: {name} ({pool_id})")
        if not DRY_RUN:
            try_call(
                lambda: cognito.delete_user_pool(UserPoolId=pool_id),
                ignore_codes=("ResourceNotFoundException",),
            )


# Empty matching S3 buckets (including versions) and delete them.
def empty_and_delete_s3_buckets():
    s3 = safe_client("s3")
    log("\n== S3 buckets ==")
    buckets = s3.list_buckets().get("Buckets", [])
    for b in buckets:
        name = b["Name"]
        if not should_delete_bucket(name):
            continue

        log(f"- Empty + delete bucket: {name}")
        if DRY_RUN:
            continue

        # Empty bucket (including versions if versioned)
        try:
            ver = s3.get_bucket_versioning(Bucket=name).get("Status", "")
            is_versioned = ver == "Enabled"
        except ClientError:
            is_versioned = False

        if is_versioned:
            # Delete all versions + delete markers
            key_marker = None
            ver_marker = None
            while True:
                resp = s3.list_object_versions(
                    Bucket=name, KeyMarker=key_marker, VersionIdMarker=ver_marker
                )
                objs = []
                for v in resp.get("Versions", []):
                    objs.append({"Key": v["Key"], "VersionId": v["VersionId"]})
                for m in resp.get("DeleteMarkers", []):
                    objs.append({"Key": m["Key"], "VersionId": m["VersionId"]})

                if objs:
                    # batch delete in chunks
                    for i in range(0, len(objs), 1000):
                        chunk = objs[i : i + 1000]
                        s3.delete_objects(
                            Bucket=name,
                            Delete={"Objects": chunk, "Quiet": True},
                        )

                if not resp.get("IsTruncated"):
                    break
                key_marker = resp.get("NextKeyMarker")
                ver_marker = resp.get("NextVersionIdMarker")
        else:
            # Non-versioned
            token = None
            while True:
                kwargs = {"Bucket": name, "MaxKeys": 1000}
                if token:
                    kwargs["ContinuationToken"] = token
                resp = s3.list_objects_v2(**kwargs)
                contents = resp.get("Contents", []) or []
                if contents:
                    objs = [{"Key": o["Key"]} for o in contents]
                    for i in range(0, len(objs), 1000):
                        chunk = objs[i : i + 1000]
                        s3.delete_objects(
                            Bucket=name,
                            Delete={"Objects": chunk, "Quiet": True},
                        )

                if not resp.get("IsTruncated"):
                    break
                token = resp.get("NextContinuationToken")

        # Now delete bucket
        try_call(lambda: s3.delete_bucket(Bucket=name))


    # Cleanup project IAM role/policies (explicitly does not touch LabRole).
def cleanup_iam():
    iam = safe_client("iam")
    log("\n== IAM (project role/policy) ==")

    # DO NOT delete LabRole
    if IAM_ROLE_TO_DELETE.lower() == "labrole":
        log("- Skipping LabRole (safety).")
        return

    # Remove inline policy if exists
    log(f"- Remove inline policy '{IAM_INLINE_POLICY_TO_DELETE}' from role '{IAM_ROLE_TO_DELETE}' (if exists)")
    if not DRY_RUN:
        try:
            try_call(
                lambda: iam.delete_role_policy(
                    RoleName=IAM_ROLE_TO_DELETE,
                    PolicyName=IAM_INLINE_POLICY_TO_DELETE,
                ),
                ignore_codes=("NoSuchEntity",),
            )
        except ClientError as e:
            log(f"  (warn) {e}")

    # Detach managed policies
    log(f"- Detach managed policies from role '{IAM_ROLE_TO_DELETE}' (if exists)")
    if not DRY_RUN:
        try:
            resp = iam.list_attached_role_policies(RoleName=IAM_ROLE_TO_DELETE)
            for ap in resp.get("AttachedPolicies", []):
                arn = ap["PolicyArn"]
                log(f"  - Detach {arn}")
                try_call(
                    lambda: iam.detach_role_policy(
                        RoleName=IAM_ROLE_TO_DELETE,
                        PolicyArn=arn,
                    ),
                    ignore_codes=("NoSuchEntity",),
                )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "NoSuchEntity":
                log(f"  (warn) {e}")

    # Delete role
    log(f"- Delete role '{IAM_ROLE_TO_DELETE}' (if exists)")
    if not DRY_RUN:
        try:
            try_call(lambda: iam.delete_role(RoleName=IAM_ROLE_TO_DELETE), ignore_codes=("NoSuchEntity",))
        except ClientError as e:
            log(f"  (warn) {e}")


# =============================================================================
# Entrypoint
# =============================================================================


# Main cleanup flow (calls cleanup steps in a safe deletion order).
def main():
    sts = safe_client("sts")
    try:
        ident = sts.get_caller_identity()
        log(f"Account: {ident.get('Account')}  Region: {REGION}")
    except ClientError as e:
        die(f"AWS credentials not working: {e}")

    log(f"\nDRY_RUN = {DRY_RUN}")
    log(f"STACK_PREFIX = {STACK_PREFIX}")
    log(f"S3_BUCKET_PREFIXES = {S3_BUCKET_PREFIXES}")

    # Order matters (delete APIs/functions first, then logs/buckets etc.)
    delete_apigw_http_apis()
    delete_lambda_functions_and_layers()
    delete_cognito_user_pools()
    delete_sns_topics()
    delete_dynamodb_tables()
    delete_cloudwatch_logs()
    empty_and_delete_s3_buckets()
    cleanup_iam()

    log("\nDONE.")
    if DRY_RUN:
        log("Nothing was deleted (DRY_RUN=true). To actually delete, run with: DRY_RUN=false")


if __name__ == "__main__":
    main()
