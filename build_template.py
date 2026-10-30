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
        # Weekday warm-up: invokes the function with a /health event every 3 minutes from 6:30 to
        # 6:45 America/Chicago (11:30-11:45 UTC in daylight time, 12:30-12:45 UTC in standard time),
        # bracketing the 6:35 CT scheduled task, which can start a few minutes late. An idle
        # environment can be reclaimed within minutes, so one 6:30 ping was not a safe margin.
        # EventBridge Scheduler (not an EventBridge rule) because rules only take UTC cron and
        # would drift an hour off the task at each DST change.
        "FredWarmupRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [
                    {"Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"}, "Action": "sts:AssumeRole",
                     "Condition": {"StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}}}]},
                "Policies": [{"PolicyName": "fred-mcp-warmup-invoke", "PolicyDocument": {
                    "Version": "2012-10-17", "Statement": [
                        {"Effect": "Allow", "Action": "lambda:InvokeFunction",
                         "Resource": {"Fn::GetAtt": ["FredFunction", "Arn"]}}]}}],
            },
        },
        "FredWarmupSchedule": {
            "Type": "AWS::Scheduler::Schedule",
            "Properties": {
                "Name": "fred-mcp-warmup",
                "Description": "Weekday /health invokes every 3 min, 6:30-6:45 CT, around the 6:35 morning brief",
                "ScheduleExpression": "cron(30,33,36,39,42,45 6 ? * MON-FRI *)",
                "ScheduleExpressionTimezone": "America/Chicago",
                "FlexibleTimeWindow": {"Mode": "OFF"},
                "State": "ENABLED",
                "Target": {
                    "Arn": {"Fn::GetAtt": ["FredFunction", "Arn"]},
                    "RoleArn": {"Fn::GetAtt": ["FredWarmupRole", "Arn"]},
                    "Input": json.dumps({"rawPath": "/health", "requestContext": {"http": {"method": "GET"}},
                                         "headers": {"user-agent": "eventbridge-scheduler-warmup"}}),
                    "RetryPolicy": {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 240},
                },
            },
        },
    },
    "Outputs": {
        "WarmupSchedule": {"Value": {"Ref": "FredWarmupSchedule"}},
        "FunctionUrl": {"Value": {"Fn::GetAtt": ["FredUrl", "FunctionUrl"]}},
        "SecretArn": {"Value": {"Ref": "FredSecret"}},
    },
}

(root / "template.json").write_text(json.dumps(template, indent=2) + "\n")
compact = json.dumps(template, separators=(",", ":")).replace("'", "\\u0027")
(root / "template.min.json").write_text(compact)
print(f"template.json written; compact size {len(compact)} bytes (CLI inline limit 51200)")
