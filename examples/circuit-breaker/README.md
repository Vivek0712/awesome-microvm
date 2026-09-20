# Circuit breaker

The last governance layer for a fleet of leased VMs, for the day the plan, the pre-flight refusal, the per-lease budget, and the janitor all agree and the account still holds more memory than it should (a loop of fan-outs, a runaway caller, a second orchestrator nobody told you about). Two small Lambda functions and one alarm, built on [microvm-ctl](https://github.com/Vivek0712/microvm-ctl) from PyPI: `MetricPublisher` lists the account's microVMs every minute and publishes `microvm-ctl/RunningMemoryGiB` (the memory of PENDING and RUNNING VMs, from each image version's `minimumMemoryInMiB`) and `microvm-ctl/ActiveMicrovms` (everything that counts against the memory quota, SUSPENDED included), once per image on the `Image` dimension and once without dimensions as the account total; the alarm watches the total against `MemoryGiBThreshold` and publishes to an SNS topic; `Drain` runs `Fleet(fm, image).drain()` for every image in `ImagesToDrain`, or every image with a live VM when it is `*`, through the throttled TerminateMicrovm bucket.

```console
cd examples/circuit-breaker
sam build && sam deploy --guided --parameter-overrides MemoryGiBThreshold=12 ImagesToDrain='*'
aws cloudwatch get-metric-statistics --namespace microvm-ctl --metric-name RunningMemoryGiB \
    --statistics Maximum --period 60 --start-time "$(date -u -v-15M +%FT%TZ)" --end-time "$(date -u +%FT%TZ)"
aws sns publish --topic-arn <TopicArn> --message drill                # fires the drain by hand
mvm ls                                                                 # nothing left RUNNING
```

Size the threshold from the plan, not from a guess: `mvm lease plan --image handoff-agent --shards 8` prints the memory quota and the concurrency the plane will allow, so a threshold just above `concurrency x baseline` (say 12 GiB for four 2 GB VMs plus one operator VM on an 8 GB quota after a quota raise) fires only when something outside the plan is launching. `TreatMissingData: notBreaching` means an empty account never alarms, and the alarm resets itself once the drain has brought the total back under the line. Subscribe a mailbox to the topic as well; the drain is silent otherwise.

Drill it before you need it. I lowered `MemoryGiBThreshold` to 1, launched four 512 MiB `handoff-agent-small` VMs, and watched: the metric caught up after about 95 seconds, the alarm went to ALARM, and `Drain` terminated all four in the same minute (`circuit breaker tripped (aws:sns): drained {'handoff-agent-small': 4}` in its log). Then I put the threshold back.

## Stop the spend, not just the VMs

The drain terminates what is running; it does not stop the next launch. AWS Budgets can, with an action that attaches a deny policy on `lambda:RunMicrovm` to the orchestrator roles when the month's cost crosses the line, and detaches it when you reset the action. Add this next to the stack above (role names are those of the Step Functions state machine role and the durable function's role; a bare `Roles` entry is a role name, not an ARN):

```yaml
  DenyRunMicrovm:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Deny
            Action: lambda:RunMicrovm
            Resource: "*"
  BudgetActionRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Principal: { Service: budgets.amazonaws.com }
            Action: sts:AssumeRole
      Policies:
        - PolicyName: attach-the-brake
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              - Effect: Allow
                Action: [iam:AttachRolePolicy, iam:DetachRolePolicy]
                Resource: !Sub arn:aws:iam::${AWS::AccountId}:role/microvm-*
  MicrovmBudget:
    Type: AWS::Budgets::Budget
    Properties:
      Budget:
        BudgetName: microvm-monthly
        BudgetType: COST
        TimeUnit: MONTHLY
        BudgetLimit: { Amount: 200, Unit: USD }   # narrow with CostFilters once you know the line items
  StopLaunches:
    Type: AWS::Budgets::BudgetsAction
    Properties:
      BudgetName: !Ref MicrovmBudget
      ActionType: APPLY_IAM_POLICY
      ActionThreshold: { Type: PERCENTAGE, Value: 100 }
      NotificationType: ACTUAL
      ApprovalModel: AUTOMATIC
      ExecutionRoleArn: !GetAtt BudgetActionRole.Arn
      Definition:
        IamActionDefinition:
          PolicyArn: !Ref DenyRunMicrovm
          Roles: [microvm-sfn-handoff-states, microvm-sfn-handoff-map-states, <durable orchestrator role name>]
      Subscribers:
        - { Type: EMAIL, Address: you@example.com }
```

With the deny attached, a Step Functions `Lease` state fails with `AccessDeniedException` into the `Reap` path, and a durable `lease_map` fails in its launch step; VMs already leased finish and terminate on their own budget. The five layers together: the plan says how many, `lease_many` refuses more than that, every lease carries a budget and a maximum duration, the janitor reaps by age, and this breaker drains by memory and Budgets stops the launches.

Orchestrators this protects: [stepfunctions-handoff](../stepfunctions-handoff) (single lease and the Map fan-out), [durable-handoff](../durable-handoff) (`lease_map`), and anything that leases [handoff-agent](../handoff-agent).
