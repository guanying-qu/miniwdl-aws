"""
miniwdl-aws-submit CLI entry point (console script) to submit a miniwdl "workflow job" to an AWS
Batch queue, which will invoke miniwdl-run-s3upload to run the workflow (spawning additional Batch
jobs as needed to execute tasks). This is typically used on-laptop to kick off workflows, without
the laptop needing to stay on/connected. It can also wait for the workflow job to complete and
stream its logs.
"""

import sys
import os
import time
import argparse
import shlex
from datetime import datetime
from collections import defaultdict
import boto3
from ._util import detect_aws_region, randomize_job_name, END_OF_LOG, efs_id_from_access_point


def miniwdl_submit_awsbatch(argv):
    # Configure from arguments & environment
    args, unused_args = parse_args_and_env(argv)
    verbose = args.follow or "--verbose" in unused_args or "--debug" in unused_args

    # TODO: accept local WDL source code; check, zip, & attach it

    aws_region_name = detect_aws_region(None)
    if not aws_region_name:
        print(
            "Failed to detect AWS region; configure AWS CLI or set environment AWS_DEFAULT_REGION",
            file=sys.stderr,
        )
        sys.exit(1)
    aws_batch = boto3.client("batch", region_name=aws_region_name)
    if not args.workflow_role:
        args.workflow_role = detect_workflow_role(aws_batch, args.workflow_queue, verbose)
    fs_id = efs_id_from_access_point(aws_region_name, args.fsap)

    # Prepare workflow job: command, environment, and container properties
    job_name, miniwdl_run_cmd = form_miniwdl_run_cmd(args, unused_args)
    job_name = randomize_job_name(job_name)

    environment = [
        {"name": "MINIWDL__AWS__FS", "value": fs_id},
        {"name": "MINIWDL__AWS__FSAP", "value": args.fsap},
        {"name": "MINIWDL__AWS__TASK_QUEUE", "value": args.task_queue},
        {"name": "MINIWDL__FILE_IO__ROOT", "value": args.mount},
    ]
    extra_env = set()
    if not args.no_env:
        # pass through environment variables starting with MINIWDL__ (except those specific to
        # workflow job launch, or passed through via command line)
        for k in os.environ:
            if k.startswith("MINIWDL__") and k not in (
                "MINIWDL__AWS__FS",
                "MINIWDL__AWS__FSAP",
                "MINIWDL__AWS__TASK_QUEUE",
                "MINIWDL__AWS__WORKFLOW_QUEUE",
                "MINIWDL__AWS__WORKFLOW_ROLE",
                "MINIWDL__AWS__WORKFLOW_IMAGE",
                "MINIWDL__AWS__S3_UPLOAD_FOLDER",
                "MINIWDL__AWS__S3_UPLOAD_DELETE_AFTER",
                "MINIWDL__FILE_IO__ROOT",
            ):
                environment.append({"name": k, "value": os.environ[k]})
                extra_env.add(k)

    if verbose:
        print("Image: " + args.image, file=sys.stderr)
        if extra_env:
            print(
                "Passing through environment variables (--no-env to disable): "
                + " ".join(list(extra_env)),
                file=sys.stderr,
            )
        print("Invocation: " + " ".join(shlex.quote(s) for s in miniwdl_run_cmd), file=sys.stderr)

    workflow_container_props = {
        "fargatePlatformConfiguration": {"platformVersion": "1.4.0"},
        "executionRoleArn": args.workflow_role,
        "jobRoleArn": args.workflow_role,
        "resourceRequirements": [
            {"type": "VCPU", "value": str(args.cpu)},
            {"type": "MEMORY", "value": str(args.memory_GiB * 1024)},
        ],
        "volumes": [
            {
                "name": "efs",
                "efsVolumeConfiguration": {
                    "fileSystemId": fs_id,
                    "transitEncryption": "ENABLED",
                    "authorizationConfig": {"accessPointId": args.fsap},
                },
            }
        ],
        "mountPoints": [{"containerPath": args.mount, "sourceVolume": "efs"}],
        "image": args.image,
        "command": miniwdl_run_cmd,
        "environment": environment,
    }
    if not args.no_public_ip:
        workflow_container_props["networkConfiguration"] = {"assignPublicIp": "ENABLED"}

    # Register & submit workflow job
    workflow_job_def = aws_batch.register_job_definition(
        jobDefinitionName=job_name,
        platformCapabilities=["FARGATE"],
        type="container",
        containerProperties=workflow_container_props,
    )
    workflow_job_def_handle = (
        f"{workflow_job_def['jobDefinitionName']}:{workflow_job_def['revision']}"
    )
    try:
        workflow_job_id = aws_batch.submit_job(
            jobName=job_name,
            jobQueue=args.workflow_queue,
            jobDefinition=workflow_job_def_handle,
        )["jobId"]
        if verbose:
            print(f"Submitted {job_name} to {args.workflow_queue}:", file=sys.stderr)
            sys.stderr.flush()
        print(workflow_job_id)
        if not sys.stdout.isatty():
            print(workflow_job_id, file=sys.stderr)
    finally:
        aws_batch.deregister_job_definition(jobDefinition=workflow_job_def_handle)

    # Wait for workflow job, if requested
    exit_code = 0
    if args.wait or args.follow:
        exit_code = wait(aws_region_name, aws_batch, workflow_job_id, args.follow)
    sys.exit(exit_code)


def parse_args_and_env(argv):
    if "COLUMNS" not in os.environ:
        os.environ["COLUMNS"] = "100"
    parser = argparse.ArgumentParser(
        prog="miniwdl-aws-submit",
        description="Launch `miniwdl run` on AWS Batch (+ EFS at /mnt/efs), itself launching additional"
        " Batch jobs to execute WDL tasks. Passed-through arguments to `miniwdl run` should refer to"
        " s3:// or /mnt/efs/ input paths, rather than the local filesystem.",
        usage="miniwdl-aws-submit [miniwdl_run_arg ...] --workflow-queue WORKFLOW_QUEUE --task-queue TASK_QUEUE --fsap fsap-xxxx",
        allow_abbrev=False,
    )
    group = parser.add_argument_group("AWS Batch")
    group.add_argument(
        "--workflow-queue",
        help="job queue for workflow job [env MINIWDL__AWS__WORKFLOW_QUEUE]",
    )
    group.add_argument(
        "--task-queue", help="job queue for task jobs [env MINIWDL__AWS__TASK_QUEUE]"
    )
    group.add_argument(
        "--fsap",
        help="EFS Access Point ID (fsap-xxxx) for mounting [env MINIWDL__AWS__FSAP]",
    )
    group.add_argument(
        "--mount", default="/mnt/efs", help="EFS mount point in all containers [/mnt/efs]"
    )
    group = parser.add_argument_group("Workflow job provisioning")
    group.add_argument(
        "--workflow-role",
        help="ARN of execution+job role for workflow job [env MINIWDL__AWS__WORKFLOW_ROLE"
        " or read from WorkflowEngineRoleArn tag on job queue]",
    )
    group.add_argument("--name", help="workflow job name [WDL filename]")
    group.add_argument("--cpu", metavar="N", type=int, default=2, help="vCPUs for workflow job [2]")
    group.add_argument(
        "--memory-GiB", metavar="N", type=int, default=4, help="memory for workflow job [4]"
    )
    group.add_argument(
        "--image",
        help="miniwdl-aws Docker image tag for workflow job [env MINIWDL__AWS__WORKFLOW_IMAGE]",
    )
    group.add_argument(
        "--no-env", action="store_true", help="don't pass through MINIWDL__* environment variables"
    )
    group.add_argument(
        "--no-public-ip",
        action="store_true",
        help="don't assign public IP (workflow compute env has private subnet & NAT)",
    )
    group = parser.add_argument_group("miniwdl I/O")
    group.add_argument(
        "--dir",
        default=None,
        help="Run directory prefix [/mnt/efs/miniwdl_run]",
    )
    group.add_argument(
        "--s3upload",
        help="s3://bucket/folder/ at which to upload run outputs (otherwise left on EFS)",
    )
    group.add_argument(
        "--delete-after",
        choices=("always", "success", "failure"),
        help="with --s3upload, delete EFS run directory afterwards",
    )
    parser.add_argument(
        "--wait", "-w", action="store_true", help="wait for workflow job to complete"
    )
    parser.add_argument(
        "--follow",
        "-f",
        action="store_true",
        help="live-stream workflow log to standard error (implies --wait)",
    )
    parser.add_argument("--self-test", action="store_true", help="perform `miniwdl run_self_test`")

    # Parse command line
    args, unused_args = parser.parse_known_args(argv[1:])

    if args.mount.endswith("/"):
        args.mount = args.mount[:-1]
    assert args.mount
    if not args.dir:
        args.dir = os.path.join(args.mount, "miniwdl_run")
    if not args.dir.startswith(args.mount):
        print(f"--dir must begin with {args.mount}", file=sys.stderr)
        sys.exit(1)

    # Detect additional configuration from environment
    args.fsap = args.fsap if args.fsap else os.environ.get("MINIWDL__AWS__FSAP", "")
    args.workflow_queue = (
        args.workflow_queue
        if args.workflow_queue
        else os.environ.get("MINIWDL__AWS__WORKFLOW_QUEUE", None)
    )
    args.task_queue = (
        args.task_queue if args.task_queue else os.environ.get("MINIWDL__AWS__TASK_QUEUE", None)
    )
    args.workflow_role = (
        args.workflow_role
        if args.workflow_role
        else os.environ.get("MINIWDL__AWS__WORKFLOW_ROLE", None)
    )
    if not (args.fsap.startswith("fsap-") and args.workflow_queue and args.task_queue):
        print(
            "--fsap, --workflow-queue, and --task-queue all required (or environment variables MINIWDL__AWS__FSAP, MINIWDL__AWS__WORKFLOW_QUEUE, MINIWDL__AWS__TASK_QUEUE)",
            file=sys.stderr,
        )
        sys.exit(1)
    args.image = args.image if args.image else os.environ.get("MINIWDL__AWS__WORKFLOW_IMAGE", None)
    if not args.image:
        import importlib_metadata

        try:
            args.image = "ghcr.io/miniwdl-ext/miniwdl-aws:v" + importlib_metadata.version(
                "miniwdl-aws"
            )
        except importlib_metadata.PackageNotFoundError:
            print(
                "Failed to detect miniwdl Docker image version tag; set explicitly with --image or MINIWDL__AWS__WORKFLOW_IMAGE",
                file=sys.stderr,
            )
            sys.exit(1)
    if args.delete_after and not args.s3upload:
        print("--delete-after requires --s3upload", file=sys.stderr)
        sys.exit(1)
    args.s3upload = (
        args.s3upload if args.s3upload else os.environ.get("MINIWDL__AWS__S3_UPLOAD_FOLDER", None)
    )
    args.delete_after = (
        args.delete_after.strip().lower()
        if args.delete_after
        else os.environ.get("MINIWDL__AWS__DELETE_AFTER_S3_UPLOAD", None)
    )

    return (args, unused_args)


def form_miniwdl_run_cmd(args, unused_args):
    """
    Formulate the `miniwdl run` command line to be invoked in the workflow job container
    """
    if args.self_test:
        self_test_dir = os.path.join(
            args.mount, "miniwdl_run_self_test", datetime.today().strftime("%Y%m%d_%H%M%S")
        )
        miniwdl_run_cmd = ["miniwdl", "run_self_test", "--dir", self_test_dir]
        job_name = args.name if args.name else "miniwdl_run_self_test"
    else:
        wdl_filename = next((arg for arg in unused_args if not arg.startswith("-")), None)
        if not wdl_filename:
            print("Command line appears to be missing WDL filename", file=sys.stderr)
            sys.exit(1)
        job_name = args.name
        if not job_name:
            job_name = os.path.basename(wdl_filename).lstrip(".")
            try:
                for punct in (".", "?"):
                    if job_name.index(punct) > 0:
                        job_name = job_name[: job_name.index(punct)]
            except ValueError:
                pass
            job_name = ("miniwdl_run_" + job_name)[:128]
        # pass most arguments through to miniwdl-run-s3upload inside workflow job
        miniwdl_run_cmd = ["miniwdl-run-s3upload"] + unused_args
        miniwdl_run_cmd.extend(["--dir", args.dir])
        miniwdl_run_cmd.extend(["--s3upload", args.s3upload] if args.s3upload else [])
        miniwdl_run_cmd.extend(["--delete-after", args.delete_after] if args.delete_after else [])
    return (job_name, miniwdl_run_cmd)


def detect_workflow_role(aws_batch, workflow_queue, verbose=False):
    """
    With Fargate Batch, we need to specify an IAM role for the workflow job at submission time.
    (Unlike EC2 Batch, where a role is associated with the EC2 instance profile in the compute
    environment). We need the role's ARN which is rather unwieldy, so rather than always making
    user provide that through command-line flags or environment, try to read it from the
    WorkflowEngineRoleArn tag on the job queue. Infra provisioning (CloudFormation, Terraform,
    etc.) can set this tag as a convenience.
    """
    try:
        workflow_role = aws_batch.describe_job_queues(jobQueues=[workflow_queue])["jobQueues"][0][
            "tags"
        ]["WorkflowEngineRoleArn"]
        if verbose:
            print(
                "Workflow engine IAM role (from WorkflowEngineRoleArn tag of workflow queue): "
                + workflow_role,
                file=sys.stderr,
            )
        assert workflow_role.startswith("arn:aws:iam::")
        return workflow_role
    except:
        print(
            "Unable to read ARN of workflow engine IAM role from WorkflowEngineRoleArn tag of workflow job queue."
            " Double-check --workflow-queue, or set --workflow-role or environment MINIWDL__AWS__WORKFLOW_ROLE.",
            file=sys.stderr,
        )
        sys.exit(1)


def wait(aws_region_name, aws_batch, workflow_job_id, follow):
    """
    Wait for workflow job to complete & return its exit code; optionally tail its log to stderr
    """
    try:
        log_follower = None
        exit_code = None
        saw_end = False
        while exit_code is None:
            time.sleep(1.0)
            job_descs = aws_batch.describe_jobs(jobs=[workflow_job_id])
            job_desc = job_descs["jobs"][0]
            if (
                not log_follower
                and "container" in job_desc
                and "logStreamName" in job_desc["container"]
            ):
                log_stream_name = job_desc["container"]["logStreamName"]
                print("Log stream: " + log_stream_name, file=sys.stderr)
                sys.stderr.flush()
                log_follower = CloudWatchLogsFollower(
                    boto3.DEFAULT_SESSION, aws_region_name, "/aws/batch/job", log_stream_name
                )
            if follow and log_follower:
                for event in log_follower.new_events():
                    if END_OF_LOG not in event["message"]:
                        print(event["message"], file=sys.stderr)
                    else:
                        saw_end = True
                sys.stderr.flush()
            if job_desc["status"] == "SUCCEEDED":
                exit_code = 0
            elif job_desc["status"] == "FAILED":
                exit_code = -1
                if "container" in job_desc and "exitCode" in job_desc["container"]:
                    exit_code = job_desc["container"]["exitCode"]
                    assert exit_code != 0
        if follow and log_follower and not saw_end:
            # give straggler log messages a few seconds to appear
            time.sleep(3.0)
            for event in log_follower.new_events():
                if END_OF_LOG not in event["message"]:
                    print(event["message"], file=sys.stderr)
                else:
                    saw_end = True
            if not saw_end:
                print(
                    f"[miniwdl-aws-submit] WARNING: end-of-log marker not seen; more information may appear in log stream {log_stream_name}",
                    file=sys.stderr,
                )
            sys.stderr.flush()
        print(job_desc["status"] + "\t" + workflow_job_id, file=sys.stderr)
        assert isinstance(exit_code, int)
        return exit_code
    except KeyboardInterrupt:
        print(
            f"[miniwdl-aws-submit] interrupted by Ctrl-C; workflow may remain active in workflow job {workflow_job_id}",
            file=sys.stderr,
        )
        return -1


class CloudWatchLogsFollower:
    # Based loosely on:
    #   https://github.com/aws/aws-cli/blob/v2/awscli/customizations/logs/tail.py
    # which wasn't suitable to use directly at the time of this writing, because of
    #   https://github.com/aws/aws-cli/issues/5560
    def __init__(self, boto_session, region_name, group_name, stream_name=None):
        self.group_name = group_name
        self.stream_name = stream_name
        self._newest_timestamp = None
        self._newest_event_ids = set()
        self._client = boto_session.client("logs", region_name=region_name)

    def new_events(self):
        event_ids_per_timestamp = defaultdict(set)

        filter_args = {"logGroupName": self.group_name}
        if self.stream_name:
            filter_args["logStreamNames"] = [self.stream_name]
        if self._newest_timestamp:
            filter_args["startTime"] = self._newest_timestamp
        while True:
            try:
                response = self._client.filter_log_events(**filter_args)
            except self._client.exceptions.ResourceNotFoundException:
                return  # we may learn the Batch job's log stream name before it actually exists
            for event in response["events"]:
                # For the case where we've hit the last page, we will be
                # reusing the newest timestamp of the received events to keep polling.
                # This means it is possible that duplicate log events with same timestamp
                # are returned back which we do not want to yield again.
                # We only want to yield log events that we have not seen.
                if event["eventId"] not in self._newest_event_ids:
                    event_ids_per_timestamp[event["timestamp"]].add(event["eventId"])
                    yield event
            if "nextToken" in response:
                filter_args["nextToken"] = response["nextToken"]
            else:
                break

        if event_ids_per_timestamp:
            self._newest_timestamp = max(event_ids_per_timestamp.keys())
            self._newest_event_ids = event_ids_per_timestamp[self._newest_timestamp]
