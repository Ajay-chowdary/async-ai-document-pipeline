# Terraform — single-EC2 demo

Provisions one Amazon Linux 2023 instance in the default VPC, installs Docker
and Compose via `user_data.sh`, clones this repository, reads `OPENAI_API_KEY`
from SSM Parameter Store, and runs `docker compose up`.

This is a portfolio demo on a single box. It is not a production architecture.

## What production would look like instead

- Managed PostgreSQL (RDS) and Redis (ElastiCache) in private subnets
- Secrets in Secrets Manager or SSM, injected by the orchestrator — not a
  hand-written `.env` on a root volume
- Private networking, no public database ports, restricted egress where possible
- An ALB (or similar) with TLS in front of multiple tasks/instances
- ECS/EKS/Fargate (or equivalent) instead of Compose on one EC2 host
- No single point of failure: this design is not highly available

## Prerequisites

1. AWS credentials with rights to create EC2, IAM, security groups and to read
   the SSM parameter you create below.
2. Terraform >= 1.5.
3. An SSM SecureString parameter holding the OpenAI key:

```bash
aws ssm put-parameter \
  --name /docpipeline/openai_api_key \
  --type SecureString \
  --value "sk-..." \
  --overwrite
```

4. Your public IP (or office CIDR) for SSH and HTTP — both are required
   variables with no open-world default.

## Apply

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars — set ssh_ingress_cidr, http_ingress_cidr,
# openai_ssm_parameter_name, git_repo_url, and key_name

terraform init
terraform plan
terraform apply
```

Useful outputs:

```bash
terraform output dashboard_url
terraform output ssh_example
```

Boot takes several minutes (packages, image pulls, migrations). Follow progress:

```bash
ssh ec2-user@<public_ip> 'sudo tail -f /var/log/docpipeline-user-data.log'
```

Or with Session Manager (IAM role includes `AmazonSSMManagedInstanceCore`):

```bash
aws ssm start-session --target $(terraform output -raw instance_id)
```

## Destroy

```bash
terraform destroy
```

Named Docker volumes live on the instance root volume and disappear with it.

## Security notes for this demo

- `OPENAI_API_KEY` is never a Terraform variable. The instance reads it from SSM
  at boot with an IAM policy scoped to that parameter ARN.
- SSH (22) and the API (8000) only accept the CIDRs you pass in. There is no
  `0.0.0.0/0` default for either.
- The API and dashboard still have no application authentication. Restrict
  `http_ingress_cidr` to operators you trust.
- Postgres and Redis listen only on the Docker network inside the instance;
  they are not published to the host in `docker-compose.yml`.
