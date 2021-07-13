"""
BatchJob: implements miniwdl TaskContainer by submitting jobs to an AWS Batch queue and polling
their status. Assumes a shared filesystem (typically EFS) between the miniwdl host and the Batch
workers.
"""

import os
import math
import time
import shutil
import threading
import heapq
from contextlib import ExitStack
import boto3
import botocore
import WDL
import WDL.runtime.task_container
import WDL.runtime._statusbar
from WDL._util import PygtailLogger
from WDL._util import StructuredLogMessage as _
from ._util import (
    detect_aws_region,
    randomize_job_name,
    efs_id_from_access_point,
    detect_sagemaker_studio_efs,
    detect_studio_fsap,
    detect_gwfcore_batch_queue,
)


class BatchJob(WDL.runtime.task_container.TaskContainer):
    @classmethod
    def global_init(cls, cfg, logger):
        cls._region_name = detect_aws_region(cfg)
        assert (
            cls._region_name
        ), "Failed to detect AWS region; configure AWS CLI or set environment AWS_DEFAULT_REGION"

        # EFS configuration based on:
        # - [aws] fsap / MINIWDL__AWS__FSAP
        # - [aws] fs / MINIWDL__AWS__FS
        # - SageMaker Studio metadata, if applicable
        cls._fs_id = None
        cls._fsap_id = None
        cls._fs_mount = cfg.get("file_io", "root")
        assert (
            len(cls._fs_mount) > 1
        ), "misconfiguration, set [file_io] root / MINIWDL__FILE_IO__ROOT to EFS mount point"
        if cfg.has_option("aws", "fs"):
            cls._fs_id = cfg.get("aws", "fs")
        if cfg.has_option("aws", "fsap"):
            cls._fsap_id = cfg.get("aws", "fsap")
            if not cls._fs_id:
                cls._fs_id = efs_id_from_access_point(cls._region_name, cls._fsap_id)
        sagemaker_studio_efs = detect_sagemaker_studio_efs(logger, region_name=cls._region_name)
        if sagemaker_studio_efs:
            (
                studio_efs_id,
                studio_efs_uid,
                studio_efs_home,
                studio_efs_mount,
            ) = sagemaker_studio_efs
            assert (
                not cls._fs_id or cls._fs_id == studio_efs_id
            ), "Configured EFS ([aws] fs / MINIWDL__AWS__FS, [aws] fsap / MINIWDL__AWS__FSAP) isn't associated with current SageMaker Studio domain EFS"
            cls._fs_id = studio_efs_id
            assert cls._fs_mount.rstrip("/") == studio_efs_mount.rstrip("/"), (
                "misconfiguration, set [file_io] root / MINIWDL__FILE_IO__ROOT to "
                + studio_efs_mount.rstrip("/")
            )
            if not cls._fsap_id:
                cls._fsap_id = detect_studio_fsap(
                    logger,
                    studio_efs_id,
                    studio_efs_uid,
                    studio_efs_home,
                    region_name=cls._region_name,
                )
                assert (
                    cls._fsap_id
                ), "Unable to detect suitable EFS Access Point for use with SageMaker Studio; set [aws] fsap / MINIWDL__AWS__FSAP"
            # TODO: else sanity-check that FSAP's root directory equals studio_efs_home
        assert (
            cls._fs_id
        ), "Missing EFS configuration ([aws] fs / MINIWDL__AWS__FS or [aws] fsap / MINIWDL__AWS__FSAP)"
        if not cls._fsap_id:
            logger.warning(
                "AWS BatchJob plugin recommends using EFS Access Point to simplify permissions between containers (configure [aws] fsap / MINIWDL__AWS__FSAP to fsap-xxxx)"
            )
        logger.debug(
            _(
                "AWS BatchJob EFS configuration",
                fs_id=cls._fs_id,
                fsap_id=cls._fsap_id,
                mount=cls._fs_mount,
            )
        )

        # set AWS Batch job queue
        if cfg.has_option("aws", "task_queue"):
            cls._job_queue = cfg.get("aws", "task_queue")
        elif sagemaker_studio_efs:
            cls._job_queue = detect_gwfcore_batch_queue(
                logger, sagemaker_studio_efs[0], region_name=cls._region_name
            )
        assert (
            cls._job_queue
        ), "Missing AWS Batch job queue configuration ([aws] task_queue / MINIWDL__AWS__TASK_QUEUE)"

        # TODO: query Batch compute environment for resource limits
        cls._resource_limits = {"cpu": 64, "mem_bytes": 261992870916}
        cls._submit_lock = threading.Lock()
        cls._last_submit_time = [0.0]
        cls._describer = BatchJobDescriber()
        logger.info(
            _(
                "initialized AWS BatchJob plugin",
                region_name=cls._region_name,
                job_queue=cls._job_queue,
                resource_limits=cls._resource_limits,
            )
        )

    @classmethod
    def detect_resource_limits(cls, cfg, logger):
        return cls._resource_limits

    def __init__(self, cfg, run_id, host_dir):
        super().__init__(cfg, run_id, host_dir)
        self._observed_states = set()
        self._logStreamName = None
        self._inputs_copied = False
        # We'll direct Batch to mount EFS inside the task container at the same location we have
        # it mounted ourselves, namely /mnt/efs. Therefore container_dir will be the same as
        # host_dir (unlike the default Swarm backend, which mounts it at a different virtualized
        # location)
        self.container_dir = self.host_dir

    def copy_input_files(self, logger):
        self._inputs_copied = True
        return super().copy_input_files(logger)

    def host_work_dir(self):
        # Since we aren't virtualizing the in-container paths as noted above, always use the same
        # working directory on task retries, instead of the base class behavior of appending the
        # try counter (on the host side). This loses some robustness to a split-brain condition
        # where the previous try is actually still running when we start the retry. We'll assume
        # Batch prevents that.
        return os.path.join(self.host_dir, "work")

    def host_stdout_txt(self):
        return os.path.join(self.host_dir, "stdout.txt")

    def host_stderr_txt(self):
        return os.path.join(self.host_dir, "stderr.txt")

    def reset(self, logger) -> None:
        shutil.rmtree(self.host_work_dir())
        super().reset(logger)

    def _run(self, logger, terminating, command):
        """
        Run task
        """
        try:
            aws_batch = boto3.Session().client(  # Session() needed for thread safety
                "batch",
                region_name=self._region_name,
                config=botocore.config.Config(retries={"max_attempts": 5, "mode": "standard"}),
            )
            with ExitStack() as cleanup:
                # submit Batch job (with request throttling)
                job_id = None
                submit_period = self.cfg.get_float("aws", "submit_period")
                while True:
                    with self._submit_lock:
                        if terminating():
                            raise WDL.runtime.Terminated(quiet=True)
                        if time.time() - self._last_submit_time[0] >= submit_period:
                            job_id = self._submit_batch_job(logger, cleanup, aws_batch, command)
                            self._last_submit_time[0] = time.time()
                            break
                    time.sleep(submit_period / 4)
                # poll Batch job status
                return self._await_batch_job(logger, cleanup, aws_batch, job_id, terminating)
        except botocore.exceptions.ClientError as exn:
            wrapper = AWSError(exn)
            logger.error(wrapper)
            raise wrapper

    def _submit_batch_job(self, logger, cleanup, aws_batch, command):
        """
        Register & submit AWS batch job, leaving a cleanup callback to deregister the transient
        job definition.
        """

        job_name = self.run_id
        if job_name.startswith("call-"):
            job_name = job_name[5:]
        if self.try_counter > 1:
            job_name += f"-try{self.try_counter}"
        # Append entropy to the job name to avoid race condition using identical job names in
        # concurrent RegisterJobDefinition requests
        job_name = randomize_job_name(job_name)

        image_tag = self.runtime_values.get("docker", "ubuntu:20.04")
        volumes, mount_points = self._prepare_mounts(logger, command)
        vcpu = self.runtime_values.get("cpu", 1)
        memory_mbytes = max(
            math.ceil(self.runtime_values.get("memory_reservation", 0) / 1048576), 1024
        )
        job_def = aws_batch.register_job_definition(
            jobDefinitionName=job_name,
            type="container",
            containerProperties={
                "image": image_tag,
                "volumes": volumes,
                "mountPoints": mount_points,
                "command": [
                    "/bin/bash",
                    "-c",
                    f"cd {self.container_dir}/work && bash ../command >> ../stdout.txt 2> >(tee -a ../stderr.txt >&2)",
                ],
                "resourceRequirements": [
                    {"type": "VCPU", "value": str(vcpu)},
                    {"type": "MEMORY", "value": str(memory_mbytes)},
                ],
            },
        )
        job_def_handle = f"{job_def['jobDefinitionName']}:{job_def['revision']}"
        logger.debug(_("registered Batch job definition", jobDefinition=job_def_handle))

        def deregister(logger, aws_batch, job_def_handle):
            try:
                aws_batch.deregister_job_definition(jobDefinition=job_def_handle)
                logger.debug(_("deregistered Batch job definition", jobDefinition=job_def_handle))
            except botocore.exceptions.ClientError as exn:
                # AWS expires job definitions after 6mo, so failing to delete them isn't fatal
                logger.warning(
                    _(
                        "failed to deregister Batch job definition",
                        jobDefinition=job_def_handle,
                        error=str(AWSError(exn)),
                    )
                )

        cleanup.callback(deregister, logger, aws_batch, job_def_handle)

        job_tags = {}
        # TODO: set a tag to indicate that this job is a retry of another
        if self.cfg.has_option("aws", "job_tags"):
            job_tags = self.cfg.get_dict("aws", "job_tags")
        job = aws_batch.submit_job(
            jobName=job_name,
            jobQueue=self._job_queue,
            jobDefinition=job_def_handle,
            timeout={"attemptDurationSeconds": self.cfg.get_int("aws", "job_timeout")},
            tags=job_tags,
        )
        logger.info(
            _(
                "AWS Batch job submitted",
                jobQueue=self._job_queue,
                jobId=job["jobId"],
                tags=job_tags,
            )
        )
        return job["jobId"]

    def _prepare_mounts(self, logger, command):
        """
        Prepare the "volumes" and "mountPoints" for the Batch job definition, assembling the
        in-container filesystem with the shared working directory, read-only input files, and
        command/stdout/stderr files.
        """

        # prepare control files
        with open(os.path.join(self.host_dir, "command"), "w") as outfile:
            outfile.write(command)
        with open(self.host_stdout_txt(), "w"):
            pass
        with open(self.host_stderr_txt(), "w"):
            pass

        # EFS mount point
        volumes = [
            {
                "name": "efs",
                "efsVolumeConfiguration": {
                    "fileSystemId": self._fs_id,
                    "transitEncryption": "ENABLED",
                },
            }
        ]
        if self._fsap_id:
            volumes[0]["efsVolumeConfiguration"]["authorizationConfig"] = {
                "accessPointId": self._fsap_id
            }
        mount_points = [{"containerPath": self._fs_mount, "sourceVolume": "efs"}]

        if self._inputs_copied:
            return volumes, mount_points

        # Prepare symlinks to the input Files & Directories
        container_prefix = os.path.join(self.container_dir, "work/_miniwdl_inputs/")
        link_dirs_made = set()
        for host_fn, container_fn in self.input_path_map.items():
            assert container_fn.startswith(container_prefix) and len(container_fn) > len(
                container_prefix
            )
            link_dn = os.path.dirname(container_fn)
            if link_dn not in link_dirs_made:
                os.makedirs(link_dn)
                link_dirs_made.add(link_dn)
            os.symlink(host_fn, container_fn)

        return volumes, mount_points

    def _await_batch_job(self, logger, cleanup, aws_batch, job_id, terminating):
        """
        Poll for Batch job success or failure & return exit code
        """
        describe_period = self.cfg.get_float("aws", "describe_period")
        cleanup.callback((lambda job_id: self._describer.unsubscribe(job_id)), job_id)
        poll_stderr = cleanup.enter_context(
            PygtailLogger(logger, self.host_stderr_txt(), callback=self.stderr_callback)
        )
        exit_code = None
        while exit_code is None:
            time.sleep(describe_period)
            job_desc = self._describer.describe(aws_batch, job_id, describe_period)
            job_status = job_desc["status"]
            if "container" in job_desc and "logStreamName" in job_desc["container"]:
                self._logStreamName = job_desc["container"]["logStreamName"]
            if job_status not in self._observed_states:
                self._observed_states.add(job_status)
                logfn = (
                    logger.notice
                    if job_status in ("RUNNING", "SUCCEEDED", "FAILED")
                    else logger.info
                )
                logdetails = {"status": job_status, "jobId": job_id}
                if self._logStreamName:
                    logdetails["logStreamName"] = self._logStreamName
                logfn(_("AWS Batch job change", **logdetails))
                if job_status == "STARTING" or (
                    job_status == "RUNNING" and "STARTING" not in self._observed_states
                ):
                    # TODO: base TaskContainer should handle this, for separation of concerns
                    cleanup.enter_context(
                        WDL.runtime._statusbar.task_running(
                            self.runtime_values.get("cpu", 0),
                            self.runtime_values.get("memory_reservation", 0),
                        )
                    )
                if job_status not in (
                    "SUBMITTED",
                    "PENDING",
                    "RUNNABLE",
                    "STARTING",
                    "RUNNING",
                    "SUCCEEDED",
                    "FAILED",
                ):
                    logger.warning(_("unknown job status from AWS Batch", status=job_status))
            if job_status == "SUCCEEDED":
                exit_code = 0
            elif job_status == "FAILED":
                reason = job_desc.get("container", {}).get("reason", None)
                status_reason = job_desc.get("statusReason", None)
                self.failure_info = {"jobId": job_id}
                if reason:
                    self.failure_info["reason"] = reason
                if status_reason:
                    self.failure_info["statusReason"] = status_reason
                if self._logStreamName:
                    self.failure_info["logStreamName"] = self._logStreamName
                if status_reason and "Host EC2" in status_reason and "terminated" in status_reason:
                    raise WDL.runtime.Interrupted(
                        "AWS Batch job interrupted (likely spot instance termination)",
                        more_info=self.failure_info,
                    )
                if "exitCode" not in job_desc.get("container", {}):
                    raise WDL.Error.RuntimeError(
                        "AWS Batch job failed", more_info=self.failure_info
                    )
                exit_code = job_desc["container"]["exitCode"]
                assert isinstance(exit_code, int) and exit_code != 0
            if "RUNNING" in self._observed_states:
                poll_stderr()
            if terminating():
                aws_batch.terminate_job(jobId=job_id, reason="terminated by miniwdl")
                raise WDL.runtime.Terminated(
                    quiet=not self._observed_states.difference(
                        {"SUBMITTED", "PENDING", "RUNNABLE", "STARTING"}
                    )
                )
        return exit_code


class BatchJobDescriber:
    """
    This singleton object handles calling the AWS Batch DescribeJobs API with up to 100 job IDs
    per request, then dispensing each job description to the thread interested in it. This helps
    avoid AWS API request rate limits when we're tracking many concurrent jobs.
    """

    JOBS_PER_REQUEST = 100  # maximum jobs per DescribeJob request

    def __init__(self):
        self.lock = threading.Lock()
        self.last_request_time = 0
        self.job_queue = []
        self.jobs = {}

    def describe(self, aws_batch, job_id, period):
        """
        Get the latest Batch job description
        """
        while True:
            with self.lock:
                if job_id not in self.jobs:
                    # register new job to be described ASAP
                    heapq.heappush(self.job_queue, (0.0, job_id))
                    self.jobs[job_id] = None
                # update as many job descriptions as possible
                self._update(aws_batch, period)
                # return the desired job description if we have it
                desc = self.jobs[job_id]
                if desc:
                    return desc
            # otherwise wait (outside the lock) and try again
            time.sleep(period / 4)

    def unsubscribe(self, job_id):
        """
        Unsubscribe from a job_id once we'll no longer be interested in it
        """
        with self.lock:
            if job_id in self.jobs:
                del self.jobs[job_id]

    def _update(self, aws_batch, period):
        # if enough time has passed since our last DescribeJobs request
        if time.time() - self.last_request_time >= period:
            # take the N least-recently described jobs
            job_ids = set()
            assert self.job_queue
            while self.job_queue and len(job_ids) < self.JOBS_PER_REQUEST:
                job_id = heapq.heappop(self.job_queue)[1]
                assert job_id not in job_ids
                if job_id in self.jobs:
                    job_ids.add(job_id)
            if not job_ids:
                return
            # describe them
            try:
                job_descs = aws_batch.describe_jobs(jobs=list(job_ids))
            finally:
                # always: bump last_request_time and re-enqueue these jobs
                self.last_request_time = time.time()
                for job_id in job_ids:
                    heapq.heappush(self.job_queue, (self.last_request_time, job_id))
            # update self.jobs with the new descriptions
            for job_desc in job_descs["jobs"]:
                job_ids.remove(job_desc["jobId"])
                self.jobs[job_desc["jobId"]] = job_desc
            assert not job_ids, "AWS Batch DescribeJobs didn't return all expected results"


class AWSError(WDL.Error.RuntimeError):
    """
    Repackage botocore.exceptions.ClientError to surface it more-informatively in miniwdl task log
    """

    def __init__(self, client_error: botocore.exceptions.ClientError):
        assert isinstance(client_error, botocore.exceptions.ClientError)
        msg = (
            f"{client_error.response['Error']['Code']}, {client_error.response['Error']['Message']}"
        )
        super().__init__(
            msg, more_info={"ResponseMetadata": client_error.response["ResponseMetadata"]}
        )