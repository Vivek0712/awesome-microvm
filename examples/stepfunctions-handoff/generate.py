"""Regenerate template.yaml, template-map.yaml, and lease.asl.json from microvm-ctl's generator.

    pip install microvm-ctl
    python3 generate.py [--budget 300] [--heartbeat 90] [--heartbeat-every 30]

`lease.asl.json` is the plain state machine (placeholder account, region, and role) for
reading; it is what `mvm lease asl --image handoff-agent --execution-role <arn> --budget 300
--heartbeat 90` prints. `template.yaml` inlines the same machine as `DefinitionString: !Sub`
with `${ImageName}`, `${AgentExecutionRole.Arn}`, `${AWS::Region}`, and `${Budget}` in place
of the literals. JSONata expressions use `{% %}` and `$name`, never `${`, so `Fn::Sub` leaves
them alone; this script checks that before writing.

`template-map.yaml` is the fan-out variant (`mvm lease asl --map`): the same lease flow as
the item processor of a Step Functions Map over `$states.input.shards`, `${MaxConcurrency}`
from a parameter, and, when `ApprovalTopicArn` is set, a `Gate` that sends executions with
more than `${ApproveAboveShards}` shards through an SNS publish with a task token first. The
template carries both definitions and picks one with a Condition. It needs microvm-ctl >=
0.3.0 (`FanoutSpec`); older versions write the other two files and say so.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path

from microvm.integrations.stepfunctions import RUN_WAIT, lease_state_machine
from microvm.lease import LeasePolicy

try:  # microvm-ctl >= 0.3.0
    from microvm.integrations.stepfunctions import FanoutSpec
except ImportError:  # pragma: no cover - older microvm-ctl
    FanoutSpec = None

HERE = Path(__file__).parent
SLACK_S = 120  # the VM outlives the budget by this much, no more
PLACEHOLDER_ACCOUNT = "123456789012"
PLACEHOLDER_REGION = "us-east-1"
SUB_NAMES = {"ImageName", "AgentExecutionRole.Arn", "AWS::Region", "AWS::AccountId", "AWS::StackName",
             "Budget", "MaxConcurrency", "ApproveAboveShards", "ApprovalTopicArn"}
BUDGET_SENTINEL = "__BUDGET__"
# integers no real machine would carry: swapped for ${MaxConcurrency} / ${ApproveAboveShards} after json.dumps
MAX_CONCURRENCY_SENTINEL = 987654301
APPROVE_ABOVE_SENTINEL = 987654302
CFN_IMAGE_ARN = "arn:aws:lambda:${AWS::Region}:${AWS::AccountId}:microvm-image:${ImageName}"
CFN_ROLE_ARN = "${AgentExecutionRole.Arn}"


def _machine(image_arn: str, role_arn: str, region: str, budget: int, heartbeat: int, every: int,
             fanout=None) -> dict:
    kw = {"fanout": fanout} if fanout is not None else {}
    return lease_state_machine(
        image_arn=image_arn, execution_role_arn=role_arn, region=region, heartbeat_s=every,
        policy=LeasePolicy(budget_s=budget, heartbeat_timeout_s=heartbeat, slack_s=SLACK_S), **kw,
    )


def plain_asl(image: str, budget: int, heartbeat: int, every: int) -> dict:
    """The committed lease.asl.json: concrete placeholder ARNs, nothing to substitute."""
    return _machine(
        f"arn:aws:lambda:{PLACEHOLDER_REGION}:{PLACEHOLDER_ACCOUNT}:microvm-image:{image}",
        f"arn:aws:iam::{PLACEHOLDER_ACCOUNT}:role/microvm-sfn-handoff-agent",
        PLACEHOLDER_REGION, budget, heartbeat, every,
    )


def _nodes(obj):
    """Every dict in the machine, depth first (a Map's item processor included)."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _nodes(v)


def _parameterise_budget(asl: dict, budget: int) -> dict:
    """Budget-derived numbers cannot be computed by Fn::Sub, so TimeoutSeconds and the idle
    cap take `${Budget}` literally and the two derived values become JSONata arithmetic.
    Finds the lease state and the stale filter wherever they sit (top level or item processor)."""
    asl = copy.deepcopy(asl)
    stale = f"- {(budget + SLACK_S) * 1000}]]"
    hits = 0
    for node in _nodes(asl):
        if node.get("Resource") == RUN_WAIT:
            node["TimeoutSeconds"] = BUDGET_SENTINEL
            node["Arguments"]["IdlePolicy"]["MaxIdleDurationSeconds"] = BUDGET_SENTINEL
            node["Arguments"]["MaximumDurationInSeconds"] = f"{{% ${{Budget}} + {SLACK_S} %}}"
            hits += 1
        elif node.get("Type") == "Map" and stale in str(node.get("Items", "")):
            node["Items"] = node["Items"].replace(stale, f"- (${{Budget}} + {SLACK_S}) * 1000]]")
            hits += 1
    assert hits == 2, f"expected one lease state and one stale filter, patched {hits}"
    return asl


def _definition(asl: dict, swaps: dict[int, str] | None = None) -> str:
    """json.dumps with the sentinels swapped for `${Name}` and a check that nothing else
    looks like an Fn::Sub placeholder."""
    text = json.dumps(asl, indent=2).replace(f'"{BUDGET_SENTINEL}"', "${Budget}")
    for literal, name in (swaps or {}).items():
        n = text.count(str(literal))
        if n != 1:
            raise SystemExit(f"sentinel {literal} for ${{{name}}} appears {n} times, expected once")
        text = text.replace(str(literal), f"${{{name}}}")
    unexpected = set(re.findall(r"\$\{([^}]*)\}", text)) - SUB_NAMES
    if unexpected:
        raise SystemExit(f"DefinitionString has ${{...}} that Fn::Sub would mangle: {sorted(unexpected)}")
    return text


def parameterised_asl(budget: int, heartbeat: int, every: int) -> str:
    """The single-lease DefinitionString body with CloudFormation placeholders."""
    asl = _parameterise_budget(_machine(CFN_IMAGE_ARN, CFN_ROLE_ARN, "${AWS::Region}", budget, heartbeat, every),
                               budget)
    asl["Comment"] = (f"microvm-ctl lease of ${{ImageName}}: budget ${{Budget}}s, VM cap "
                      f"Budget + {SLACK_S}s (generated by generate.py, do not edit by hand)")
    return _definition(asl)


def parameterised_map_asl(budget: int, heartbeat: int, every: int, approval: bool) -> str:
    """The fan-out DefinitionString body: Map over $states.input.shards, `${MaxConcurrency}`,
    and with `approval` the Gate -> RequestApproval (`${ApprovalTopicArn}`, `${ApproveAboveShards}`)."""
    spec = FanoutSpec(
        items_expr="$states.input.shards", max_concurrency=MAX_CONCURRENCY_SENTINEL,
        approval_topic_arn="${ApprovalTopicArn}" if approval else None,
        approve_above_shards=APPROVE_ABOVE_SENTINEL if approval else None,
    )
    asl = _parameterise_budget(
        _machine(CFN_IMAGE_ARN, CFN_ROLE_ARN, "${AWS::Region}", budget, heartbeat, every, fanout=spec), budget)
    asl["Comment"] = (f"microvm-ctl lease fan-out of ${{ImageName}}: ${{MaxConcurrency}} at a time, budget "
                      f"${{Budget}}s, VM cap Budget + {SLACK_S}s"
                      + (", approval above ${ApproveAboveShards} shards" if approval else "")
                      + " (generated by generate.py, do not edit by hand)")
    swaps = {MAX_CONCURRENCY_SENTINEL: "MaxConcurrency"}
    if approval:
        swaps[APPROVE_ABOVE_SENTINEL] = "ApproveAboveShards"
    return _definition(asl, swaps)


TEMPLATE = """\
AWSTemplateFormatVersion: '2010-09-09'
Description: >
  Step Functions state machine that leases a Lambda MicroVM (built with microvm-ctl) to one
  task with runMicrovm.waitForTaskToken and lets the VM complete the task token itself.
  Generated by generate.py from microvm.integrations.stepfunctions; do not edit by hand.

Parameters:
  ImageName:
    Type: String
    Default: handoff-agent
    Description: Name of the agent MicroVM image built with `mvm image build`.
  Budget:
    Type: Number
    Default: {budget}
    Description: Seconds the state machine waits for the VM (TimeoutSeconds); the VM cap is Budget + {slack}.

Resources:
  # ---- the role the leased VM runs as: logs on both group prefixes, task token calls back to this machine
  AgentExecutionRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub ${{AWS::StackName}}-agent
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal: {{ Service: lambda.amazonaws.com }}
            Action: [sts:AssumeRole, sts:TagSession]
      Policies:
        - PolicyName: agent
          PolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Effect: Allow
                Action: [logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents]
                Resource:
                  - !Sub arn:aws:logs:${{AWS::Region}}:${{AWS::AccountId}}:log-group:/aws/lambda-microvms/*
                  - !Sub arn:aws:logs:${{AWS::Region}}:${{AWS::AccountId}}:log-group:/aws/lambda/microvms/*
              - Sid: CompleteTheLease
                Effect: Allow
                Action: [states:SendTaskSuccess, states:SendTaskFailure, states:SendTaskHeartbeat]
                # the machine is named below so this ARN is known before it exists (no circular reference)
                Resource: !Sub arn:aws:states:${{AWS::Region}}:${{AWS::AccountId}}:stateMachine:${{AWS::StackName}}-lease

  # ---- the role the state machine runs as: launch, list, terminate, and pass the agent role
  StateMachineRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub ${{AWS::StackName}}-states
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal: {{ Service: states.amazonaws.com }}
            Action: sts:AssumeRole
      Policies:
        - PolicyName: orchestrator
          PolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Effect: Allow
                Action: [lambda:RunMicrovm, lambda:TerminateMicrovm, lambda:ListMicrovms, lambda:GetMicrovm]
                Resource: "*"
              - Effect: Allow
                Action: iam:PassRole
                Resource: !GetAtt AgentExecutionRole.Arn
              - Effect: Allow
                Action: lambda:PassNetworkConnector
                Resource: arn:aws:lambda:*:aws:network-connector:aws-network-connector:*

  # ---- Lease (waitForTaskToken) -> Terminate -> Done; timeouts and failures -> Reap -> TerminateStale -> Failed
  StateMachine:
    Type: AWS::StepFunctions::StateMachine
    Properties:
      StateMachineName: !Sub ${{AWS::StackName}}-lease
      StateMachineType: STANDARD
      RoleArn: !GetAtt StateMachineRole.Arn
      DefinitionString: !Sub |
{definition}

Outputs:
  StateMachineArn:
    Value: !Ref StateMachine
    Description: Start executions here; run.sh reads it from the stack.
  AgentExecutionRoleArn:
    Value: !GetAtt AgentExecutionRole.Arn
    Description: Pass as MVM_EXECUTION_ROLE_ARN when leasing the agent image by hand.
"""


TEMPLATE_MAP = """\
AWSTemplateFormatVersion: '2010-09-09'
Description: >
  Step Functions state machine that leases one Lambda MicroVM (built with microvm-ctl) per
  element of the input's `shards` array with a Map over runMicrovm.waitForTaskToken, at most
  MaxConcurrency at a time, optionally behind an SNS approval gate above a shard count.
  Generated by generate.py from microvm.integrations.stepfunctions; do not edit by hand.

Parameters:
  ImageName:
    Type: String
    Default: handoff-agent
    Description: Name of the agent MicroVM image built with `mvm image build`.
  Budget:
    Type: Number
    Default: {budget}
    Description: Seconds each shard's lease waits for its VM (TimeoutSeconds); the VM cap is Budget + {slack}.
  MaxConcurrency:
    Type: Number
    Default: 4
    MinValue: 1
    Description: Shards in flight at once. Use the number `mvm lease plan --image <ImageName> --shards N` prints.
  ApprovalTopicArn:
    Type: String
    Default: ""
    Description: SNS topic for the approval gate; empty means no gate.
  ApproveAboveShards:
    Type: Number
    Default: 8
    MinValue: 0
    Description: With a topic set, executions with more shards than this wait for SendTaskSuccess first.

Conditions:
  HasApproval: !Not [!Equals [!Ref ApprovalTopicArn, ""]]

Resources:
  # ---- the role the leased VMs run as: logs on both group prefixes, task token calls back to this machine
  AgentExecutionRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub ${{AWS::StackName}}-agent
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal: {{ Service: lambda.amazonaws.com }}
            Action: [sts:AssumeRole, sts:TagSession]
      Policies:
        - PolicyName: agent
          PolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Effect: Allow
                Action: [logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents]
                Resource:
                  - !Sub arn:aws:logs:${{AWS::Region}}:${{AWS::AccountId}}:log-group:/aws/lambda-microvms/*
                  - !Sub arn:aws:logs:${{AWS::Region}}:${{AWS::AccountId}}:log-group:/aws/lambda/microvms/*
              - Sid: CompleteTheLease
                Effect: Allow
                Action: [states:SendTaskSuccess, states:SendTaskFailure, states:SendTaskHeartbeat]
                # the machine is named below so this ARN is known before it exists (no circular reference)
                Resource: !Sub arn:aws:states:${{AWS::Region}}:${{AWS::AccountId}}:stateMachine:${{AWS::StackName}}-map

  # ---- the role the state machine runs as: launch, list, terminate, pass the agent role, publish the gate
  StateMachineRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub ${{AWS::StackName}}-states
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal: {{ Service: states.amazonaws.com }}
            Action: sts:AssumeRole
      Policies:
        - PolicyName: orchestrator
          PolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Effect: Allow
                Action: [lambda:RunMicrovm, lambda:TerminateMicrovm, lambda:ListMicrovms, lambda:GetMicrovm]
                Resource: "*"
              - Effect: Allow
                Action: iam:PassRole
                Resource: !GetAtt AgentExecutionRole.Arn
              - Effect: Allow
                Action: lambda:PassNetworkConnector
                Resource: arn:aws:lambda:*:aws:network-connector:aws-network-connector:*
              - !If
                - HasApproval
                - Effect: Allow
                  Action: sns:Publish
                  Resource: !Ref ApprovalTopicArn
                - !Ref AWS::NoValue

  # ---- [Gate -> RequestApproval ->] Fanout (Map: Lease -> Terminate -> Done per shard) -> Done: the VM payloads
  StateMachine:
    Type: AWS::StepFunctions::StateMachine
    Properties:
      StateMachineName: !Sub ${{AWS::StackName}}-map
      StateMachineType: STANDARD
      RoleArn: !GetAtt StateMachineRole.Arn
      DefinitionString: !If
        - HasApproval
        - !Sub |
{gated}
        - !Sub |
{plain}

Outputs:
  StateMachineArn:
    Value: !Ref StateMachine
    Description: 'Start executions here with {{"shards": [task, ...]}}; run-map.sh reads it from the stack.'
  AgentExecutionRoleArn:
    Value: !GetAtt AgentExecutionRole.Arn
    Description: Pass as MVM_EXECUTION_ROLE_ARN when leasing the agent image by hand.
"""


def _indent(definition: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(f"{pad}{line}" if line else "" for line in definition.splitlines())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="handoff-agent")
    ap.add_argument("--budget", type=int, default=300,
                    help="seconds the state machine waits (TimeoutSeconds)")
    ap.add_argument("--heartbeat", type=int, default=90,
                    help="heartbeat timeout in seconds (HeartbeatSeconds)")
    ap.add_argument("--heartbeat-every", type=int, default=30, help="seconds between VM heartbeats")
    args = ap.parse_args()

    asl_path = HERE / "lease.asl.json"
    asl_path.write_text(json.dumps(plain_asl(args.image, args.budget, args.heartbeat, args.heartbeat_every),
                                   indent=2) + "\n")
    definition = parameterised_asl(args.budget, args.heartbeat, args.heartbeat_every)
    (HERE / "template.yaml").write_text(
        TEMPLATE.format(budget=args.budget, slack=SLACK_S, definition=_indent(definition, 8)))
    written = [asl_path.name, "template.yaml"]
    if FanoutSpec is None:
        print("template-map.yaml skipped: microvm-ctl >= 0.3.0 is required "
              "(microvm.integrations.stepfunctions.FanoutSpec); pip install -U microvm-ctl")
    else:
        gated = parameterised_map_asl(args.budget, args.heartbeat, args.heartbeat_every, approval=True)
        plain = parameterised_map_asl(args.budget, args.heartbeat, args.heartbeat_every, approval=False)
        (HERE / "template-map.yaml").write_text(
            TEMPLATE_MAP.format(budget=args.budget, slack=SLACK_S, gated=_indent(gated, 12),
                                plain=_indent(plain, 12)))
        written.append("template-map.yaml")
    print(f"wrote {', '.join(written)} (budget {args.budget}s, heartbeat timeout {args.heartbeat}s)")


if __name__ == "__main__":
    main()
