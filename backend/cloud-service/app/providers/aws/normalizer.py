"""
AWS Resource Normalization Engine — Phase 3B.3 Architecture.

Standardizes heterogeneous AWS SDK responses into normalized, JSON-serializable
resource dictionaries aligned with the CloudResource model and ResourceType enum.

Guarantees:
- Strict JSON-serializability (converts datetimes to ISO 8601 strings, sets to lists).
- Normalized resource_type values matching ResourceType enum.
- Safe handling of missing or optional AWS metadata fields.
- Zero inclusion of secrets, passwords, or temporary credentials.
"""

from datetime import datetime, date
from typing import Any, Dict, List, Optional
from app.models.cloud_resource import ResourceType


def json_sanitize(val: Any) -> Any:
    """
    Recursively convert AWS response types (datetime, sets, bytes) into JSON-serializable primitives.
    """
    if val is None:
        return None
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, (set, tuple)):
        return [json_sanitize(item) for item in val]
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    if isinstance(val, dict):
        return {str(k): json_sanitize(v) for k, v in val.items()}
    if isinstance(val, list):
        return [json_sanitize(item) for item in val]
    return val


def parse_aws_tags(tag_input: Any) -> Dict[str, str]:
    """
    Convert AWS tag structures ([{'Key': '...', 'Value': '...'}, ...]) into a flat string dictionary.
    Also handles pre-flattened dictionaries gracefully.
    """
    if not tag_input:
        return {}
    if isinstance(tag_input, dict):
        return {str(k): str(v) for k, v in tag_input.items() if v is not None}
    if isinstance(tag_input, list):
        result = {}
        for item in tag_input:
            if isinstance(item, dict):
                k = item.get("Key") or item.get("key")
                v = item.get("Value") if "Value" in item else item.get("value")
                if k is not None and v is not None:
                    result[str(k)] = str(v)
        return result
    return {}


class ResourceNormalizer:
    """
    Transforms raw AWS service payloads into unified normalized dictionaries.
    """

    # ── EC2 Normalization ───────────────────────────────────────────────────

    @staticmethod
    def normalize_ec2_instance(raw: Dict[str, Any], region: str) -> Dict[str, Any]:
        """
        Normalize an AWS EC2 instance description dictionary.
        """
        instance_id = raw.get("InstanceId", "")
        tags = parse_aws_tags(raw.get("Tags", []))
        name = tags.get("Name") or instance_id

        # Operational status: running, stopped, pending, shutting-down, terminated, stopping
        state_dict = raw.get("State", {})
        status = state_dict.get("Name", "unknown")

        placement = raw.get("Placement", {})
        az = placement.get("AvailabilityZone")

        security_groups = [
            {"group_id": sg.get("GroupId"), "group_name": sg.get("GroupName")}
            for sg in raw.get("SecurityGroups", [])
            if sg.get("GroupId")
        ]

        metadata = {
            "instance_type": raw.get("InstanceType"),
            "state_code": state_dict.get("Code"),
            "state_name": status,
            "availability_zone": az,
            "vpc_id": raw.get("VpcId"),
            "subnet_id": raw.get("SubnetId"),
            "private_ip": raw.get("PrivateIpAddress"),
            "public_ip": raw.get("PublicIpAddress"),
            "private_dns_name": raw.get("PrivateDnsName"),
            "public_dns_name": raw.get("PublicDnsName"),
            "architecture": raw.get("Architecture"),
            "image_id": raw.get("ImageId"),
            "launch_time": json_sanitize(raw.get("LaunchTime")),
            "security_groups": security_groups,
            "iam_instance_profile": raw.get("IamInstanceProfile", {}).get("Arn"),
            "key_name": raw.get("KeyName"),
            "hypervisor": raw.get("Hypervisor"),
            "root_device_type": raw.get("RootDeviceType"),
            "root_device_name": raw.get("RootDeviceName"),
        }

        return {
            "provider": "aws",
            "resource_type": ResourceType.EC2_INSTANCE.value,
            "provider_resource_id": instance_id,
            "name": name,
            "region": region,
            "status": status,
            "tags": tags,
            "metadata": json_sanitize(metadata),
        }

    # ── S3 Normalization ────────────────────────────────────────────────────

    @staticmethod
    def normalize_s3_bucket(
        bucket_name: str,
        region: str,
        creation_date: Any = None,
        versioning_status: Optional[str] = None,
        encryption: Optional[Dict[str, Any]] = None,
        tags: Optional[Dict[str, str]] = None,
        permissions_status: Optional[Dict[str, bool]] = None,
    ) -> Dict[str, Any]:
        """
        Normalize an AWS S3 bucket with regional, versioning, and encryption attributes.
        """
        clean_tags = parse_aws_tags(tags)
        status = "available"

        metadata = {
            "creation_date": json_sanitize(creation_date),
            "versioning_status": versioning_status or "Disabled",
            "encryption": encryption or {"status": "Disabled"},
            "arn": f"arn:aws:s3:::{bucket_name}",
            "permissions_accessible": permissions_status or {
                "tags": bool(tags is not None),
                "encryption": bool(encryption is not None),
                "versioning": bool(versioning_status is not None),
            },
        }

        return {
            "provider": "aws",
            "resource_type": ResourceType.S3_BUCKET.value,
            "provider_resource_id": bucket_name,
            "name": bucket_name,
            "region": region,
            "status": status,
            "tags": clean_tags,
            "metadata": json_sanitize(metadata),
        }

    # ── VPC Normalization ───────────────────────────────────────────────────

    @staticmethod
    def normalize_vpc(
        raw: Dict[str, Any],
        region: str,
        subnets: Optional[List[str]] = None,
        route_tables: Optional[List[str]] = None,
        internet_gateways: Optional[List[str]] = None,
        security_groups: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Normalize an AWS VPC with associated network reference identifiers.
        """
        vpc_id = raw.get("VpcId", "")
        tags = parse_aws_tags(raw.get("Tags", []))
        name = tags.get("Name") or vpc_id
        status = raw.get("State", "available")

        cidr_associations = [
            c.get("CidrBlock")
            for c in raw.get("CidrBlockAssociationSet", [])
            if c.get("CidrBlockState", {}).get("State") == "associated" and c.get("CidrBlock")
        ]

        metadata = {
            "cidr_block": raw.get("CidrBlock"),
            "cidr_block_associations": cidr_associations or ([raw.get("CidrBlock")] if raw.get("CidrBlock") else []),
            "is_default": bool(raw.get("IsDefault", False)),
            "dhcp_options_id": raw.get("DhcpOptionsId"),
            "instance_tenancy": raw.get("InstanceTenancy", "default"),
            "subnets": subnets or [],
            "route_tables": route_tables or [],
            "internet_gateways": internet_gateways or [],
            "security_groups": security_groups or [],
        }

        return {
            "provider": "aws",
            "resource_type": ResourceType.VPC.value,
            "provider_resource_id": vpc_id,
            "name": name,
            "region": region,
            "status": status,
            "tags": tags,
            "metadata": json_sanitize(metadata),
        }

    @staticmethod
    def normalize_subnet(raw: Dict[str, Any], region: str) -> Dict[str, Any]:
        """
        Normalize an AWS Subnet.
        """
        subnet_id = raw.get("SubnetId", "")
        tags = parse_aws_tags(raw.get("Tags", []))
        name = tags.get("Name") or subnet_id
        status = raw.get("State", "available")

        metadata = {
            "vpc_id": raw.get("VpcId"),
            "cidr_block": raw.get("CidrBlock"),
            "availability_zone": raw.get("AvailabilityZone"),
            "available_ip_address_count": raw.get("AvailableIpAddressCount"),
            "default_for_az": raw.get("DefaultForAz", False),
            "map_public_ip_on_launch": raw.get("MapPublicIpOnLaunch", False),
            "arn": raw.get("SubnetArn"),
        }

        return {
            "provider": "aws",
            "resource_type": ResourceType.SUBNET.value,
            "provider_resource_id": subnet_id,
            "name": name,
            "region": region,
            "status": status,
            "tags": tags,
            "metadata": json_sanitize(metadata),
        }

    @staticmethod
    def normalize_security_group(raw: Dict[str, Any], region: str) -> Dict[str, Any]:
        """
        Normalize an AWS Security Group.
        """
        group_id = raw.get("GroupId", "")
        tags = parse_aws_tags(raw.get("Tags", []))
        group_name = raw.get("GroupName")
        name = tags.get("Name") or group_name or group_id

        # Count ingress/egress rules safely
        ingress_count = len(raw.get("IpPermissions", []))
        egress_count = len(raw.get("IpPermissionsEgress", []))

        metadata = {
            "vpc_id": raw.get("VpcId"),
            "group_name": group_name,
            "description": raw.get("Description"),
            "ingress_rule_count": ingress_count,
            "egress_rule_count": egress_count,
            "arn": raw.get("SecurityGroupArn"),
        }

        return {
            "provider": "aws",
            "resource_type": ResourceType.SECURITY_GROUP.value,
            "provider_resource_id": group_id,
            "name": name,
            "region": region,
            "status": "active",
            "tags": tags,
            "metadata": json_sanitize(metadata),
        }

    # ── RDS Normalization ───────────────────────────────────────────────────

    @staticmethod
    def normalize_rds_instance(raw: Dict[str, Any], region: str) -> Dict[str, Any]:
        """
        Normalize an AWS RDS DBInstance description dictionary.
        Strictly excludes database passwords, master credentials, or internal hash data.
        """
        db_id = raw.get("DBInstanceIdentifier", "")
        tags = parse_aws_tags(raw.get("TagList") or raw.get("Tags", []))
        name = tags.get("Name") or db_id
        status = raw.get("DBInstanceStatus", "unknown")

        endpoint = raw.get("Endpoint")
        endpoint_info = None
        if endpoint:
            endpoint_info = {
                "address": endpoint.get("Address"),
                "port": endpoint.get("Port"),
            }

        subnet_group = raw.get("DBSubnetGroup")
        subnet_group_info = None
        if subnet_group:
            subnet_group_info = {
                "name": subnet_group.get("DBSubnetGroupName"),
                "vpc_id": subnet_group.get("VpcId"),
                "status": subnet_group.get("SubnetGroupStatus"),
            }

        vpc_security_groups = [
            sg.get("VpcSecurityGroupId")
            for sg in raw.get("VpcSecurityGroups", [])
            if sg.get("VpcSecurityGroupId")
        ]

        metadata = {
            "db_instance_arn": raw.get("DBInstanceArn"),
            "engine": raw.get("Engine"),
            "engine_version": raw.get("EngineVersion"),
            "instance_class": raw.get("DBInstanceClass"),
            "allocated_storage_gb": raw.get("AllocatedStorage"),
            "storage_type": raw.get("StorageType"),
            "storage_encrypted": bool(raw.get("StorageEncrypted", False)),
            "kms_key_id": raw.get("KmsKeyId"),
            "availability_zone": raw.get("AvailabilityZone"),
            "multi_az": bool(raw.get("MultiAZ", False)),
            "db_subnet_group": subnet_group_info,
            "vpc_security_groups": vpc_security_groups,
            "endpoint": endpoint_info,
            "publicly_accessible": bool(raw.get("PubliclyAccessible", False)),
            "auto_minor_version_upgrade": bool(raw.get("AutoMinorVersionUpgrade", False)),
            "backup_retention_period": raw.get("BackupRetentionPeriod"),
            "preferred_backup_window": raw.get("PreferredBackupWindow"),
            "preferred_maintenance_window": raw.get("PreferredMaintenanceWindow"),
            "instance_create_time": json_sanitize(raw.get("InstanceCreateTime")),
        }

        return {
            "provider": "aws",
            "resource_type": ResourceType.RDS_INSTANCE.value,
            "provider_resource_id": db_id,
            "name": name,
            "region": region,
            "status": status,
            "tags": tags,
            "metadata": json_sanitize(metadata),
        }

