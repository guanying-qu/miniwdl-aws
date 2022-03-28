import os
import boto3
import base64
import json
import uuid
import requests
from WDL._util import StructuredLogMessage as _


def detect_aws_region(cfg):
    if cfg and cfg.has_option("aws", "region") and cfg.get("aws", "region"):
        return cfg.get("aws", "region")

    # check environment variables
    for ev in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        if os.environ.get(ev):
            return os.environ[ev]

    # check boto3, which will load ~/.aws
    if boto3.DEFAULT_SESSION and boto3.DEFAULT_SESSION.region_name:
        return boto3.DEFAULT_SESSION.region_name
    session = boto3.Session()
    if session.region_name:
        return session.region_name

    # query EC2 metadata
    try:
        return requests.get(
            "http://169.254.169.254/latest/meta-data/placement/region", timeout=2.0
        ).text
    except:
        pass

    return None


def randomize_job_name(job_name):
    # Append entropy to the Batch job name to avoid race condition using identical names in
    # concurrent RegisterJobDefinition requests
    return (
        job_name[:103]  # 119 + 1 + 8 = 128
        + "-"
        + base64.b32encode(uuid.uuid4().bytes[:5]).lower().decode()
    )






END_OF_LOG = "[miniwdl_run_s3upload] -- END OF LOG --"
