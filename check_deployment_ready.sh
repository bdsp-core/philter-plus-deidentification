#!/bin/bash
#
# Check if AWS environment is ready for deployment
#

AWS_PROFILE="bidmc"
REGION="us-east-1"
ROLE_NAME="Philter-EC2-S3-Role"
PROFILE_NAME="Philter-EC2-Profile"

echo "=========================================="
echo "Deployment Readiness Check"
echo "=========================================="
echo ""

# Check 1: AWS Profile
echo "1. Checking AWS profile..."
if aws configure list --profile $AWS_PROFILE > /dev/null 2>&1; then
    echo "   ✓ Profile 'bidmc' exists"
else
    echo "   ✗ Profile 'bidmc' not found"
    echo "   Run: aws configure --profile bidmc  (config at ~/.aws/credentials)"
    exit 1
fi

# Check 2: S3 Access
echo ""
echo "2. Checking S3 access..."
if aws s3 ls --profile $AWS_PROFILE > /dev/null 2>&1; then
    echo "   ✓ S3 access OK"
else
    echo "   ✗ S3 access FAILED"
    exit 1
fi

# Check 3: EC2 Access
echo ""
echo "3. Checking EC2 access..."
if aws ec2 describe-regions --profile $AWS_PROFILE --region $REGION > /dev/null 2>&1; then
    echo "   ✓ EC2 access OK"
else
    echo "   ✗ EC2 access FAILED"
    exit 1
fi

# Check 4: AWS Credentials
echo ""
echo "4. Checking AWS credentials..."
AWS_ACCESS_KEY=$(aws configure get aws_access_key_id --profile $AWS_PROFILE 2>/dev/null)
AWS_SECRET_KEY=$(aws configure get aws_secret_access_key --profile $AWS_PROFILE 2>/dev/null)
if [ -n "$AWS_ACCESS_KEY" ] && [ -n "$AWS_SECRET_KEY" ]; then
    echo "   ✓ AWS credentials found in bidmc profile"
    echo "   Access Key: ${AWS_ACCESS_KEY:0:10}..."
    echo ""
    echo "   Note: Instances will use these user credentials (no IAM roles)"
else
    echo "   ✗ AWS credentials not found"
    echo "   Run: aws configure --profile bidmc  (config at ~/.aws/credentials)"
    exit 1
fi

# Check 5: Philter Config
echo ""
echo "5. Checking Philter config..."
if [ -f "configs/philter_one.json" ]; then
    echo "   ✓ configs/philter_one.json found"

    # Validate JSON
    if python3 -c "import json; json.load(open('configs/philter_one.json'))" 2>/dev/null; then
        echo "   ✓ Config is valid JSON"
    else
        echo "   ⚠  Config might have JSON errors"
    fi
else
    echo "   ✗ configs/philter_one.json not found"
    echo "   ⚠  Deployment will continue but instances won't have config"
fi

# Check 6: Git Bash / Unix environment
echo ""
echo "6. Checking Unix environment..."
if command -v bash > /dev/null 2>&1; then
    echo "   ✓ Bash available"
    BASH_VERSION=$(bash --version | head -n1)
    echo "   Version: $BASH_VERSION"
else
    echo "   ✗ Bash not found"
fi

# Check 7: Windows-specific
echo ""
echo "7. Checking Windows environment..."
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" ]]; then
    echo "   ✓ Running in Git Bash on Windows"
elif [[ "$OSTYPE" == "linux-gnu"* ]] && grep -qi microsoft /proc/version 2>/dev/null; then
    echo "   ✓ Running in WSL on Windows"
else
    echo "   ℹ Running on: $OSTYPE"
fi

# Check 8: Deployment scripts
echo ""
echo "8. Checking deployment scripts..."
if [ -f "deploy_aws.sh" ]; then
    echo "   ✓ deploy_aws.sh found"
    if [ -x "deploy_aws.sh" ]; then
        echo "   ✓ deploy_aws.sh is executable"
    else
        echo "   ⚠  deploy_aws.sh not executable (run: chmod +x deploy_aws.sh)"
    fi
else
    echo "   ✗ deploy_aws.sh not found"
fi

if [ -f "cleanup_aws.sh" ]; then
    echo "   ✓ cleanup_aws.sh found"
else
    echo "   ⚠  cleanup_aws.sh not found"
fi

# Summary
echo ""
echo "=========================================="
echo "Summary"
echo "=========================================="
echo ""

READY=true

echo "✓ Using bidmc user credentials (no IAM roles required)"
echo ""

if [ "$READY" = true ]; then
    echo "✓ READY TO DEPLOY!"
    echo ""
    echo "Next steps:"
    echo "  1. Review configuration in deploy_aws.sh"
    echo "  2. Run: ./deploy_aws.sh"
    echo "  3. Upload data partitions to S3"
    echo "  4. Monitor progress"
    echo ""
    echo "Estimated cost: ~$600"
    echo "Estimated time: ~3 days"
else
    echo "⚠  NOT READY - See issues above"
    echo ""
    echo "Fix any failed checks and run this script again"
fi

echo ""
