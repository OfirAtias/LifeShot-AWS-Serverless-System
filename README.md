# LifeShot — Installation Guide (Clean AWS Account)

This document provides clear and detailed instructions for installing the **LifeShot** system from source files on a clean AWS account.
It is intended for technical reviewers, evaluators, or potential clients who need to deploy and verify the system independently.

The installation was designed and tested using the automated deployment script: `lifeshot_bootstrap.py`.

## Table of Contents

1. [Overview](#1-overview)
2. [Prerequisites](#2-prerequisites)
3. [Expected Project Structure](#3-expected-project-structure)
4. [Connect to a Clean AWS Account](#4-connect-to-a-clean-aws-account)
5. [Full System Deployment](#5-full-system-deployment)
6. [AWS Resources Created](#6-aws-resources-created)
7. [System Verification (Smoke Test)](#7-system-verification-smoke-test)
8. [Important Notes & Troubleshooting](#8-important-notes--troubleshooting)
9. [Cleanup (Optional)](#9-cleanup-optional)
10. [AWS CLI Logout (Optional)](#10-aws-cli-logout-optional)
11. [API Documentation](#11-api-documentation)
12. [Final Notes](#12-final-notes)

---

## 1. Overview

LifeShot is a serverless, cloud-based safety monitoring system designed to assist lifeguards and pool administrators by detecting potential drowning situations and managing safety events in real time.

The system is deployed entirely on AWS using managed services and can be installed on any compatible AWS account.

---

## 2. Prerequisites

### 2.1 Required Software (Local Machine)

Make sure the following tools are installed:

- **Python 3.11 or higher**
- **AWS CLI v2**
- **Node.js 18 or 20 + npm** (required only if Lambda ZIP packages are not prebuilt)
- Operating System:
  - Windows (PowerShell)
  - macOS / Linux (Terminal)

### 2.2 AWS Permissions

The deployment script creates and configures the following AWS services:

- Amazon Cognito
  - User Pool
  - App Client
  - User Groups (Admins, Lifeguards)
- Amazon API Gateway (HTTP API)
- AWS Lambda Functions
- Amazon DynamoDB
- Amazon S3 (data + frontend hosting)
- Amazon SNS (alerts)
- AWS Lambda Layer (Pillow)

> **Note:**
> In restricted environments (e.g., VocLabs / Academy accounts), IAM role creation may be blocked.
> In such cases, the script automatically falls back to using an existing **LabRole**, if available.

---

## 3. Expected Project Structure

Ensure the project directory is structured approximately as follows:

```text
LifeShot/
├── deploy/                 # Deployment assets and test images
├── client/                 # Frontend (Admin / Lifeguard UI)
├── lifeshot_bootstrap.py   # Main installation script ✅
├── lifeshot_cleanup.py     # Cleanup script (optional) ✅
├── swagger.yaml            # OpenAPI (Swagger) documentation ✅
├── detector_logic.zip      # Detector Lambda package (optional)
├── render_and_s3.zip       # Render Lambda package (optional)
├── events_and_sns.zip      # Events + SNS Lambda package (optional)
├── api_handler.zip         # Events API Lambda package (optional)
└── pillow311.zip           # Pillow Lambda Layer for Python ✅
```

> If Lambda ZIP packages are missing, the bootstrap script may deploy placeholder functions.
> For a fully functional deployment, all Lambda source ZIPs should be included.

---

## 4. Connect to a Clean AWS Account

Open **PowerShell** (or Terminal) and verify AWS CLI authentication:

```powershell
aws sts get-caller-identity
```

If you are not authenticated, configure AWS credentials:

```powershell
aws configure
```

You will be prompted to enter:

- AWS Access Key ID
- AWS Secret Access Key
- AWS Session Token
- Default region (recommended: `us-east-1`)
- Output format (recommended: `json`)

---

## 5. Full System Deployment

### 5.1 Navigate to the Project Directory

Run the deployment script from the project root directory:

```powershell
cd path\to\LifeShot
```

### 5.2 Run the Installation Script

```powershell
python lifeshot_bootstrap.py
```

At the end of the process, the script prints important outputs, including:

- API Gateway Base URL
- Detector Lambda Function URL
- Frontend S3 Static Website URL
- Cognito User Pool and App Client ID
- SNS Topic ARN

---

## 6. AWS Resources Created

After a successful deployment, the following resources will exist in the AWS account:

### 6.1 Authentication & API

- Amazon Cognito User Pool
  - Groups: Admins, Lifeguards
- Amazon API Gateway (HTTP API)
  - `/auth/*` endpoints — public
  - `/events` endpoints — JWT protected

### 6.2 Compute (AWS Lambda)

- `LifeShot_Login` (Node.js)
- `LifeShot_detector_logic` (Python)
- `LifeShot_RenderAndS3` (Python)
- `LifeShot_EventsAndSNS` (Python)
- `LifeShot_Api_Handler` (Python – Events API)

### 6.3 Data & Storage

- Amazon DynamoDB
  - Table: `LifeShot_Events`
- Amazon S3
  - Frames and processed images bucket
  - Frontend static website bucket

### 6.4 Notifications

- Amazon SNS Topic
  - Email subscription (requires confirmation)

---

## 7. System Verification (Smoke Test)

### 7.1 Open the Frontend

Open the S3 Static Website URL printed at the end of the deployment.

### 7.2 User Login

Log in using one of the predefined users created by the script:

- Admin user
  - Email: `lifeguard647@gmail.com`
- Lifeguard user
  - Email: `lifeguarduser1@gmail.com`

Initial password:

```text
LifeShot!123
```

On first login, the system may require a password change using the `/auth/complete-password` endpoint.

### 7.3 Run Detector Demo

From the Admin → Live Demo screen:

1. Select **Test1** or **Test2**
2. Click **Run Detector**

Expected behavior:

- Images are loaded from S3
- Persons are detected and analyzed
- Rendered images are saved back to S3
- Events are written to DynamoDB
- Alerts are sent via SNS (if detected)
- Events appear in both Admin and Lifeguard dashboards

---

## 8. Important Notes & Troubleshooting

### 8.1 SNS Email Confirmation

If an email subscription is configured, it must be confirmed via the confirmation email.
Without confirmation, alerts will not be delivered.

### 8.2 Automatic S3 Bucket Selection

If the default bucket name is unavailable or not writable, the script will:

- Create a new bucket
- Update all Lambda environment variables accordingly

This behavior is expected and does not affect system functionality.

---

## 9. Cleanup (Optional)

To remove all deployed resources from the AWS account, run:

```powershell
python lifeshot_cleanup.py
```

If the cleanup script supports dry-run mode:

```powershell
$env:DRY_RUN="false"
python lifeshot_cleanup.py
```

Cleanup should only be performed in test or evaluation environments.

---

## 10. AWS CLI Logout (Optional)

To remove local AWS credentials from the machine:

```powershell
Remove-Item $env:USERPROFILE\.aws\credentials -ErrorAction SilentlyContinue
Remove-Item $env:USERPROFILE\.aws\config -ErrorAction SilentlyContinue
```

---

## 11. API Documentation

The LifeShot API is fully documented using OpenAPI (Swagger).

- File: `swagger.yaml`
- Location: Project root directory

The specification describes all authentication and event management endpoints and can be used for review, testing, or future redeployment.

---

## 12. Final Notes

This installation guide is designed to allow a full system deployment on a clean AWS account without prior setup.
Following these steps ensures the system can be installed, verified, and evaluated in a repeatable and consistent way.
