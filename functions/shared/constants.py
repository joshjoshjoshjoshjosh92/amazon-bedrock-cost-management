"""Shared constants for Bedrock Cost Tracker Lambda functions.

The DENIED_ACTIONS list defines all Bedrock actions that incur cost and is used
by both the IAM deny policy and SCP generation to ensure consistent enforcement.
"""

DENIED_ACTIONS = [
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse",
    "bedrock:ConverseStream",
    "bedrock:InvokeAgent",
    "bedrock:CreateModelInvocationJob",
    "bedrock:RetrieveAndGenerate",
    "bedrock:Retrieve",
    "bedrock:ApplyGuardrail",
    "bedrock-agent-runtime:InvokeAgent",
    "bedrock-agent-runtime:InvokeFlow",
    "bedrock-agent-runtime:Retrieve",
    "bedrock-agent-runtime:RetrieveAndGenerate",
]
