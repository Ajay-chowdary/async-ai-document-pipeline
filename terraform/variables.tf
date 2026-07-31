variable "aws_region" {
  type        = string
  description = "AWS region for the demo instance."
  default     = "us-east-1"
}

variable "project_name" {
  type        = string
  description = "Short name used for resource Name tags and the app directory."
  default     = "docpipeline"
}

variable "instance_type" {
  type        = string
  description = "EC2 instance type. The demo runs Postgres, Redis, API and a worker on one box."
  default     = "t3.small"
}

variable "root_volume_gb" {
  type        = number
  description = "Root volume size in GiB."
  default     = 30
}

variable "key_name" {
  type        = string
  description = "Existing EC2 key pair name for SSH. Leave empty to rely on SSM Session Manager only."
  default     = ""
}

variable "ssh_ingress_cidr" {
  type        = string
  description = "CIDR allowed to reach SSH (port 22). Required explicitly — there is no open-world default."
}

variable "http_ingress_cidr" {
  type        = string
  description = "CIDR allowed to reach the API/dashboard (port 8000). Required explicitly — there is no open-world default."
}

variable "openai_ssm_parameter_name" {
  type        = string
  description = "SSM Parameter Store name for OPENAI_API_KEY (SecureString). Read at boot; never placed in Terraform state as a value."
}

variable "git_repo_url" {
  type        = string
  description = "Git repository to clone onto the instance (HTTPS). Must be readable without interactive auth."
}

variable "git_ref" {
  type        = string
  description = "Git branch or tag to check out."
  default     = "main"
}

variable "llm_provider" {
  type        = string
  description = "LLM_PROVIDER written into the instance .env (openai or fake)."
  default     = "openai"

  validation {
    condition     = contains(["openai", "fake"], var.llm_provider)
    error_message = "llm_provider must be openai or fake."
  }
}

variable "tags" {
  type        = map(string)
  description = "Extra tags applied to every supported resource via the provider default_tags."
  default = {
    Project   = "async-ai-document-pipeline"
    ManagedBy = "terraform"
    Purpose   = "portfolio-demo"
  }
}
