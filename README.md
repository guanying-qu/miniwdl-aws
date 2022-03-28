# miniwdl-AWS-FsxL plugin

**Extends [miniwdl](https://github.com/chanzuckerberg/miniwdl) to run workflows on [AWS Batch](https://aws.amazon.com/batch/) and [FSx Lustre](https://aws.amazon.com/fsx/lustre/)**

This miniwdl plugin enables it to submit AWS Batch jobs to execute WDL tasks. It uses FSx Lustre for work-in-progress file I/O, with optional S3 rails for workflow-level I/O.

The following assumes familiarity with [local use of `miniwdl run`](https://miniwdl.readthedocs.io/en/latest/getting_started.html).


## Unattended operations

For non-interactive use, a command-line wrapper `miniwdl-aws-submit` *launches miniwdl in its own small Batch job* to orchestrate the workflow. This **workflow job** then spawns **task jobs** as needed, without needing the submitting computer (e.g. your laptop) to remain connected for the duration. 

### Submitting workflow jobs

First `pip3 install miniwdl-aws` locally to make the `miniwdl-aws-submit` program available. 

The command line resembles `miniwdl run`'s with extra AWS-related arguments:

|command-line argument|equivalent environment variable| |
|---------------------|-------------------------------|-|
| `--workflow-queue`  | `MINIWDL__AWS__WORKFLOW_QUEUE`| Batch job queue on which to schedule the *workflow* job |
| `--task-queue` | `MINIWDL__AWS__TASK_QUEUE` | Batch job queue on which to schedule *task* jobs |
| `--fsid` | `MINIWDL__AWS__FS` | Filesystem ID, which workflow and task jobs will mount at `/mnt/fsx` |
| `--s3upload` | | (optional) S3 folder URI under which to upload the workflow products, including the log and output files |

Unless `--s3upload` ends with /, one more subfolder is added to the uploaded URI prefix, equal to miniwdl's automatic timestamp-prefixed run name. If it does end in /, then the uploads go directly into/under that folder (and a repeat invocation would be expected to overwrite them).

Adding `--wait` makes the tool await the workflow job's success or failure, reproducing miniwdl's exit code. `--follow` does the same and also live-streams the workflow log. Without `--wait` or `--follow`, the tool displays the workflow job UUID and exits immediately.

Arguments not consumed by `miniwdl-aws-submit` are *passed through* to `miniwdl run` inside the workflow job; as are environment variables whose names begin with `MINIWDL__`, allowing override of any [miniwdl configuration option](https://miniwdl.readthedocs.io/en/latest/runner_reference.html#configuration) (disable wih `--no-env`). See [miniwdl_aws.cfg](miniwdl_aws.cfg) for various options preconfigured in the workflow job container.


## Logs & troubleshooting

If the terminal log isn't available (through Studio or `miniwdl-submit-awsbatch --follow`) to trace a workflow failure, look for miniwdl's usual log files written in the run directory on EFS or copied to S3.

Each task job's log is also forwarded to [CloudWatch Logs](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/WhatIsCloudWatchLogs.html) under the `/aws/batch/job` group and a log stream name reported in miniwdl's log. Using `miniwdl_submit_awsbatch`, the workflow job's log is also forwarded. CloudWatch Logs indexes the logs for structured search through the AWS Console & API.

Misconfigured infrastructure might prevent logs from being written to FSL or CloudWatch at all. In that case, use the AWS Batch console/API to find status messages for the workflow or task jobs.
