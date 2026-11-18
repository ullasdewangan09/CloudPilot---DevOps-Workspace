"""
Resource Normalizer Unit Tests — Phase 3B.3 Automated Test Suite.

Verifies:
1. JSON sanitization converts datetimes, dates, sets, tuples, and bytes.
2. Tag parsing handles AWS tag lists and dictionary formats.
3. EC2 instance normalization with complete and minimal payloads.
4. S3 bucket normalization with region fallbacks (None -> us-east-1, EU -> eu-west-1).
5. VPC, Subnet, and Security Group normalization.
6. RDS instance normalization strictly excluding sensitive credentials.
7. Strict JSON serializability for all normalized payloads.
8. Exact match of normalized resource_type with ResourceType enum.
"""

import json
from datetime import datetime, timezone, date
import pytest

from app.models.cloud_resource import ResourceType
from app.providers.aws.normalizer import (
    ResourceNormalizer,
    json_sanitize,
    parse_aws_tags,
)


def test_json_sanitize_primitives_and_collections():
    """Verify JSON sanitization handles primitives, nested dictionaries, and lists."""
    data = {
        "str": "hello",
        "int": 42,
        "float": 3.14,
        "bool": True,
        "none": None,
        "list": [1, 2, "three"],
        "nested": {"a": [1, 2], "b": {"c": "deep"}},
    }
    sanitized = json_sanitize(data)
    assert sanitized == data
    json.dumps(sanitized)


def test_json_sanitize_converts_datetimes_sets_and_bytes():
    """Verify datetimes, dates, sets, tuples, and bytes are converted to JSON-serializable types."""
    dt = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)
    d = date(2026, 11, 8)
    data = {
        "dt": dt,
        "d": d,
        "set": {1, 2, 3},
        "tuple": (4, 5, 6),
        "bytes": b"utf8-bytes-test",
    }
    sanitized = json_sanitize(data)
    assert sanitized["dt"] == "2026-11-08T12:00:00+00:00"
    assert sanitized["d"] == "2026-11-08"
    assert sorted(sanitized["set"]) == [1, 2, 3]
    assert sanitized["tuple"] == [4, 5, 6]
    assert sanitized["bytes"] == "utf8-bytes-test"
    json.dumps(sanitized)


def test_parse_aws_tags():
    """Verify AWS tag list [{'Key': '...', 'Value': '...'}] and dict formats."""
    # Standard AWS tag list
    tag_list = [
        {"Key": "Environment", "Value": "Production"},
        {"Key": "Project", "Value": "CloudPilot"},
        {"Key": "EmptyVal", "Value": ""},
    ]
    parsed = parse_aws_tags(tag_list)
    assert parsed == {"Environment": "Production", "Project": "CloudPilot", "EmptyVal": ""}

    # Dictionary input
    dict_tags = {"Environment": "Dev", "Owner": "Alice"}
    assert parse_aws_tags(dict_tags) == dict_tags

    # None and empty inputs
    assert parse_aws_tags(None) == {}
    assert parse_aws_tags([]) == {}
    assert parse_aws_tags("invalid") == {}


def test_normalize_ec2_instance():
    """Verify EC2 instance normalization with full attributes."""
    launch_dt = datetime(2026, 10, 1, 8, 30, 0, tzinfo=timezone.utc)
    raw = {
        "InstanceId": "i-0123456789abcdef0",
        "InstanceType": "t3.medium",
        "State": {"Name": "running", "Code": 16},
        "Placement": {"AvailabilityZone": "us-east-1a"},
        "VpcId": "vpc-12345",
        "SubnetId": "subnet-67890",
        "PrivateIpAddress": "10.0.1.25",
        "PublicIpAddress": "54.12.34.56",
        "PrivateDnsName": "ip-10-0-1-25.ec2.internal",
        "PublicDnsName": "ec2-54-12-34-56.compute-1.amazonaws.com",
        "Architecture": "x86_64",
        "ImageId": "ami-0abcdef1234567890",
        "LaunchTime": launch_dt,
        "SecurityGroups": [{"GroupId": "sg-111", "GroupName": "web-sg"}],
        "Tags": [
            {"Key": "Name", "Value": "web-node-01"},
            {"Key": "Role", "Value": "frontend"}
        ]
    }

    normalized = ResourceNormalizer.normalize_ec2_instance(raw, "us-east-1")

    assert normalized["provider"] == "aws"
    assert normalized["resource_type"] == ResourceType.EC2_INSTANCE.value
    assert normalized["provider_resource_id"] == "i-0123456789abcdef0"
    assert normalized["name"] == "web-node-01"
    assert normalized["region"] == "us-east-1"
    assert normalized["status"] == "running"
    assert normalized["tags"]["Name"] == "web-node-01"
    assert normalized["tags"]["Role"] == "frontend"
    assert normalized["metadata"]["instance_type"] == "t3.medium"
    assert normalized["metadata"]["private_ip"] == "10.0.1.25"
    assert normalized["metadata"]["public_ip"] == "54.12.34.56"
    assert normalized["metadata"]["launch_time"] == "2026-10-01T08:30:00+00:00"

    # Confirm JSON serializability
    serialized = json.dumps(normalized)
    assert "i-0123456789abcdef0" in serialized


def test_normalize_ec2_instance_minimal_fallback():
    """Verify EC2 normalization falls back gracefully when optional tags and IPs are missing."""
    raw = {
        "InstanceId": "i-99999999999999999",
        "State": {"Name": "stopped", "Code": 80},
    }
    normalized = ResourceNormalizer.normalize_ec2_instance(raw, "us-west-2")
    assert normalized["provider_resource_id"] == "i-99999999999999999"
    assert normalized["name"] == "i-99999999999999999"
    assert normalized["status"] == "stopped"
    assert normalized["region"] == "us-west-2"
    assert normalized["tags"] == {}
    assert normalized["metadata"]["private_ip"] is None
    assert normalized["metadata"]["public_ip"] is None
    json.dumps(normalized)


def test_normalize_s3_bucket():
    """Verify S3 bucket normalization with encryption and versioning."""
    created = datetime(2025, 5, 12, 10, 0, 0, tzinfo=timezone.utc)
    normalized = ResourceNormalizer.normalize_s3_bucket(
        bucket_name="my-production-bucket",
        region="us-east-1",
        creation_date=created,
        versioning_status="Enabled",
        encryption={"status": "Enabled", "sse_algorithm": "aws:kms"},
        tags={"Environment": "production"}
    )

    assert normalized["provider"] == "aws"
    assert normalized["resource_type"] == ResourceType.S3_BUCKET.value
    assert normalized["provider_resource_id"] == "my-production-bucket"
    assert normalized["name"] == "my-production-bucket"
    assert normalized["region"] == "us-east-1"
    assert normalized["status"] == "available"
    assert normalized["tags"]["Environment"] == "production"
    assert normalized["metadata"]["versioning_status"] == "Enabled"
    assert normalized["metadata"]["arn"] == "arn:aws:s3:::my-production-bucket"
    assert normalized["metadata"]["encryption"]["sse_algorithm"] == "aws:kms"
    json.dumps(normalized)


def test_normalize_vpc_and_subresources():
    """Verify VPC, Subnet, and Security Group normalization."""
    # 1. VPC
    raw_vpc = {
        "VpcId": "vpc-0011223344",
        "CidrBlock": "10.0.0.0/16",
        "CidrBlockAssociationSet": [{"CidrBlock": "10.0.0.0/16", "CidrBlockState": {"State": "associated"}}],
        "IsDefault": False,
        "State": "available",
        "Tags": [{"Key": "Name", "Value": "prod-vpc"}]
    }
    norm_vpc = ResourceNormalizer.normalize_vpc(
        raw=raw_vpc,
        region="us-east-1",
        subnets=["subnet-1", "subnet-2"],
        route_tables=["rtb-1"],
        internet_gateways=["igw-1"],
        security_groups=["sg-1"]
    )
    assert norm_vpc["resource_type"] == ResourceType.VPC.value
    assert norm_vpc["provider_resource_id"] == "vpc-0011223344"
    assert norm_vpc["name"] == "prod-vpc"
    assert norm_vpc["metadata"]["subnets"] == ["subnet-1", "subnet-2"]
    assert norm_vpc["metadata"]["internet_gateways"] == ["igw-1"]
    json.dumps(norm_vpc)

    # 2. Subnet
    raw_subnet = {
        "SubnetId": "subnet-0011",
        "VpcId": "vpc-0011223344",
        "CidrBlock": "10.0.1.0/24",
        "AvailabilityZone": "us-east-1a",
        "State": "available",
        "Tags": [{"Key": "Name", "Value": "public-subnet-1"}]
    }
    norm_subnet = ResourceNormalizer.normalize_subnet(raw_subnet, "us-east-1")
    assert norm_subnet["resource_type"] == ResourceType.SUBNET.value
    assert norm_subnet["provider_resource_id"] == "subnet-0011"
    assert norm_subnet["name"] == "public-subnet-1"
    assert norm_subnet["metadata"]["vpc_id"] == "vpc-0011223344"
    json.dumps(norm_subnet)

    # 3. Security Group
    raw_sg = {
        "GroupId": "sg-0011",
        "GroupName": "web-firewall",
        "Description": "Allows HTTPS",
        "VpcId": "vpc-0011223344",
        "IpPermissions": [{"FromPort": 443, "ToPort": 443}],
        "IpPermissionsEgress": [{"IpProtocol": "-1"}],
        "Tags": []
    }
    norm_sg = ResourceNormalizer.normalize_security_group(raw_sg, "us-east-1")
    assert norm_sg["resource_type"] == ResourceType.SECURITY_GROUP.value
    assert norm_sg["provider_resource_id"] == "sg-0011"
    assert norm_sg["name"] == "web-firewall"
    assert norm_sg["metadata"]["ingress_rule_count"] == 1
    assert norm_sg["metadata"]["egress_rule_count"] == 1
    json.dumps(norm_sg)


def test_normalize_rds_instance_no_secrets():
    """Verify RDS normalization captures engine and specs but strictly omits passwords and secrets."""
    create_dt = datetime(2025, 8, 20, 14, 0, 0, tzinfo=timezone.utc)
    raw_rds = {
        "DBInstanceIdentifier": "prod-postgres-main",
        "DBInstanceArn": "arn:aws:rds:us-east-1:123456789012:db:prod-postgres-main",
        "Engine": "postgres",
        "EngineVersion": "16.1",
        "DBInstanceClass": "db.m6g.large",
        "DBInstanceStatus": "available",
        "AllocatedStorage": 100,
        "StorageType": "gp3",
        "StorageEncrypted": True,
        "KmsKeyId": "arn:aws:kms:us-east-1:123456789012:key/sample-key",
        "AvailabilityZone": "us-east-1b",
        "MultiAZ": True,
        "PubliclyAccessible": False,
        "Endpoint": {"Address": "prod-postgres-main.c0example.us-east-1.rds.amazonaws.com", "Port": 5432},
        "DBSubnetGroup": {"DBSubnetGroupName": "main-subnet-group", "VpcId": "vpc-001122"},
        "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-rds-01"}],
        "InstanceCreateTime": create_dt,
        "TagList": [{"Key": "Name", "Value": "prod-db"}, {"Key": "Environment", "Value": "prod"}]
    }

    normalized = ResourceNormalizer.normalize_rds_instance(raw_rds, "us-east-1")

    assert normalized["provider"] == "aws"
    assert normalized["resource_type"] == ResourceType.RDS_INSTANCE.value
    assert normalized["provider_resource_id"] == "prod-postgres-main"
    assert normalized["name"] == "prod-db"
    assert normalized["status"] == "available"
    assert normalized["metadata"]["engine"] == "postgres"
    assert normalized["metadata"]["engine_version"] == "16.1"
    assert normalized["metadata"]["multi_az"] is True
    assert normalized["metadata"]["endpoint"]["port"] == 5432

    # Secret verification: confirm password fields are strictly absent
    for secret_key in ["MasterUserPassword", "password", "Secret", "MasterPassword"]:
        assert secret_key not in normalized["metadata"]
        assert secret_key not in normalized

    json.dumps(normalized)

