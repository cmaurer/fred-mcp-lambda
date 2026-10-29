"""Builds template.json (CloudFormation) with src/app.py embedded inline."""
import json
import pathlib

root = pathlib.Path(__file__).parent
code = (root / "src" / "app.py").read_text()

template = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Description": "FRED MCP server: public Lambda function URL, bearer-token auth, keys in Secrets Manager",
    "Resources": {
        "FredSecret": {
            "Type": "AWS::SecretsManager::Secret",
            "Properties": {
                "Name": "fred-mcp/config",
                "Description": "fred_api_key (set manually) and bearer_token (generated) for the FRED MCP server",
                "GenerateSecretString": {
                    "SecretStringTemplate": json.dumps({"fred_api_key": "REPLACE_ME"}),
                    "GenerateStringKey": "bearer_token",
                    "PasswordLength": 48,
                    "ExcludePunctuation": True,
                },
            },
        },
        "FredLogGroup": {
            "Type": "AWS::Logs::LogGroup",
            "Properties": {"LogGroupName": "/aws/lambda/fred-mcp", "RetentionInDays": 30},
        },
        "FredRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                                   "Action": "sts:AssumeRole"}],
                },
                "Policies": [{
                    "PolicyName": "fred-mcp-minimal",
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                             "Resource": {"Fn::Sub": "${FredLogGroup.Arn}"}},
                            {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
                             "Resource": {"Ref": "FredSecret"}},
                        ],
                    },
                }],
            },
        },
        "FredFunction": {
            "Type": "AWS::Lambda::Function",
            "DependsOn": ["FredLogGroup"],
            "Properties": {
                "FunctionName": "fred-mcp",
                "Description": "FRED MCP server (stateless streamable HTTP)",
                "Runtime": "python3.13",
                "Architectures": ["arm64"],
                "Handler": "index.handler",
                "MemorySize": 256,
                "Timeout": 60,
                "Role": {"Fn::GetAtt": ["FredRole", "Arn"]},
                "Environment": {"Variables": {"SECRET_ARN": {"Ref": "FredSecret"}}},
                "Code": {"ZipFile": code},
            },
        },
        "FredUrl": {
            "Type": "AWS::Lambda::Url",
            "Properties": {"AuthType": "NONE", "TargetFunctionArn": {"Fn::GetAtt": ["FredFunction", "Arn"]}},
        },
        "FredUrlInvokePermission": {
            "Type": "AWS::Lambda::Permission",
            "Properties": {
                "FunctionName": {"Ref": "FredFunction"},
                "Action": "lambda:InvokeFunctionUrl",
                "Principal": "*",
                "FunctionUrlAuthType": "NONE",
            },
        },
        "FredInvokeViaUrlPermission": {
            "Type": "AWS::Lambda::Permission",
            "Properties": {
                "FunctionName": {"Ref": "FredFunction"},
                "Action": "lambda:InvokeFunction",
                "Principal": "*",
                "InvokedViaFunctionUrl": True,
            },
        },
    },
    "Outputs": {
        "FunctionUrl": {"Value": {"Fn::GetAtt": ["FredUrl", "FunctionUrl"]}},
        "SecretArn": {"Value": {"Ref": "FredSecret"}},
    },
}

(root / "template.json").write_text(json.dumps(template, indent=2) + "\n")
compact = json.dumps(template, separators=(",", ":")).replace("'", "\\u0027")
(root / "template.min.json").write_text(compact)
print(f"template.json written; compact size {len(compact)} bytes (CLI inline limit 51200)")
