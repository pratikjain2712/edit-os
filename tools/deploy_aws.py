"""Deploy Edit OS to one EC2 instance for testing (docs/deploy-aws.md, option A).

    .venv/bin/python tools/deploy_aws.py              # first run: creates everything; later: redeploys
    .venv/bin/python tools/deploy_aws.py --status     # instance, URL and health
    .venv/bin/python tools/deploy_aws.py --dry-run    # what it would do
    .venv/bin/python tools/deploy_aws.py --instance-type t3.large --disk-gb 30   # scale up later

What it sets up (all named edit-os*, idempotent):
- S3 bucket edit-os-<account>-<region> (private, encrypted): releases and job files
- Secrets Manager secret edit-os/env: EDITOS_API_KEYS (generated once), EDITOS_ANTHROPIC_API_KEY
  (from this shell), EDITOS_S3_BUCKET
- IAM role + instance profile edit-os-instance: SSM, that bucket, that secret
- security group edit-os-web: HTTP 80 from --allow-cidr (default: anywhere; the API key protects it)
- EC2 t3.medium (4 GB) with 20 GB gp3 to start, Amazon Linux 2023, IMDSv2, no SSH key (SSM Session
  Manager instead). Rerun with a bigger --instance-type / --disk-gb to scale the same instance (a stop
  and start: a minute of downtime, the data volume is kept). Disks only grow, never shrink.
The release is `git archive HEAD` uploaded to S3; the instance builds the Docker images and runs
deploy/ec2-deploy.sh (user data on first boot, SSM Run Command after).

Stylesheets (client material, never in git or the release): `--stylesheets DIR` (the journals' .txt files) and
`--guide FILE` (the pre-editing guide .txt) upload them to s3://<bucket>/stylesheets/ (guide under guide/, SSE);
ec2-deploy.sh syncs that prefix into the container read-only (EDITOS_STYLESHEETS / EDITOS_PREEDIT_GUIDE), so the
review screen can quote the rule behind each suggestion. Without them it shows the rules' plain descriptions.

Credentials: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY of a deploy user; AWS_DEFAULT_REGION
(default ap-south-1).
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request

import boto3
from botocore.exceptions import ClientError

NAME = "edit-os"
SECRET_ID = f"{NAME}/env"
ROLE = f"{NAME}-instance"
SG = f"{NAME}-web"
AMI_PARAM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

USER_DATA = """#!/bin/bash
set -euxo pipefail
mkdir -p /opt/edit-os && cd /opt/edit-os
aws s3 cp "s3://{bucket}/{key}" release.tar.gz --region {region}
rm -rf app && mkdir app && tar -xzf release.tar.gz -C app
SECRET_ID={secret} bash app/deploy/ec2-deploy.sh > /var/log/edit-os-deploy.log 2>&1
"""


def log(msg: str) -> None:
    print(msg, flush=True)


class Deployer:
    def __init__(self, region: str, dry_run: bool = False):
        self.region, self.dry = region, dry_run
        s = boto3.Session(region_name=region)
        self.account = s.client("sts").get_caller_identity()["Account"]
        self.s3, self.ec2, self.iam = s.client("s3"), s.client("ec2"), s.client("iam")
        self.sm, self.ssm = s.client("secretsmanager"), s.client("ssm")
        self.bucket = f"{NAME}-{self.account}-{region}"

    # --- resources --------------------------------------------------------------------------

    def ensure_bucket(self) -> None:
        try:
            self.s3.head_bucket(Bucket=self.bucket)
            return
        except ClientError:
            pass
        log(f"create bucket {self.bucket}")
        if self.dry:
            return
        kw = {} if self.region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": self.region}}
        self.s3.create_bucket(Bucket=self.bucket, **kw)
        self.s3.put_public_access_block(Bucket=self.bucket, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        self.s3.put_bucket_encryption(Bucket=self.bucket, ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})

    def ensure_secret(self) -> dict:
        """The instance's environment. The API key is generated once and kept across deploys."""
        try:
            env = json.loads(self.sm.get_secret_value(SecretId=SECRET_ID)["SecretString"])
            exists = True
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
            env, exists = {}, False
        env.setdefault("EDITOS_API_KEYS", secrets.token_urlsafe(24))
        env["EDITOS_S3_BUCKET"] = self.bucket
        claude = os.environ.get("EDITOS_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if claude:
            env["EDITOS_ANTHROPIC_API_KEY"] = claude
        log(f"{'update' if exists else 'create'} secret {SECRET_ID} ({', '.join(sorted(env))})")
        if not self.dry:
            if exists:
                self.sm.put_secret_value(SecretId=SECRET_ID, SecretString=json.dumps(env))
            else:
                self.sm.create_secret(Name=SECRET_ID, SecretString=json.dumps(env))
        return env

    def ensure_role(self) -> None:
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"},
                                                          "Action": "sts:AssumeRole"}]}
        secret_arn = f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{SECRET_ID}-*"
        policy = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
             "Resource": f"arn:aws:s3:::{self.bucket}/*"},
            {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{self.bucket}"},
            {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": secret_arn},
        ]}
        try:
            self.iam.get_role(RoleName=ROLE)
        except ClientError:
            log(f"create role {ROLE}")
            if not self.dry:
                self.iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust))
                self.iam.attach_role_policy(RoleName=ROLE, PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
        if not self.dry:
            self.iam.put_role_policy(RoleName=ROLE, PolicyName=f"{NAME}-access", PolicyDocument=json.dumps(policy))
        try:
            prof = self.iam.get_instance_profile(InstanceProfileName=ROLE)["InstanceProfile"]
        except ClientError:
            log(f"create instance profile {ROLE}")
            if self.dry:
                return
            prof = self.iam.create_instance_profile(InstanceProfileName=ROLE)["InstanceProfile"]
            time.sleep(5)
        if not self.dry and not prof.get("Roles"):
            self.iam.add_role_to_instance_profile(InstanceProfileName=ROLE, RoleName=ROLE)
            time.sleep(10)  # a new profile takes a moment before EC2 accepts it

    def ensure_sg(self, cidr: str) -> str | None:
        vpc = self.ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
        if not vpc:
            sys.exit("No default VPC in this region; create one or pass a region that has it.")
        vpc_id = vpc[0]["VpcId"]
        found = self.ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SG]},
                                                          {"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]
        if found:
            sg = found[0]["GroupId"]
        else:
            log(f"create security group {SG} (HTTP from {cidr})")
            if self.dry:
                return None
            sg = self.ec2.create_security_group(GroupName=SG, Description="Edit OS web (HTTP)", VpcId=vpc_id)["GroupId"]
        if not self.dry:
            try:
                self.ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
                    "IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": cidr}]}])
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                    raise
        return sg

    def instance(self) -> dict | None:
        res = self.ec2.describe_instances(Filters=[{"Name": "tag:Name", "Values": [NAME]},
                                                   {"Name": "instance-state-name", "Values": ["pending", "running", "stopped"]}])
        found = [i for r in res["Reservations"] for i in r["Instances"]]
        return found[0] if found else None

    # --- release ----------------------------------------------------------------------------

    def upload_release(self) -> str:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
        if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True).strip():
            log("note: uncommitted changes are not deployed (the release is git HEAD)")
        key = f"releases/{NAME}-{sha}.tar.gz"
        log(f"upload release {sha} -> s3://{self.bucket}/{key}")
        if not self.dry:
            data = subprocess.check_output(["git", "archive", "--format=tar.gz", "HEAD"], cwd=ROOT)
            self.s3.upload_fileobj(io.BytesIO(data), self.bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"})
        return key

    def upload_stylesheets(self, directory: str | None, guide: str | None) -> None:
        """Journal stylesheets (*.txt) and the guide to s3://<bucket>/stylesheets/ (private, encrypted). Only .txt
        files are sent; nothing is read or kept anywhere else."""
        files = sorted(glob.glob(os.path.join(directory, "*.txt"))) if directory else []
        if directory and not files:
            sys.exit(f"--stylesheets {directory}: no .txt files")
        if guide and not os.path.isfile(guide):
            sys.exit(f"--guide {guide}: not a file")
        todo = [(p, f"stylesheets/{os.path.basename(p)}") for p in files]
        if guide:
            todo.append((guide, f"stylesheets/guide/{os.path.basename(guide)}"))
        if not todo:
            return
        log(f"upload {len(files)} stylesheet(s){' + guide' if guide else ''} -> s3://{self.bucket}/stylesheets/")
        if self.dry:
            return
        for path, key in todo:
            self.s3.upload_file(path, self.bucket, key, ExtraArgs={"ServerSideEncryption": "AES256"})

    def resize(self, inst: dict, itype: str, disk_gb: int) -> None:
        """Scale the existing instance in place: instance type (needs a stop) and root volume (grow only)."""
        iid = inst["InstanceId"]
        vol_id = next(m["Ebs"]["VolumeId"] for m in inst["BlockDeviceMappings"] if m["DeviceName"] == inst["RootDeviceName"])
        size = self.ec2.describe_volumes(VolumeIds=[vol_id])["Volumes"][0]["Size"]
        if disk_gb < size:
            sys.exit(f"--disk-gb {disk_gb} is smaller than the current {size} GB; EBS volumes cannot shrink.")
        if inst["InstanceType"] == itype and disk_gb == size:
            return
        log(f"resize {iid}: {inst['InstanceType']} -> {itype}, {size} GB -> {disk_gb} GB (stop and start)")
        if self.dry:
            return
        if disk_gb > size:  # the root partition and filesystem grow on the next boot (cloud-init growpart)
            self.ec2.modify_volume(VolumeId=vol_id, Size=disk_gb)
        if inst["State"]["Name"] != "stopped":
            self.ec2.stop_instances(InstanceIds=[iid])
            self.ec2.get_waiter("instance_stopped").wait(InstanceIds=[iid])
        if inst["InstanceType"] != itype:
            self.ec2.modify_instance_attribute(InstanceId=iid, InstanceType={"Value": itype})
        self.ec2.start_instances(InstanceIds=[iid])
        self.ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
        inst["State"]["Name"] = "running"

    def launch(self, key: str, sg: str, itype: str, disk_gb: int) -> str:
        ami = self.ssm.get_parameter(Name=AMI_PARAM)["Parameter"]["Value"]
        log(f"launch {itype} ({ami})")
        if self.dry:
            return "i-dry-run"
        user_data = USER_DATA.format(bucket=self.bucket, key=key, region=self.region, secret=SECRET_ID)
        for attempt in range(6):  # the instance profile can take a few seconds to become usable
            try:
                r = self.ec2.run_instances(
                    ImageId=ami, InstanceType=itype, MinCount=1, MaxCount=1, SecurityGroupIds=[sg],
                    IamInstanceProfile={"Name": ROLE}, UserData=user_data,
                    MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
                    BlockDeviceMappings=[{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": disk_gb, "VolumeType": "gp3",
                                                                             "Encrypted": True}}],
                    TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": NAME}]}])
                return r["Instances"][0]["InstanceId"]
            except ClientError as e:
                if "instance profile" not in str(e).lower() or attempt == 5:
                    raise
                time.sleep(10)
        raise RuntimeError("unreachable")

    def redeploy(self, inst: dict, key: str) -> None:
        if inst["State"]["Name"] == "stopped":
            log("start stopped instance")
            if not self.dry:
                self.ec2.start_instances(InstanceIds=[inst["InstanceId"]])
                self.ec2.get_waiter("instance_running").wait(InstanceIds=[inst["InstanceId"]])
        log(f"redeploy on {inst['InstanceId']} via SSM")
        if self.dry:
            return
        script = USER_DATA.format(bucket=self.bucket, key=key, region=self.region, secret=SECRET_ID)
        cmd = self._ssm_send(inst["InstanceId"], script.splitlines()[1:])
        self._ssm_wait(inst["InstanceId"], cmd)

    def _ssm_send(self, iid: str, lines: list[str]) -> str:
        for _ in range(30):  # the SSM agent registers a minute or two after boot
            try:
                return self.ssm.send_command(InstanceIds=[iid], DocumentName="AWS-RunShellScript",
                                             Parameters={"commands": lines, "executionTimeout": ["3600"]},
                                             TimeoutSeconds=600)["Command"]["CommandId"]
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidInstanceId":
                    raise
                time.sleep(10)
        sys.exit("SSM agent on the instance did not register")

    def _ssm_wait(self, iid: str, cmd: str) -> None:
        while True:
            time.sleep(15)
            try:
                inv = self.ssm.get_command_invocation(CommandId=cmd, InstanceId=iid)
            except ClientError:
                continue
            if inv["Status"] not in ("Pending", "InProgress", "Delayed"):
                log(f"deploy script: {inv['Status']}")
                if inv["Status"] != "Success":
                    log((inv.get("StandardErrorContent") or "")[-3000:])
                    tail = self._ssm_tail(iid)
                    sys.exit(f"deploy failed; last lines of /var/log/edit-os-deploy.log:\n{tail}")
                return

    def _ssm_tail(self, iid: str) -> str:
        cmd = self._ssm_send(iid, ["tail -n 40 /var/log/edit-os-deploy.log"])
        for _ in range(20):
            time.sleep(3)
            inv = self.ssm.get_command_invocation(CommandId=cmd, InstanceId=iid)
            if inv["Status"] == "Success":
                return inv["StandardOutputContent"]
        return "(could not read the log)"


def wait_healthy(url: str, minutes: int = 25) -> bool:
    end = time.time() + minutes * 60
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=10) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(20)
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "ap-south-1")
    ap.add_argument("--instance-type", default="t3.medium", help="t3.medium (4 GB) to start; t3.large (8 GB) for more load")
    ap.add_argument("--disk-gb", type=int, default=20, help="root volume size; can grow later, never shrink")
    ap.add_argument("--allow-cidr", default="0.0.0.0/0", help="who may reach port 80")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--no-wait", action="store_true", help="do not wait for the site to answer")
    ap.add_argument("--stylesheets", default=None,
                    help="directory of journal stylesheet .txt files to upload to s3://<bucket>/stylesheets/")
    ap.add_argument("--guide", default=None,
                    help="pre-editing guide .txt to upload to s3://<bucket>/stylesheets/guide/")
    a = ap.parse_args(argv)

    d = Deployer(a.region, a.dry_run)
    log(f"account {d.account}, region {a.region}")
    inst = d.instance()
    if a.status:
        if not inst:
            log("no edit-os instance")
            return 1
        ip = inst.get("PublicIpAddress")
        log(f"{inst['InstanceId']} {inst['State']['Name']} http://{ip}")
        return 0
    d.ensure_bucket()
    env = d.ensure_secret()
    d.ensure_role()
    sg = d.ensure_sg(a.allow_cidr)
    d.upload_stylesheets(a.stylesheets, a.guide)
    key = d.upload_release()
    if inst:
        d.resize(inst, a.instance_type, a.disk_gb)
        d.redeploy(inst, key)
        iid = inst["InstanceId"]
    else:
        iid = d.launch(key, sg, a.instance_type, a.disk_gb)
    if a.dry_run:
        return 0
    d.ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    ip = d.ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0].get("PublicIpAddress")
    url = f"http://{ip}"
    log(f"instance {iid}: {url}")
    if not a.no_wait:
        log("waiting for the site (first boot builds the images: about 10 minutes)")
        log("site is up" if wait_healthy(url) else "site did not answer yet (check: tools/deploy_aws.py --status)")
    log(f"\nReview screen: {url}\nAPI key (paste it when the screen asks): {env['EDITOS_API_KEYS'].split(',')[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
